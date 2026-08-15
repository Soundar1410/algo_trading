"""Position manager — the engine's book, and the seam to real execution.

Ported from the reference repository's ``framework/execution/order_manager.py``
(Phase 3 Part 2b-i). Owns a strategy's open position(s), translates engine
decisions into fills, and records each completed round-trip as a
:class:`~common.engine.models.Trade`.

The one structural change (deviation D19, the position seam)
-------------------------------------------------------------
The reference's manager calls a broker directly::

    self._broker.buy_market(contract, lots, ref_price=..., ts=...)

This repository's :class:`~common.broker.base.Broker` has a different, deliberate
shape: ``submit(OrderIntent, Quote) -> Order``, idempotent on a correlation ID,
driven by :class:`~common.execution.lifecycle.OrderLifecycle` which persists the
signal, reserves the intent, calls the broker outside any transaction, and applies
the fill — in that order, because that ordering is the crash-safety property.

Rather than port a second broker interface that bypasses all of it, the manager
here talks to a narrow :class:`ExecutionGateway`. Two implementations:

* :class:`InMemoryGateway` (here, Part 2b-i) — fills at the reference price plus
  adverse slippage and prices charges with the **existing**
  :class:`~common.broker.costs.ChargesCalculator`. No database, so the engine and
  its ported tests run offline exactly as the reference's did.
* ``LifecycleGateway`` (Part 2b-ii) — drives ``OrderLifecycle``, so every open and
  close is persisted with a correlation ID, a fill row and ``execution_mode``.

Everything else — the MFE/MAE bookkeeping, the entry-charge stashing, the merged
breakdown, the ``keep the position keyed by security_id`` structure — is the
reference's, unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from common.broker.costs import ChargesCalculator, CostRates
from common.logging import get_logger
from common.models import ExitReason, Side

from .models import OpenPosition, OptionContract, OrderSide, Trade

log = get_logger(__name__)


@dataclass(frozen=True)
class FillOutcome:
    """What one leg's execution produced. The reference's ``OrderResult``."""

    fill_price: float
    charges: float = 0.0
    charges_breakdown: dict[str, float] = field(default_factory=dict)
    #: The correlation ID this fill's order actually persisted under, when
    #: the gateway can report one. ``LifecycleGateway`` always can (every
    #: fill that reaches ``PositionManager`` came from a real, persisted
    #: order); ``InMemoryGateway`` never does (there is no persisted order
    #: to name) — ``None`` there is honest, not a gap.
    correlation_id: str | None = None


@runtime_checkable
class ExecutionGateway(Protocol):
    """How the position manager gets a leg executed.

    Two verbs, matching the reference's broker contract, because the manager's
    body is a port and a wider interface here would mean editing it.

    ``stop_price``/``target_price`` (Phase 6 Part 2, optional, default ``None``)
    are the one addition since the port: observability only, threaded through to
    ``positions.stop_price``/``.target_price`` where a gateway persists. Neither
    verb's own decision logic consults them — a call that doesn't pass them
    behaves exactly as before.
    """

    def buy(
        self,
        contract: OptionContract,
        lots: int,
        *,
        ref_price: float,
        ts: datetime,
        stop_price: float | None = None,
        target_price: float | None = None,
        basket_id: str | None = None,
        leg_id: str | None = None,
        cycle_id: str | None = None,
    ) -> FillOutcome: ...

    def sell(
        self,
        contract: OptionContract,
        lots: int,
        *,
        ref_price: float,
        ts: datetime,
        stop_price: float | None = None,
        target_price: float | None = None,
        basket_id: str | None = None,
        leg_id: str | None = None,
        cycle_id: str | None = None,
    ) -> FillOutcome: ...


class InMemoryGateway:
    """Offline execution: adverse slippage on the reference price, real charges.

    Deliberately minimal, and **not** a paper-fill model: this repository's
    :class:`~common.broker.paper.PaperBroker` is that, and Phase 4 owns widening
    it (bid/ask depth, latency, partial fills, the rejection matrix). This exists
    so the ported engine and the ported ``PositionManager`` tests can run with no
    database, which is what the reference's own tests did with a zero-charge
    ``PaperBroker``.
    """

    def __init__(
        self,
        *,
        slippage_points: float = 0.0,
        rates: CostRates | None = None,
    ) -> None:
        if slippage_points < 0:
            raise ValueError("slippage_points must be non-negative")
        self._slippage = slippage_points
        self._charges = ChargesCalculator(rates)

    def buy(
        self,
        contract: OptionContract,
        lots: int,
        *,
        ref_price: float,
        ts: datetime,
        stop_price: float | None = None,
        target_price: float | None = None,
        basket_id: str | None = None,
        leg_id: str | None = None,
        cycle_id: str | None = None,
    ) -> FillOutcome:
        # Adverse by construction: a buy fills above the reference price.
        # stop_price/target_price/basket_id/leg_id/cycle_id: nowhere to
        # persist them offline (no database, no order); accepted only to
        # satisfy ExecutionGateway. correlation_id stays None — there is no
        # persisted order to name one.
        return self._fill(Side.BUY, ref_price + self._slippage, lots * contract.lot_size)

    def sell(
        self,
        contract: OptionContract,
        lots: int,
        *,
        ref_price: float,
        ts: datetime,
        stop_price: float | None = None,
        target_price: float | None = None,
        basket_id: str | None = None,
        leg_id: str | None = None,
        cycle_id: str | None = None,
    ) -> FillOutcome:
        # Never below zero: a deep slippage setting must not mint a negative price.
        return self._fill(Side.SELL, max(ref_price - self._slippage, 0.0), lots * contract.lot_size)

    def _fill(self, side: Side, price: float, quantity: int) -> FillOutcome:
        breakdown = self._charges.for_leg(side=side, price=price, quantity=quantity)
        return FillOutcome(
            fill_price=price,
            charges=breakdown.total,
            charges_breakdown={
                "brokerage": breakdown.brokerage,
                "exchange_charges": breakdown.exchange_charges,
                "stt": breakdown.stt,
                "sebi_charges": breakdown.sebi_charges,
                "gst": breakdown.gst,
                "stamp_duty": breakdown.stamp_duty,
            },
        )


class PositionManager:
    """Owns the open position(s) and the day's closed trades."""

    def __init__(self, gateway: ExecutionGateway, *, lots: int = 1) -> None:
        self._gateway = gateway
        self._lots = lots
        self._positions: dict[str, OpenPosition] = {}
        self._entry_charges: dict[str, float] = {}
        self._entry_breakdown: dict[str, dict[str, float]] = {}
        # Market regime detected at entry, kept alongside the position until it
        # closes (same pattern as _entry_charges) so the closed Trade can carry it.
        self._entry_regime: dict[str, str | None] = {}
        self._entry_regime_features: dict[str, str] = {}
        self._trades: list[Trade] = []

    @property
    def lots(self) -> int:
        return self._lots

    @property
    def positions(self) -> list[OpenPosition]:
        return list(self._positions.values())

    def get(self, position_id: str) -> OpenPosition | None:
        return self._positions.get(position_id)

    def has_position(self, position_id: str | None = None) -> bool:
        if position_id is not None:
            return position_id in self._positions
        return bool(self._positions)

    @property
    def trades(self) -> list[Trade]:
        return list(self._trades)

    def open(
        self,
        contract: OptionContract,
        side: OrderSide,
        ref_price: float,
        ts: datetime,
        *,
        regime: str | None = None,
        regime_features: str = "{}",
        stop_price: float | None = None,
        target_price: float | None = None,
        basket_id: str | None = None,
        leg_id: str | None = None,
        cycle_id: str | None = None,
    ) -> OpenPosition:
        """Open a position: BUY to go long the option, SELL to write it.

        ``regime`` (optional) is the market regime detected at entry;
        ``regime_features`` its raw diagnostic values (JSON). Both are stashed and
        attached to the resulting :class:`Trade` on close.

        ``stop_price``/``target_price`` (Phase 6 Part 2, optional) are passed
        straight to the gateway for persistence — observability only, this class
        does not read them back for any decision.

        ``basket_id``/``leg_id`` (optional, multi-leg engines only) are passed
        straight to the gateway too, so the persisted ``order_intents`` row
        for this fill carries the same identity a caller's own basket/leg
        bookkeeping uses — the join key restart reconciliation needs. A
        single-leg caller that never passes them gets exactly today's
        behaviour: both stay ``None`` end to end.

        ``cycle_id`` (optional, the positional multi-leg engine only): a
        cross-day cycle's durable identity, passed straight to the gateway so
        the resulting position row is resolved through
        ``cycle_position_bindings`` rather than ``trading_date`` — see
        ``ExecutionRepository._upsert_position``. ``None`` for every other
        caller, unaffected.
        """
        position_id = contract.security_id
        if position_id in self._positions:
            raise RuntimeError(f"Cannot open: a position is already open for {contract.symbol}.")

        if side is OrderSide.BUY:
            result = self._gateway.buy(
                contract,
                self._lots,
                ref_price=ref_price,
                ts=ts,
                stop_price=stop_price,
                target_price=target_price,
                basket_id=basket_id,
                leg_id=leg_id,
                cycle_id=cycle_id,
            )
        else:
            result = self._gateway.sell(
                contract,
                self._lots,
                ref_price=ref_price,
                ts=ts,
                stop_price=stop_price,
                target_price=target_price,
                basket_id=basket_id,
                leg_id=leg_id,
                cycle_id=cycle_id,
            )

        position = OpenPosition(
            contract=contract,
            side=side,
            lots=self._lots,
            entry_price=result.fill_price,
            entry_time=ts,
        )
        position.entry_correlation_id = result.correlation_id
        self._positions[position_id] = position
        self._entry_charges[position_id] = result.charges
        self._entry_breakdown[position_id] = result.charges_breakdown or {}
        self._entry_regime[position_id] = regime
        self._entry_regime_features[position_id] = regime_features
        log.info(
            "OPEN %s %s @ %.2f x%d (qty %d)",
            side.value,
            contract.symbol,
            result.fill_price,
            self._lots,
            position.quantity,
        )
        return position

    def adopt(
        self,
        contract: OptionContract,
        side: OrderSide,
        lots: int,
        entry_price: float,
        entry_time: datetime,
        *,
        entry_charges: float = 0.0,
        last_price: float | None = None,
        max_favorable_pnl: float = 0.0,
        max_adverse_pnl: float = 0.0,
        entry_correlation_id: str | None = None,
    ) -> OpenPosition:
        """Take ownership of a position this process did not open.

        Phase 3 Part 2b-ii-B-2, for restart recovery. The position is already in the
        database — a previous process opened it and died before closing it — so
        :meth:`open` is exactly wrong here: it would call the gateway and place a
        **second** order, doubling the exposure the recovery exists to prevent. This
        seeds the same in-memory state ``open`` would have left behind, and touches
        no gateway at all.

        ``entry_charges`` comes from the persisted position's own ``charges``, so the
        resulting :class:`Trade` nets correctly on close rather than under-reporting
        the round trip's cost by one leg.

        ``max_favorable_pnl``/``max_adverse_pnl`` restore the excursion a previous
        process observed (Phase 6 Part 3 — ``positions.highest_favourable``/
        ``.lowest_favourable``, kept current by
        :class:`~common.engine.engine.TradingEngine`'s per-candle persistence, the
        same checkpoint Part 2's exit-state snapshot already uses). Set on the
        constructed position **before** any ``last_price`` mark below, so a restored
        baseline — not zero — is what the first excursion comparison runs against;
        this was the gap Part 1's original design left, at a time before Part 2 had
        established a checkpoint to keep them current from.
        """
        position_id = contract.security_id
        if position_id in self._positions:
            raise RuntimeError(f"Cannot adopt: a position is already open for {contract.symbol}.")

        position = OpenPosition(
            contract=contract,
            side=side,
            lots=lots,
            entry_price=entry_price,
            entry_time=entry_time,
        )
        position.max_favorable_pnl = max_favorable_pnl
        position.max_adverse_pnl = max_adverse_pnl
        position.entry_correlation_id = entry_correlation_id
        if last_price is not None:
            position.update_price(last_price)
        self._positions[position_id] = position
        self._entry_charges[position_id] = entry_charges
        self._entry_breakdown[position_id] = {}
        self._entry_regime[position_id] = None
        self._entry_regime_features[position_id] = "{}"
        log.info(
            "ADOPT %s %s @ %.2f x%d (qty %d) — recovered, no order placed",
            side.value,
            contract.symbol,
            entry_price,
            lots,
            position.quantity,
        )
        return position

    def close(
        self,
        position_id: str,
        ref_price: float,
        ts: datetime,
        reason: ExitReason,
        *,
        exit_regime: str | None = None,
        session_tags: str = "",
        basket_id: str | None = None,
        leg_id: str | None = None,
        cycle_id: str | None = None,
    ) -> Trade:
        """Close the position identified by ``position_id`` (inverse of entry).

        ``basket_id``/``leg_id`` (optional): the same identity the position
        was opened under — passed to the gateway so the closing order's own
        ``order_intents`` row carries it too, and copied onto the resulting
        :class:`Trade` alongside the entry correlation ID the position
        already carries, so a caller can find both orders for this exact
        round trip without approximating by ``(security_id, time)``.

        ``cycle_id`` (optional): see :meth:`open`'s own note — must match
        whatever ``cycle_id`` the position was opened under, so the closing
        fill resolves to the same cross-day row.
        """
        pos = self._positions.get(position_id)
        if pos is None:
            raise RuntimeError(f"Cannot close: no open position for {position_id}.")

        if pos.side is OrderSide.BUY:
            result = self._gateway.sell(
                pos.contract,
                pos.lots,
                ref_price=ref_price,
                ts=ts,
                basket_id=basket_id,
                leg_id=leg_id,
                cycle_id=cycle_id,
            )
            gross = (result.fill_price - pos.entry_price) * pos.quantity
        else:
            result = self._gateway.buy(
                pos.contract,
                pos.lots,
                ref_price=ref_price,
                ts=ts,
                basket_id=basket_id,
                leg_id=leg_id,
                cycle_id=cycle_id,
            )
            gross = (pos.entry_price - result.fill_price) * pos.quantity

        entry_charges = self._entry_charges.pop(position_id, 0.0)
        entry_breakdown = self._entry_breakdown.pop(position_id, {})
        entry_regime = self._entry_regime.pop(position_id, None)
        entry_regime_features = self._entry_regime_features.pop(position_id, "{}")
        exit_breakdown = result.charges_breakdown or {}
        total_charges = entry_charges + result.charges
        breakdown = self._merge_breakdowns(entry_breakdown, exit_breakdown)

        # The exit fill itself can be the new best/worst point (e.g. a market
        # order fills through the last streamed tick), so fold it in before
        # reading off the final MFE/MAE.
        pos.update_price(result.fill_price)

        trade = Trade(
            contract=pos.contract,
            side=pos.side,
            lots=pos.lots,
            quantity=pos.quantity,
            entry_price=pos.entry_price,
            exit_price=result.fill_price,
            entry_time=pos.entry_time,
            exit_time=ts,
            exit_reason=reason,
            gross_pnl=gross,
            charges=total_charges,
            charges_breakdown=breakdown,
            mfe=pos.max_favorable_pnl,
            mae=pos.max_adverse_pnl,
            entry_regime=entry_regime,
            exit_regime=exit_regime,
            session_tags=session_tags,
            entry_regime_features=entry_regime_features,
            entry_correlation_id=pos.entry_correlation_id,
            exit_correlation_id=result.correlation_id,
        )
        self._trades.append(trade)
        log.info(
            "CLOSE %s %s @ %.2f | reason=%s | gross %.2f charges %.2f net %.2f",
            pos.side.value,
            pos.contract.symbol,
            result.fill_price,
            reason.value,
            gross,
            total_charges,
            trade.net_pnl,
        )
        del self._positions[position_id]
        return trade

    def close_all(
        self, ref_price_for: dict[str, float], ts: datetime, reason: ExitReason
    ) -> list[Trade]:
        """Close every open position (e.g. for square-off/shutdown).

        ``ref_price_for`` maps ``position_id -> ref_price``; a missing entry falls
        back to the position's last known price.
        """
        closed = []
        for position_id in list(self._positions):
            pos = self._positions[position_id]
            ref_price = ref_price_for.get(position_id, pos.last_price)
            closed.append(self.close(position_id, ref_price, ts, reason))
        return closed

    @staticmethod
    def _merge_breakdowns(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
        keys = set(a) | set(b)
        return {k: round(a.get(k, 0.0) + b.get(k, 0.0), 4) for k in sorted(keys)}
