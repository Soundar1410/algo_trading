"""Multi-leg trading engine — orchestrates one multi-leg strategy process.

Sibling to :class:`~common.engine.engine.TradingEngine`, not a modification of it
— see that module's own docstring and ``common/engine/multi_leg_models.py``'s for
why. ``TradingEngine._current_position()`` hardcodes ``positions[0] if positions
else None`` and its tick routing, pending-entry tracking and signal vocabulary
are all built around exactly one concurrent leg; a strategy like ``straddle_920``
needs two independently-managed sold legs (CE+PE) sharing one logical basket and
one set of day-level risk formulas. Rather than widen the single-leg engine with
per-strategy branches — the one thing the spec and this repository's ``CLAUDE.md``
both forbid — this is a new, generic host for *any* multi-leg strategy.

Reused, unmodified, from the single-leg engine's own infrastructure (see each
module's docstring for why each one is already leg-count-agnostic):

* :class:`~common.engine.positions.PositionManager` — already a
  ``dict[str, OpenPosition]`` keyed by ``security_id``, with ``open``/``adopt``/
  ``close``/``close_all`` supporting any number of simultaneous positions.
* :class:`~common.engine.gateway.LifecycleGateway` — its ``buy``/``sell`` verbs
  carry no position-count assumption.
* :class:`~common.engine.hub_feed.HubTickFeed` — a queue drain with a
  ``subscribe``/``unsubscribe`` forwarding contract, indifferent to how many
  contracts a caller subscribes.
* :class:`~common.engine.square_off.SquareOffAuthority` /
  :class:`~common.engine.selection.OptionSelector` /
  :class:`~common.candles.builder.CandleBuilder` /
  :class:`~common.engine.reporting_bindings.HeartbeatEngineReporter` /
  ``RepositoryReportWriter`` — all already operate on ``positions.positions``/
  ``.trades`` (plain lists) or pure clock/strike arithmetic.

What genuinely needs to be new (and is, entirely in this module and
``multi_leg_models.py``): tick routing across many open ``security_id``\\ s at
once, N-way pending-fill tracking (so two independent legs can each be
independently pending — spec section 9.6's partial-execution tolerance), and
basket-level restart reconciliation. There is no ``_current_position()``
equivalent anywhere in this file.

VIX / underlying routing (spec section 6, and the ``straddle_920`` port's own
correction record): the underlying and India VIX share one dynamic-instrument
type (:class:`~common.market_data.instruments.MarketDataInstrument`) but are
routed differently on every tick — only the *underlying*'s ticks build the
5-minute candle a strategy's ``on_candle`` fires from; a VIX tick only ever
updates ``self._last_vix_price``, handed to the strategy as a plain float
alongside the next candle. A VIX tick is never mistaken for an underlying one.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from common.candles.builder import CandleBuilder, to_ohlc
from common.logging import get_logger
from common.models import ExitReason, OptionType, Tick
from common.notifications import NotificationEvent, Notifier, NullNotifier, SafeNotifier
from common.utils.timeutils import now_ist, parse_timeframe_minutes

from .config import EngineConfig
from .daily_guard import DailyRiskConfig, DailyRiskGuard
from .feed import MarketDataFeed
from .multi_leg_models import (
    Basket,
    BasketAction,
    BasketSignal,
    LegInstance,
    LegRole,
    LegState,
    UnmanageableBasketState,
)
from .multi_leg_strategy import BaseMultiLegStrategy
from .positions import PositionManager
from .reporting import EngineReporter, NullReporter, NullReportWriter, ReportWriter, summarise
from .risk import opt_float
from .selection import OptionSelector
from .session import MarketSession
from .square_off import SessionSquareOffAuthority, SquareOffAuthority

log = get_logger(__name__)

#: LegRole -> OptionType, for the CE/PE roles every straddle-shaped strategy
#: uses. LegRole.GENERIC has no default mapping — a future non-CE/PE multi-leg
#: strategy's LegIntent must carry an explicit option_type some other way, or
#: this engine's contract resolution will refuse it (see ``_resolve_contract``).
_ROLE_TO_OPTION_TYPE = {LegRole.CE: OptionType.CE, LegRole.PE: OptionType.PE}


class MultiLegEngine:
    def __init__(
        self,
        cfg: EngineConfig,
        *,
        feed: MarketDataFeed,
        option_selector: OptionSelector,
        strategy: BaseMultiLegStrategy,
        position_manager: PositionManager,
        underlying_security_id: str,
        underlying_instrument: str = "",
        vix_security_id: str | None = None,
        runtime_id: str = "multi_leg_engine",
        notifier: Notifier | None = None,
        notify_deferred: bool = True,
        persist_notification_failure: Callable[[NotificationEvent, str], None] | None = None,
        reporter: EngineReporter | None = None,
        report: ReportWriter | None = None,
        square_off_event: threading.Event | None = None,
        square_off_authority: SquareOffAuthority | None = None,
        recover_basket: Callable[[], Basket | None] | None = None,
        persist_basket: Callable[[Basket], None] | None = None,
        persist_leg: Callable[[LegInstance], None] | None = None,
        clock: Callable[[], datetime] = now_ist,
        trading_date: str = "",
    ) -> None:
        self.cfg = cfg
        self.feed = feed
        self.selector = option_selector
        self.strategy = strategy
        self.positions = position_manager
        self.report: ReportWriter = report or NullReportWriter()
        self.notifier = (
            notifier
            if isinstance(notifier, SafeNotifier)
            else SafeNotifier(
                notifier or NullNotifier(),
                deferred=notify_deferred,
                on_failure=persist_notification_failure,
            )
        )
        self._runtime_id = runtime_id
        self._reporter: EngineReporter = reporter or NullReporter()
        self.underlying_id = underlying_security_id
        self.underlying_instrument = underlying_instrument or underlying_security_id
        self.vix_id = vix_security_id
        self._last_vix_price: float | None = None

        self.session = MarketSession(cfg.session)
        self._square_off: SquareOffAuthority = square_off_authority or SessionSquareOffAuthority(
            self.session
        )
        self._recover_basket = recover_basket
        self._persist_basket_cb = persist_basket
        self._persist_leg_cb = persist_leg
        self._trading_date = trading_date

        interval = parse_timeframe_minutes(cfg.timeframe)
        self.candles = CandleBuilder(
            interval,
            security_id=self.underlying_id,
            instrument=self.underlying_instrument,
        )
        self._lots = position_manager.lots
        self.label = strategy.name

        self._spot: float | None = None
        self._squared_off = False
        #: security_id -> leg_id, for legs awaiting their first fresh tick.
        self._pending_by_security: dict[str, str] = {}

        self._square_off_requested = square_off_event or threading.Event()
        self._stopped = threading.Event()
        self._shutdown_reason = "external request"
        self._stopped_by_request = False
        self._now = clock
        self._entry_blocked: str | None = None

        self._basket_id = f"{strategy.name}:{trading_date}" if trading_date else f"{strategy.name}"
        self._basket = Basket(
            basket_id=self._basket_id,
            strategy_id=strategy.name,
            execution_mode=cfg.execution_mode,
            trading_date=trading_date,
        )

        # Optional, generic, engine-level daily guard for a *future* multi-leg
        # strategy that wants one. straddle_920's own gross-P&L daily-loss/
        # combined-stop/profit-target formulas live entirely in the strategy
        # (BaseMultiLegStrategy.on_leg_tick) and are the sole decider for that
        # strategy — this stays disabled for it (max_daily_loss_percent unset
        # in its config) so there is never a second, disagreeing daily-loss
        # decider. See the straddle_920 port's own architecture notes.
        self._daily_guard = self._build_daily_guard(cfg)

    @staticmethod
    def _build_daily_guard(cfg: EngineConfig) -> DailyRiskGuard | None:
        pct = opt_float(cfg.max_daily_loss_percent)
        if pct is None or pct <= 0:
            return None
        capital = float(cfg.starting_capital)
        return DailyRiskGuard(DailyRiskConfig(daily_max_loss=pct / 100.0 * capital))

    # -------------------------------------------------------------- shutdown
    def request_square_off(self, reason: str = "external request") -> None:
        """Ask the engine to square off and stop at its next boundary. Safe from
        any thread — see ``TradingEngine.request_square_off`` for the identical
        ownership rule this mirrors exactly."""
        self._shutdown_reason = reason
        self._square_off_requested.set()

    @property
    def square_off_requested(self) -> bool:
        return self._square_off_requested.is_set()

    @property
    def stopped_by_request(self) -> bool:
        return self._stopped_by_request

    def wait_until_stopped(self, timeout: float | None = None) -> bool:
        return self._stopped.wait(timeout)

    @property
    def entries_blocked(self) -> str | None:
        return self._entry_blocked or self._basket.day_blocked_reason

    def block_entries(self, reason: str) -> None:
        self._block_entries(reason)

    def _block_entries(self, reason: str) -> None:
        if self._entry_blocked is not None:
            return
        self._entry_blocked = reason
        msg = f"BLOCKED: {reason} — no new entries today"
        log.error("%s %s", self.label, msg)
        self._notify("entries_blocked", f"{self.label} {msg}")

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        self._stopped.clear()
        self._start_day()
        self.feed.on_tick(self.on_tick)
        self._reporter.start()
        log.info(
            "multi-leg engine running mode=%s strategy=%s timeframe=%s",
            self.cfg.execution_mode.value,
            self.strategy.name,
            self.cfg.timeframe,
        )
        try:
            self.feed.run()
        except (KeyboardInterrupt, SystemExit):
            log.warning("interrupted; forcing square-off before shutdown")
            self._handle_square_off(self._now())
            raise
        except Exception as exc:
            self._notify("engine_error", str(exc))
            log.exception("unhandled error; forcing square-off before shutdown")
            self._handle_square_off(self._now())
            raise
        finally:
            if self._square_off_requested.is_set():
                self._stopped_by_request = True
                self._handle_square_off(self._now())
            self._end_day()
            self._reporter.stopped(self.positions.positions, self.positions.trades)
            self._stopped.set()

    def _start_day(self) -> None:
        self.strategy.reset()
        self.candles.reset()
        self._spot = None
        self._last_vix_price = None
        self._squared_off = False
        self._entry_blocked = None
        self._pending_by_security = {}
        if self._daily_guard is not None:
            self._daily_guard.reset()
        self._basket = Basket(
            basket_id=self._basket_id,
            strategy_id=self.strategy.name,
            execution_mode=self.cfg.execution_mode,
            trading_date=self._trading_date,
        )
        self.feed.subscribe(self.underlying_id)
        if self.vix_id:
            self.feed.subscribe(self.vix_id)
        self._adopt_recovered_basket()
        log.info("new multi-leg trading day initialised (fresh state)")

    def _adopt_recovered_basket(self) -> None:
        """Take over a basket a previous process left open, placing no order.

        Mirrors ``TradingEngine._adopt_recovered_position`` exactly: a provider
        that raises :class:`~common.engine.multi_leg_models.
        UnmanageableBasketState` propagates (aborting the worker — recovery
        found exposure it cannot prove); any other raise blocks new entries for
        the day but leaves already-open legs alone; ``None`` means nothing to
        adopt.
        """
        if self._recover_basket is None:
            return
        try:
            recovered = self._recover_basket()
        except UnmanageableBasketState:
            raise
        except Exception as exc:
            log.exception("%s: could not establish whether a basket was carried over", self.label)
            self._block_entries(
                f"restart recovery failed ({exc}) — unable to establish whether a basket "
                "from a previous run is still open"
            )
            return
        if recovered is None:
            return

        self._basket = recovered
        for leg in recovered.legs.values():
            leg_is_open = (
                leg.state is LegState.OPEN
                and leg.contract is not None
                and leg.entry_price is not None
            )
            if leg_is_open:
                assert leg.contract is not None
                assert leg.entry_price is not None
                lots = leg.quantity // leg.contract.lot_size if leg.contract.lot_size else 0
                self.positions.adopt(
                    leg.contract,
                    leg.side,
                    lots,
                    leg.entry_price,
                    leg.entry_time or self._now(),
                    entry_charges=0.0,
                    last_price=leg.last_price,
                    max_favorable_pnl=leg.max_favorable_pnl,
                    max_adverse_pnl=leg.max_adverse_pnl,
                )
                self.feed.subscribe(leg.contract.security_id)
            elif leg.is_pending and leg.contract is not None:
                self.feed.subscribe(leg.contract.security_id)
                self._pending_by_security[leg.contract.security_id] = leg.leg_id
        log.info(
            "adopted basket %s: %d open leg(s), %d pending leg(s) from a previous run",
            recovered.basket_id,
            len(recovered.open_legs()),
            len(recovered.pending_legs()),
        )
        self._notify(
            "restart_adoption",
            f"adopted basket {recovered.basket_id}: {len(recovered.open_legs())} open, "
            f"{len(recovered.pending_legs())} pending leg(s)",
        )

    def _end_day(self) -> None:
        summary_trades = self.positions.trades
        self.report.generate(summary_trades)
        if summary_trades:
            summary = summarise(summary_trades)
            self._notify(
                "eod_summary",
                f"{summary.trade_count} trade(s), net {summary.net_pnl:.2f}, "
                f"win rate {summary.win_rate:.1f}%",
            )

    # ----------------------------------------------------------- tick routing
    def on_tick(self, tick: Tick) -> None:
        if self._square_off_requested.is_set():
            self._shutdown(tick.exchange_time)
            return
        if self._square_off.due(tick.exchange_time):
            self._handle_square_off(tick.exchange_time)
            return

        if tick.security_id == self.underlying_id:
            self._on_underlying_tick(tick)
        elif self.vix_id is not None and tick.security_id == self.vix_id:
            # VIX is market-data only: it updates the strategy's next-candle
            # input and nothing else. It is never fed to the candle builder,
            # so a VIX tick structurally cannot become a "NIFTY candle".
            self._last_vix_price = tick.last_price
        else:
            self._on_leg_tick(tick)

        self._reporter.beat(self.positions.positions, self.positions.trades)

    def _on_underlying_tick(self, tick: Tick) -> None:
        self._spot = tick.last_price
        if not self.session.is_open(tick.exchange_time):
            return
        completed = self.candles.add(tick.last_price, tick.exchange_time)
        if completed is None:
            return
        bar = to_ohlc(completed)
        if bar.spans_gap:
            log.warning(
                "[CANDLE_GAP] %s bar %s-%s was built across a hole in the tick stream; "
                "skipping any signal for it.",
                self.label,
                completed.start_at.isoformat(),
                completed.end_at.isoformat(),
            )
            return
        self._on_candle_close(bar, tick.exchange_time)

    def _on_candle_close(self, candle: Any, ts: datetime) -> None:
        strat_signal = self.strategy.on_candle(
            candle, ts, basket=self._basket, vix=self._last_vix_price
        )
        # Correction: the primary attempt must be durably consumed *before*
        # filter evaluation can create retry ambiguity (spec section 9.2), and
        # more generally any basket-level bookkeeping a strategy touches
        # directly (entries_consumed, day_blocked_reason, an expired pending
        # replacement) must be durable before the engine acts on whatever
        # signal (or None) came back. Persisting unconditionally here, before
        # ``_apply_signal`` does anything with an external side effect
        # (contract resolution, subscription, order submission), is what
        # gives that guarantee — cheap, since this runs once per closed
        # underlying candle, not per tick.
        self._persist_basket()

        log.info(
            "%s bar evaluated_at=%s | %s",
            self.label,
            ts.strftime("%H:%M:%S"),
            self.strategy.status(),
        )

        if strat_signal is None or strat_signal.action is BasketAction.NONE:
            return
        self._apply_signal(strat_signal, ts)

    def _on_leg_tick(self, tick: Tick) -> None:
        leg_id = self._pending_by_security.get(tick.security_id)
        if leg_id is not None:
            self._try_fill_pending(leg_id, tick)
            return

        leg = self._open_leg_for_security(tick.security_id)
        if leg is None:
            return
        leg.update_price(tick.last_price)
        if self._daily_guard is not None:
            self._daily_guard.check_open_mtm(self._basket.unrealised_gross_pnl())
        strat_signal = self.strategy.on_leg_tick(leg, tick, self._basket)
        if strat_signal is None or strat_signal.action is BasketAction.NONE:
            return
        self._apply_signal(strat_signal, tick.exchange_time)

    def _open_leg_for_security(self, security_id: str) -> LegInstance | None:
        for leg in self._basket.legs.values():
            if (
                leg.state is LegState.OPEN
                and leg.contract is not None
                and leg.contract.security_id == security_id
            ):
                return leg
        return None

    def _try_fill_pending(self, leg_id: str, tick: Tick) -> None:
        leg = self._basket.legs.get(leg_id)
        if leg is None or leg.state is not LegState.PENDING_ORDER or leg.contract is None:
            return
        if not self.session.can_enter(tick.exchange_time):
            return
        self._open_leg(leg, tick.last_price, tick.exchange_time)

    # ----------------------------------------------------------- signal apply
    def _apply_signal(self, signal: BasketSignal, ts: datetime) -> None:
        if signal.action is BasketAction.ENTER_BASKET or signal.action is BasketAction.ENTER_LEG:
            self._enter_legs(signal, ts)
        elif signal.action is BasketAction.EXIT_LEG:
            self._exit_leg_signal(signal, ts)
        elif signal.action is BasketAction.EXIT_ALL:
            self._exit_all_signal(signal, ts)

    def _enter_legs(self, signal: BasketSignal, ts: datetime) -> None:
        if self._entry_blocked is not None or self._basket.day_blocked_reason is not None:
            log.info("%s: entry/replacement suppressed — day is blocked", self.label)
            return
        if self._spot is None:
            log.warning("%s: no spot price yet; cannot select option contract(s)", self.label)
            return
        for intent in signal.legs:
            option_type = _ROLE_TO_OPTION_TYPE.get(intent.role)
            if option_type is None:
                log.error(
                    "%s: leg role %s has no default option-type mapping; skipping",
                    self.label,
                    intent.role,
                )
                continue
            leg_id = self._basket.next_leg_id(intent.role)
            leg = LegInstance(
                leg_id=leg_id,
                basket_id=self._basket.basket_id,
                role=intent.role,
                sequence=self._basket.next_sequence(intent.role),
                is_replacement=intent.is_replacement,
                side=intent.side,
                replaces_leg_id=intent.replaces_leg_id,
                state=LegState.PENDING_CONTRACT,
            )
            self._basket.legs[leg_id] = leg
            try:
                contract = self.selector.select(
                    self._spot,
                    option_type,
                    intent.option_selection.moneyness,
                    intent.option_selection.steps,
                )
            except Exception as exc:
                log.error(
                    "%s: could not resolve a contract for %s: %s", self.label, intent.role, exc
                )
                leg.state = LegState.FAILED
                self._persist_leg(leg)
                continue
            leg.contract = contract
            leg.state = LegState.PENDING_ORDER
            self._persist_leg(leg)
            self.feed.subscribe(contract.security_id)
            self._pending_by_security[contract.security_id] = leg_id
            log.info(
                "%s: pending %s entry queued: %s %s (awaiting first fresh tick)",
                self.label,
                "replacement" if intent.is_replacement else "leg",
                intent.side.value,
                contract.symbol,
            )
        if signal.action is BasketAction.ENTER_LEG:
            # The one-shot replacement attempt has now been made (queued, or
            # every intent failed to resolve above) — spec section 12.3 step
            # 5: resolved once, on this candle, never retried on a later one.
            self._basket.pending_replacement_role = None
            self._basket.pending_replacement_state = None
            self._persist_basket()

    def _open_leg(self, leg: LegInstance, price: float, ts: datetime) -> None:
        assert leg.contract is not None
        self.positions.open(leg.contract, leg.side, price, ts)
        position = self.positions.get(leg.contract.security_id)
        assert position is not None
        leg.state = LegState.OPEN
        leg.entry_price = position.entry_price
        leg.quantity = position.quantity
        leg.entry_time = ts
        leg.last_price = position.entry_price
        self._pending_by_security.pop(leg.contract.security_id, None)
        self._persist_leg(leg)
        self._maybe_capture_original_basis()
        self._notify("fill", f"{leg.side.value} {leg.contract.symbol} @ {price:.2f}")

    def _maybe_capture_original_basis(self) -> None:
        """Spec section 13.4: B_original is captured once, at the moment the
        *original* (non-replacement, sequence 1) two-leg basket both fill —
        and never rebased afterwards."""
        if self._basket.original_combined_basis is not None:
            return
        originals = [
            leg
            for leg in self._basket.legs.values()
            if leg.sequence == 1 and not leg.is_replacement and leg.role in _ROLE_TO_OPTION_TYPE
        ]
        if len(originals) < 2 or any(leg.state is not LegState.OPEN for leg in originals):
            return
        self._basket.original_combined_basis = sum(leg.entry_price or 0.0 for leg in originals)
        self._persist_basket()

    def _exit_leg_signal(self, signal: BasketSignal, ts: datetime) -> None:
        if signal.target_leg_id is None:
            log.error("%s: EXIT_LEG signal carries no target_leg_id; ignoring", self.label)
            return
        leg = self._basket.legs.get(signal.target_leg_id)
        if leg is None or leg.state is not LegState.OPEN or leg.contract is None:
            return
        reason = signal.exit_reason or ExitReason.STRATEGY_EXIT
        if reason is ExitReason.ADJUSTMENT:
            # Correction: the sole permitted adjustment is durably claimed
            # *before* submitting the adjustment exit (spec section 12.3
            # steps 1-2) — never after, so a crash between the two cannot
            # leave a second adjustment possible on restart.
            self._basket.adjustment_count += 1
            self._basket.pending_replacement_role = leg.role
            self._basket.pending_replacement_state = "AWAITING_NEXT_CANDLE"
            self._persist_basket()
        self._close_leg(leg, leg.last_price if leg.last_price is not None else 0.0, ts, reason)

    def _exit_all_signal(self, signal: BasketSignal, ts: datetime) -> None:
        reason = signal.exit_reason or ExitReason.STRATEGY_EXIT
        self._basket.day_blocked_reason = signal.reason or reason.value
        self._persist_basket()
        self._close_all(ts, reason)

    def _close_leg(self, leg: LegInstance, price: float, ts: datetime, reason: ExitReason) -> None:
        if leg.contract is None:
            return
        trade = self.positions.close(leg.contract.security_id, price, ts, reason)
        leg.state = LegState.CLOSED
        leg.exit_price = trade.exit_price
        leg.exit_time = ts
        leg.exit_reason = reason
        leg.realized_gross_pnl = trade.gross_pnl
        self._persist_leg(leg)
        if self._daily_guard is not None:
            self._daily_guard.register_trade(trade.net_pnl)
        self.feed.unsubscribe(leg.contract.security_id)
        self._notify(
            "exit",
            f"{trade.side.value} {trade.contract.symbol} @ {trade.exit_price:.2f} "
            f"reason={reason.value} gross={trade.gross_pnl:.2f}",
        )
        self.strategy.on_leg_closed(trade, leg, self._basket)

    def _close_all(self, ts: datetime, reason: ExitReason) -> None:
        for leg in list(self._basket.open_legs()):
            price = leg.last_price if leg.last_price is not None else 0.0
            self._close_leg(leg, price, ts, reason)
        for leg in list(self._basket.pending_legs()):
            self._terminate_pending_leg(leg, LegState.EXPIRED)

    def _terminate_pending_leg(self, leg: LegInstance, terminal_state: LegState) -> None:
        leg.state = terminal_state
        if leg.contract is not None:
            self._pending_by_security.pop(leg.contract.security_id, None)
        self._persist_leg(leg)

    # -------------------------------------------------------------- shutdown
    def _shutdown(self, ts: datetime) -> None:
        if not self._stopped_by_request:
            self._stopped_by_request = True
            log.warning(
                "square-off requested (%s); closing every leg and stopping the feed",
                self._shutdown_reason,
            )
        already_squared_off = self._squared_off
        self._handle_square_off(ts)
        if already_squared_off:
            self.feed.stop()

    def _handle_square_off(self, ts: datetime) -> None:
        if self._squared_off:
            return
        self._basket.square_off_state = "IN_PROGRESS"
        self._basket.day_blocked_reason = self._basket.day_blocked_reason or "hard square-off"
        self._persist_basket()
        self._close_all(ts, ExitReason.SQUARE_OFF)
        self._squared_off = True
        self._basket.square_off_state = "COMPLETED"
        self._basket.lifecycle_state = "CLOSED"
        self._persist_basket()
        self._square_off.completed(ts)
        log.info("%s: trading day complete; stopping feed", self.label)
        self._notify("square_off_completed", f"{self.label} squared off at {ts.isoformat()}")
        self.feed.stop()

    # ------------------------------------------------------------ persistence
    def _persist_basket(self) -> None:
        if self._persist_basket_cb is not None:
            try:
                self._persist_basket_cb(self._basket)
            except Exception:
                log.exception("%s: could not persist basket state", self.label)

    def _persist_leg(self, leg: LegInstance) -> None:
        if self._persist_leg_cb is not None:
            try:
                self._persist_leg_cb(leg)
            except Exception:
                log.exception("%s: could not persist leg %s", self.label, leg.leg_id)

    def _notify(self, event_type: str, message: str) -> None:
        self.notifier.send(
            NotificationEvent(
                event_type=event_type,
                message=message,
                runtime_id=self._runtime_id,
                strategy_id=self.strategy.name,
                execution_mode=self.cfg.execution_mode,
            )
        )
