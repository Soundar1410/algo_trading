"""``straddle_920`` — the 9:20 Morning Straddle, ported exactly from the legacy
``Trading_Automation`` implementation onto the generic
:class:`~common.engine.multi_leg_engine.MultiLegEngine`.

See ``MORNING_STRADDLE_ALGO_TRADING_SPEC.md`` in this directory for the
authoritative rule set this class implements; section references in the
comments below point at that document.

State ownership (spec section 8/14.3)
--------------------------------------
This class holds **no private durable state** beyond its own (immutable, once
constructed) configuration. Every fact a decision needs — whether the day's
attempt is consumed, the adjustment count, which replacement is pending, the
original opening basis, which legs are currently open — is already on the
:class:`~common.engine.multi_leg_models.Basket` the engine hands to every
call, which is itself backed by the durable ``strategy_baskets``/
``strategy_legs`` tables (migration ``0009``). This class mutates a small set
of simple, non-order-affecting ``Basket`` fields directly (``entries_consumed``,
``day_blocked_reason``, ``pending_replacement_state``) — the engine persists
the whole basket immediately after every :meth:`on_candle` call, before acting
on any returned signal, so a crash between "the strategy decided" and "an
order was placed" can never leave the day's one attempt ambiguous (spec
section 9.2, section 12.1's "durably consume before performing irreversible
effects"). Every *order-affecting* decision (open a leg, close a leg, close
everything) is expressed as a :class:`~common.engine.multi_leg_models.
BasketSignal` instead — the engine is the only thing that ever resolves a
contract, subscribes, or submits an order.

This class never constructs a Dhan SDK client and never touches SQLite
directly, matching every other strategy in this repository.
"""

from __future__ import annotations

from datetime import time as time_
from typing import Any

from common.engine.multi_leg_models import (
    Basket,
    BasketAction,
    BasketSignal,
    LegInstance,
    LegIntent,
    LegRole,
    LegState,
)
from common.engine.multi_leg_strategy import BaseMultiLegStrategy, register_multi_leg_strategy
from common.indicators.base import OHLC
from common.models import ExitReason, OrderSide, Tick
from common.utils.timeutils import parse_hhmm


def _pick(cfg: Any, kwargs: dict[str, Any], key: str, default: Any) -> Any:
    """Read ``key`` from explicit kwargs first, then ``cfg.parameters``, else default.

    Mirrors ``EmaCross9x21BuyStrategy._pick`` exactly, so this strategy supports
    both the direct-kwargs construction path
    (``runtimes.intraday_options.multi_leg_engine_worker``'s ``strategy_kwargs``)
    and a future registry-style ``cfg``-only construction, without duplicating
    the read logic per key.
    """
    if key in kwargs:
        return kwargs[key]
    params = dict(getattr(cfg, "parameters", {}) or {})
    return params.get(key, default)


@register_multi_leg_strategy("straddle_920")
class Straddle920Strategy(BaseMultiLegStrategy):
    """Short ATM NIFTY CE + PE at 09:20, one leg-doubling adjustment, exact
    legacy risk formulas (spec sections 9-13)."""

    def __init__(self, cfg: Any = None, **kwargs: Any) -> None:
        super().__init__(cfg)
        self._lots_per_leg = int(_pick(cfg, kwargs, "lots_per_leg", 10))
        self._entry_evaluation_time: time_ = parse_hhmm(
            str(_pick(cfg, kwargs, "entry_evaluation_time", "09:20"))
        )
        self._last_entry_time: time_ = parse_hhmm(
            str(_pick(cfg, kwargs, "last_entry_time", "15:00"))
        )
        self._vix_threshold = float(_pick(cfg, kwargs, "vix_threshold", 20.0))
        self._leg_adjustment_multiplier = float(
            _pick(cfg, kwargs, "leg_adjustment_multiplier", 2.0)
        )
        self._max_adjustments_per_day = int(_pick(cfg, kwargs, "max_adjustments_per_day", 1))
        # Absolute rupee daily-loss threshold — spec section 13.2:
        # 3% x capital_base = Rs 60,000 for the specified Rs 20,00,000 base.
        # Passed through as the already-computed absolute amount (matching
        # config_adapter.py's own "pass the two config numbers through
        # unmultiplied" convention elsewhere) rather than recomputed here from
        # a percentage, so there is exactly one place capital_base x
        # daily_loss_pct is multiplied together.
        self._daily_loss_amount = float(_pick(cfg, kwargs, "daily_loss_amount", 60_000.0))
        self._combined_stop_percentage = float(
            _pick(cfg, kwargs, "combined_stop_percentage", 0.30)
        )
        self._profit_target_percentage = float(
            _pick(cfg, kwargs, "profit_target_percentage", 0.50)
        )
        self._blackout_dates: frozenset[str] = frozenset(
            str(d) for d in _pick(cfg, kwargs, "blackout_dates", ())
        )

    # ------------------------------------------------------------- lifecycle
    def reset(self) -> None:
        """No-op: every fact this strategy needs is already on ``Basket``,
        which the engine rebuilds fresh at the start of each day."""
        return None

    def status(self) -> str:
        return (
            f"lots={self._lots_per_leg} vix<= {self._vix_threshold:g} "
            f"adjustments<= {self._max_adjustments_per_day}"
        )

    @property
    def quantity_lots(self) -> int:
        return self._lots_per_leg

    # ------------------------------------------------------------- entry/day
    def on_candle(
        self,
        candle: OHLC,
        timestamp: Any,
        *,
        basket: Basket,
        vix: float | None,
    ) -> BasketSignal | None:
        t = timestamp.time()

        # --- Replacement handling (spec section 12.3 steps 5-7) -----------
        # Independent of the primary-entry gate below: this fires on the
        # very next completed candle after an adjustment, whatever time of
        # day that candle lands at, as long as it is still within the same
        # cutoff a fresh entry would need.
        if basket.pending_replacement_role is not None:
            return self._replacement_signal(basket, timestamp, t)

        # --- Primary entry (spec sections 9.1-9.5) -------------------------
        if basket.entries_consumed:
            return None
        if t < self._entry_evaluation_time:
            return None

        # This is the entry-evaluation candle. Consume the day's one attempt
        # *before* evaluating the VIX/news filters below (spec section 9.2) —
        # whatever the filters decide, there is no second attempt today.
        basket.entries_consumed = True

        if basket.trading_date in self._blackout_dates:
            basket.day_blocked_reason = "news blackout"
            return None

        # Spec section 9.3: strictly greater than the threshold skips the
        # day; exactly the threshold, below it, or an unavailable reading
        # (None) all permit entry (fail-open on missing VIX).
        if vix is not None and vix > self._vix_threshold:
            basket.day_blocked_reason = f"India VIX {vix:.2f} > {self._vix_threshold:g}"
            return None

        return BasketSignal(
            action=BasketAction.ENTER_BASKET,
            timestamp=timestamp,
            legs=(
                LegIntent(role=LegRole.CE, side=OrderSide.SELL),
                LegIntent(role=LegRole.PE, side=OrderSide.SELL),
            ),
            reason="09:20 primary entry: sell ATM CE and PE",
        )

    def _replacement_signal(
        self, basket: Basket, timestamp: Any, t: time_
    ) -> BasketSignal | None:
        role = basket.pending_replacement_role
        assert role is not None
        if t > self._last_entry_time:
            # Spec section 12.4: the cutoff was reached before a replacement
            # could be attempted. Recorded as expired — never reopen the
            # closed adjusted leg using stale data, never auto-close the
            # surviving leg; normal risk management stays active for it.
            basket.pending_replacement_state = "EXPIRED"
            basket.pending_replacement_role = None
            return None

        # The one-shot attempt, on this exact candle (spec 12.3 step 5): the
        # then-current ATM contract of the same option type as the leg that
        # doubled. replaces_leg_id links to the most recently closed instance
        # of this role, purely for traceability.
        closed_of_role = [
            leg
            for leg in basket.legs.values()
            if leg.role is role and leg.state is LegState.CLOSED
        ]
        replaces_leg_id = (
            max(closed_of_role, key=lambda leg: leg.sequence).leg_id if closed_of_role else None
        )
        return BasketSignal(
            action=BasketAction.ENTER_LEG,
            timestamp=timestamp,
            legs=(
                LegIntent(
                    role=role,
                    side=OrderSide.SELL,
                    is_replacement=True,
                    replaces_leg_id=replaces_leg_id,
                ),
            ),
            reason=f"adjustment replacement for {role.value}",
        )

    # ------------------------------------------------------------- leg tick
    def on_leg_tick(self, leg: LegInstance, tick: Tick, basket: Basket) -> BasketSignal | None:
        # Step 1 (hard square-off) is entirely engine-owned, evaluated before
        # this is ever reached — see MultiLegEngine.on_tick.

        # Step 2: single-tick leg-doubling adjustment (spec section 12.1).
        if (
            basket.adjustment_count < self._max_adjustments_per_day
            and leg.entry_price is not None
            and leg.entry_price > 0
            and tick.last_price >= self._leg_adjustment_multiplier * leg.entry_price
        ):
            return BasketSignal(
                action=BasketAction.EXIT_LEG,
                timestamp=tick.exchange_time,
                exit_reason=ExitReason.ADJUSTMENT,
                target_leg_id=leg.leg_id,
                reason=(
                    f"{leg.role.value} doubled: {tick.last_price:.2f} >= "
                    f"{self._leg_adjustment_multiplier:g}x{leg.entry_price:.2f}"
                ),
            )

        if basket.day_blocked_reason is not None:
            return None

        realised = basket.realised_gross_pnl()
        unrealised = basket.unrealised_gross_pnl()
        total = realised + unrealised

        # Step 3: daily maximum loss (spec section 13.2). Gross R + U.
        if total <= -self._daily_loss_amount:
            return BasketSignal(
                action=BasketAction.EXIT_ALL,
                timestamp=tick.exchange_time,
                exit_reason=ExitReason.DAILY_LOSS_LIMIT,
                reason=f"daily max loss: {total:.2f} <= -{self._daily_loss_amount:.2f}",
            )

        # Step 4: combined open-position stop (spec section 13.3). Current
        # open-leg basis only, open unrealised P&L only — rebases
        # automatically on replacement because current_open_basis() reads
        # the live open-leg set.
        open_basis = basket.current_open_basis()
        if open_basis > 0:
            combined_stop_amount = self._combined_stop_percentage * open_basis
            if unrealised <= -combined_stop_amount:
                return BasketSignal(
                    action=BasketAction.EXIT_ALL,
                    timestamp=tick.exchange_time,
                    exit_reason=ExitReason.STOP_LOSS,
                    reason=(
                        f"combined stop: U={unrealised:.2f} <= "
                        f"-{combined_stop_amount:.2f}"
                    ),
                )

        # Step 5: profit target (spec section 13.4). Original two-leg
        # opening basis, never rebased by a later replacement.
        if basket.original_combined_basis is not None:
            target_amount = (
                self._profit_target_percentage * basket.original_combined_basis * leg.quantity
            )
            if total >= target_amount:
                return BasketSignal(
                    action=BasketAction.EXIT_ALL,
                    timestamp=tick.exchange_time,
                    exit_reason=ExitReason.TARGET_PROFIT,
                    reason=f"profit target: T={total:.2f} >= {target_amount:.2f}",
                )
        return None
