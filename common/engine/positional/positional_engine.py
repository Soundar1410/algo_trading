"""Positional multi-leg engine — orchestrates one positional multi-leg
strategy process across trading sessions.

Sibling to :class:`~common.engine.multi_leg_engine.MultiLegEngine`, **never a
modification of it** (spec review requirement, and the same discipline that
module's own docstring holds itself to against
:class:`~common.engine.engine.TradingEngine`). ``MultiLegEngine`` is
structurally intraday: ``_start_day()`` rebuilds a fresh
``Basket(trading_date=...)`` on every start, ``run()``'s ``finally`` forces
``_handle_square_off`` whenever a stop was requested, and
``_handle_square_off`` marks ``lifecycle_state = "CLOSED"`` on every stop —
all three are wrong for a position meant to survive across trading days.

Reused, unmodified, exactly as ``MultiLegEngine`` reuses them (see that
module's own docstring for why each is already leg-count/session-agnostic):

* :class:`~common.engine.positions.PositionManager` — its ``open``/``close``
  gained an *additive*, optional ``cycle_id`` parameter (this branch) so a
  fill resolves through ``cycle_position_bindings`` instead of
  ``trading_date`` — every other caller is unaffected.
* :class:`~common.engine.gateway.LifecycleGateway` — same additive
  ``cycle_id`` parameter on ``buy``/``sell``.
* :class:`~common.engine.hub_feed.HubTickFeed`, :class:`~common.engine.
  selection.OptionSelector`, :class:`~common.engine.session.MarketSession`,
  :class:`~common.engine.reporting_bindings.HeartbeatEngineReporter`/
  ``RepositoryReportWriter``.
* From :mod:`common.engine.multi_leg_models`: ``LegInstance``, ``LegState``
  (including ``CLOSE_SUBMISSION_UNKNOWN``), ``AdjustmentLifecycle``,
  ``MultiLegDurabilityError`` — the exact same claim/submit/reconcile
  adjustment machine and fail-closed durability posture, applied to
  :class:`~common.engine.positional.positional_models.Cycle` instead of
  ``Basket``.

What is genuinely new, here and in ``positional_models.py``/
``positional_state.py``: cross-session cycle identity, sequential
hedge-first staged entry (spec section 5.2), expiry-day timing evaluated
against the persisted ``resolved_expiry_date`` (:mod:`~common.engine.
positional.lifecycle`), and the explicit "stop the runtime" vs. "square off
exposure" distinction — :meth:`PositionalMultiLegEngine.run` never forces an
exit on its own stop; only an explicit exit trigger (strategy-signalled,
operator-requested, or the engine's own hard-expiry-deadline net) closes a
leg.

**No ``weekly_delta_neutral`` branch anywhere in this module** — every
strategy-specific rule (delta targets, credit/width ratios, adjustment
triggers, expiry-day exit thresholds beyond the generic timing skeleton)
lives in the strategy class this engine drives, never here.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime

from common.broker.quotes import QuoteBook
from common.config.models import ExecutionMode
from common.execution import ExecutionRepository
from common.greeks import GreekSnapshot, GreeksService
from common.logging import get_logger
from common.models import ExitReason, Tick
from common.notifications import NotificationEvent, Notifier, NullNotifier, SafeNotifier
from common.utils.timeutils import now_ist

from ..feed import MarketDataFeed
from ..gateway import LifecycleGateway
from ..positions import PositionManager
from ..reporting import EngineReporter, NullReporter, NullReportWriter, ReportWriter
from ..selection import OptionSelector
from ..session import MarketSession
from .lifecycle import PositionalLifecyclePolicy
from .positional_models import (
    ENTRY_ROLE_ORDER,
    HEDGE_ROLES,
    SHORT_ROLES,
    TERMINAL_CYCLE_STATES,
    AdjustmentLifecycle,
    Cycle,
    CycleAction,
    CycleSignal,
    CycleState,
    LegInstance,
    LegRole,
    LegState,
    MultiLegDurabilityError,
    UnmanageableCycleState,
    entry_has_blocking_leg,
    entry_is_complete,
    next_entry_role,
)
from .positional_state import ROLE_TO_OPTION_TYPE
from .positional_strategy import BasePositionalMultiLegStrategy, PositionalContext

log = get_logger(__name__)


class PositionalMultiLegEngine:
    def __init__(
        self,
        *,
        strategy: BasePositionalMultiLegStrategy,
        position_manager: PositionManager,
        gateway: LifecycleGateway,
        repository: ExecutionRepository,
        feed: MarketDataFeed,
        option_selector: OptionSelector,
        greeks_service: GreeksService,
        quotes: QuoteBook,
        session: MarketSession,
        lifecycle_policy: PositionalLifecyclePolicy,
        underlying_security_id: str,
        underlying_instrument: str,
        underlying_segment: str,
        runtime_id: str,
        strategy_id: str,
        execution_mode: ExecutionMode,
        max_adjustments_per_day: int = 1,
        max_adjustments_per_cycle: int = 3,
        min_minutes_between_adjustments: int = 90,
        evaluation_interval_seconds: float = 5.0,
        max_quote_age_seconds: float = 5.0,
        notifier: Notifier | None = None,
        notify_deferred: bool = True,
        persist_notification_failure: Callable[[NotificationEvent, str], None] | None = None,
        reporter: EngineReporter | None = None,
        report: ReportWriter | None = None,
        square_off_event: threading.Event | None = None,
        recover_cycle: Callable[[], Cycle | None] | None = None,
        persist_cycle: Callable[[Cycle], None] | None = None,
        persist_cycle_leg: Callable[[LegInstance], None] | None = None,
        record_incident: Callable[[str, str], None] | None = None,
        clock: Callable[[], datetime] = now_ist,
    ) -> None:
        self.feed = feed
        self.selector = option_selector
        self.strategy = strategy
        self.positions = position_manager
        self._gateway = gateway
        self._repository = repository
        self._greeks = greeks_service
        self._quotes = quotes
        self.session = session
        self._lifecycle_policy = lifecycle_policy
        self.underlying_id = underlying_security_id
        self.underlying_instrument = underlying_instrument or underlying_security_id
        self.underlying_segment = underlying_segment
        self._runtime_id = runtime_id
        self._strategy_id = strategy_id
        self._mode = execution_mode
        self._max_adjustments_per_day = max_adjustments_per_day
        self._max_adjustments_per_cycle = max_adjustments_per_cycle
        self._min_minutes_between_adjustments = min_minutes_between_adjustments
        self._evaluation_interval = evaluation_interval_seconds
        self._max_quote_age_seconds = max_quote_age_seconds

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
        self._reporter: EngineReporter = reporter or NullReporter()

        self._recover_cycle = recover_cycle
        self._persist_cycle_cb = persist_cycle
        self._persist_cycle_leg_cb = persist_cycle_leg
        self._record_incident_cb = record_incident
        self._now = clock

        self._cycle: Cycle | None = None
        self._spot: float | None = None
        self._spot_fresh_at: datetime | None = None
        self._pending_by_security: dict[str, str] = {}
        self._last_evaluated_at: datetime | None = None

        self._square_off_requested = square_off_event or threading.Event()
        self._stopped = threading.Event()
        self._stopped_by_request = False
        self._entry_blocked: str | None = None
        self.label = strategy.name

    # -------------------------------------------------------------- lifecycle
    def request_square_off(self, reason: str = "external request") -> None:
        """Thread-safe: sets a flag the evaluation loop reads. Unlike
        ``MultiLegEngine``, this does not mean "stop the process" — it means
        "close every open leg", evaluated the same way any other exit
        trigger is (see :meth:`_evaluate`)."""
        self._square_off_reason = reason
        self._square_off_requested.set()

    @property
    def square_off_requested(self) -> bool:
        return self._square_off_requested.is_set()

    def wait_until_stopped(self, timeout: float | None = None) -> bool:
        return self._stopped.wait(timeout)

    @property
    def entries_blocked(self) -> str | None:
        return self._entry_blocked or (self._cycle.day_blocked_reason if self._cycle else None)

    def block_entries(self, reason: str) -> None:
        if self._entry_blocked is None:
            self._entry_blocked = reason
            log.error("%s: entries blocked: %s", self.label, reason)
            self._notify("entries_blocked", f"{self.label} {reason}")

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        self._stopped.clear()
        self._start_session()
        self.feed.on_tick(self.on_tick)
        self._reporter.start()
        log.info(
            "positional engine running mode=%s strategy=%s underlying=%s",
            self._mode,
            self.strategy.name,
            self.underlying_instrument,
        )
        try:
            self.feed.run()
        except (KeyboardInterrupt, SystemExit):
            log.warning("%s: interrupted; stopping (exposure, if any, is left open)", self.label)
            raise
        except Exception as exc:
            self._notify("engine_error", str(exc))
            log.exception(
                "%s: unhandled error; stopping (exposure, if any, is left open)", self.label
            )
            raise
        finally:
            # Deliberately NOT a forced square-off: stopping this engine is
            # not the same act as closing exposure (spec section 9.2). A
            # square-off request is honoured through the ordinary evaluation
            # path (_evaluate -> _apply_signal -> _exit_all), which already
            # ran to completion or recorded why it could not before run()
            # ever reaches this block.
            self._end_session()
            self._reporter.stopped(self.positions.positions, self.positions.trades)
            self._stopped.set()

    def _start_session(self) -> None:
        self._spot = None
        self._entry_blocked = None
        self._pending_by_security = {}
        self.feed.subscribe(self.underlying_id)
        self._adopt_recovered_cycle()
        now = self._now()
        if self._cycle is not None and self._lifecycle_policy.is_new_trading_date(
            last_seen_trading_date=self._cycle.adjustments_today_date, now=now
        ):
            self._cycle.adjustments_today = 0
            self._cycle.adjustments_today_date = self._lifecycle_policy.trading_date(now)
            self._persist_cycle()
            self.strategy.reset_daily()
        log.info("%s: session initialised (cycle=%s)", self.label, self._cycle_id_or_none())

    def _adopt_recovered_cycle(self) -> None:
        """Take over a cycle a previous session left open, placing no order.

        Mirrors ``MultiLegEngine._adopt_recovered_basket`` exactly:
        :class:`~common.engine.positional.positional_models.
        UnmanageableCycleState` propagates (aborting the worker — recovery
        found exposure it cannot prove); any other raise blocks new entries
        but leaves already-open legs alone; ``None`` means no cycle to
        adopt.
        """
        if self._recover_cycle is None:
            return
        try:
            recovered = self._recover_cycle()
        except UnmanageableCycleState:
            raise
        except Exception as exc:
            log.exception("%s: could not establish whether a cycle was carried over", self.label)
            self.block_entries(
                f"restart recovery failed ({exc}) — unable to establish whether a cycle "
                "from a previous session is still open"
            )
            return
        if recovered is None:
            return

        self._cycle = recovered
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
                    entry_correlation_id=leg.entry_correlation_id,
                )
                self.feed.subscribe(leg.contract.security_id)
            elif leg.state in (
                LegState.PENDING_CONTRACT,
                LegState.PENDING_SUBSCRIPTION,
                LegState.PENDING_ORDER,
            ):
                if leg.contract is not None:
                    self.feed.subscribe(leg.contract.security_id)
                    self._pending_by_security[leg.contract.security_id] = leg.leg_id
        log.info(
            "%s: adopted cycle %s (state=%s): %d open leg(s)",
            self.label,
            recovered.cycle_id,
            recovered.state.value,
            len(recovered.open_legs()),
        )
        self._notify(
            "restart_adoption",
            f"adopted cycle {recovered.cycle_id} (state={recovered.state.value}): "
            f"{len(recovered.open_legs())} open leg(s)",
        )

    def _end_session(self) -> None:
        summary = self.strategy.status()
        log.info("%s: session ended — %s", self.label, summary)

    def _cycle_id_or_none(self) -> str:
        return self._cycle.cycle_id if self._cycle is not None else "(none)"

    # ------------------------------------------------------- tick routing
    def on_tick(self, tick: Tick) -> None:
        if tick.security_id == self.underlying_id:
            self._on_underlying_tick(tick)
            return
        self._on_leg_tick(tick)

    def _on_underlying_tick(self, tick: Tick) -> None:
        self._spot = tick.last_price
        self._spot_fresh_at = tick.exchange_time
        self._maybe_evaluate(tick.exchange_time)

    def _on_leg_tick(self, tick: Tick) -> None:
        leg_id = self._pending_by_security.get(tick.security_id)
        if leg_id is not None and self._cycle is not None:
            # A pending entry/adjustment leg becomes tradeable once ticks
            # confirm the feed genuinely carries it; the actual order is
            # still placed synchronously from _drive_entry/_roll_short
            # against a fresh quote, not from this tick directly.
            return
        if self._cycle is None:
            return
        for leg in self._cycle.legs.values():
            if (
                leg.state is LegState.OPEN
                and leg.contract is not None
                and leg.contract.security_id == tick.security_id
            ):
                leg.update_price(tick.last_price)
                break

    def poll(self) -> None:
        """Called on the feed's own timer (``HubTickFeed(on_poll=...)``) so
        time-based evaluation (expiry-day deadlines, the 90-minute
        adjustment cooldown) fires even on a quiet tick stream."""
        self._maybe_evaluate(self._now())

    def _maybe_evaluate(self, ts: datetime) -> None:
        if self._last_evaluated_at is not None:
            elapsed = (ts - self._last_evaluated_at).total_seconds()
            if elapsed < self._evaluation_interval:
                return
        self._last_evaluated_at = ts
        self._evaluate(ts)

    # ----------------------------------------------------------- evaluation
    def _evaluate(self, ts: datetime) -> None:
        cycle_is_active = self._cycle is not None and self._cycle.state not in TERMINAL_CYCLE_STATES
        if self._square_off_requested.is_set() and cycle_is_active:
            self._exit_all(
                CycleSignal(
                    action=CycleAction.EXIT_ALL,
                    timestamp=ts,
                    reason=getattr(self, "_square_off_reason", "operator square-off"),
                    exit_reason=ExitReason.SQUARE_OFF,
                ),
                ts,
            )
            return

        # The engine's own hard-expiry-deadline net: fires regardless of
        # what the strategy itself would have signalled, and cannot be
        # defeated by any execution config (spec section 8's "not disabled
        # by market_fallback_enabled: false" — enforced at the paper broker
        # level: see config/strategies/weekly_delta_neutral.yaml's
        # allow_ltp_fallback: false, which makes an LTP-based market fill
        # structurally impossible, not merely discouraged).
        if self._cycle is not None and self._cycle.state not in TERMINAL_CYCLE_STATES:
            phase = self._lifecycle_policy.expiry_day_phase(
                resolved_expiry_date=self._cycle.resolved_expiry_date, now=ts
            )
            hard_exit_due = self._lifecycle_policy.hard_exit_due(phase)
            if hard_exit_due and self._cycle.state is not CycleState.EXITING:
                self._exit_all(
                    CycleSignal(
                        action=CycleAction.EXIT_ALL,
                        timestamp=ts,
                        reason="actual-expiry-day hard exit deadline (15:15)",
                        exit_reason=ExitReason.EXPIRY,
                    ),
                    ts,
                )
                return

        context = self._build_context(ts)
        signal = self.strategy.evaluate(cycle=self._cycle, context=context)
        if signal is None or signal.action is CycleAction.NONE:
            return
        self._apply_signal(signal, ts)

    def _build_context(self, ts: datetime) -> PositionalContext:
        chain = None
        if self._cycle is not None:
            try:
                chain = self._greeks.chain_snapshot(
                    underlying_security_id=int(self.underlying_id),
                    underlying_segment=self.underlying_segment,
                    expiry=self._cycle.resolved_expiry_date,
                )
            except Exception as exc:
                log.warning("%s: could not fetch/parse the option chain: %s", self.label, exc)

        leg_greeks: dict[str, GreekSnapshot] = {}
        if self._cycle is not None and chain is not None:
            for leg in self._cycle.open_legs():
                if leg.contract is None:
                    continue
                try:
                    leg_greeks[leg.contract.security_id] = self._greeks.resolve(
                        chain=chain,
                        security_id=leg.contract.security_id,
                        option_type=ROLE_TO_OPTION_TYPE.get(leg.role, leg.contract.option_type),
                        strike=leg.contract.strike,
                        spot=self._spot or 0.0,
                        expiry_at=ts,
                    )
                except Exception:
                    continue

        return PositionalContext(
            now=ts,
            trading_date=self._lifecycle_policy.trading_date(ts),
            spot=self._spot,
            spot_is_fresh=self._spot_fresh_at is not None,
            chain=chain,
            leg_greeks=leg_greeks,
            can_enter=self.session.can_enter(ts),
            is_holiday=self.session.is_holiday(ts),
            is_trading_day=self.session.is_trading_day(ts),
        )

    def _apply_signal(self, signal: CycleSignal, ts: datetime) -> None:
        if signal.action is CycleAction.ENTER_CYCLE:
            self._enter_cycle(signal, ts)
        elif signal.action is CycleAction.ROLL_SHORT:
            self._roll_short(signal, ts)
        elif signal.action is CycleAction.REPAIR_HEDGE:
            self._repair_hedge(signal, ts)
        elif signal.action is CycleAction.EXIT_ALL:
            self._exit_all(signal, ts)
        elif signal.action is CycleAction.EXIT_LEG:
            self._exit_leg(signal, ts)

    # --------------------------------------------------------------- entry
    def _enter_cycle(self, signal: CycleSignal, ts: datetime) -> None:
        if self._entry_blocked is not None:
            log.info(
                "%s: entry suppressed — entries are blocked (%s)", self.label, self._entry_blocked
            )
            return
        if self._cycle is not None and self._cycle.state not in TERMINAL_CYCLE_STATES:
            log.error(
                "%s: ENTER_CYCLE received while cycle %s is already open; ignoring",
                self.label,
                self._cycle.cycle_id,
            )
            return
        signal_roles = {intent.role for intent in signal.legs}
        if len(signal.legs) != 4 or signal_roles != set(ENTRY_ROLE_ORDER):
            log.error(
                "%s: ENTER_CYCLE must carry exactly the four original roles %s; got %s",
                self.label,
                [r.value for r in ENTRY_ROLE_ORDER],
                [intent.role.value for intent in signal.legs],
            )
            return

        resolved_expiry = signal.legs[0].contract.expiry
        trading_date = self._lifecycle_policy.trading_date(ts)
        cycle_id = f"{self._strategy_id}:{resolved_expiry}"
        cycle = Cycle(
            cycle_id=cycle_id,
            strategy_id=self._strategy_id,
            execution_mode=self._mode,
            underlying=self.underlying_instrument,
            resolved_expiry_date=resolved_expiry,
            opened_trading_date=trading_date,
            entries_consumed=True,
            original_wing_width=signal.original_wing_width,
            adjustments_today_date=trading_date,
        )
        for intent in signal.legs:
            leg_id = cycle.next_leg_id(intent.role)
            cycle.legs[leg_id] = LegInstance(
                leg_id=leg_id,
                basket_id=cycle_id,
                role=intent.role,
                sequence=cycle.next_sequence(intent.role),
                is_replacement=False,
                side=intent.side,
                contract=intent.contract,
                state=LegState.PENDING_ORDER,
            )

        # Pre-effect (spec section 5.1): cycle identity, resolved expiry,
        # entry-attempt consumption and every intended leg/contract/
        # correlation must be durable *before* the first order.
        self._cycle = cycle
        try:
            self._persist_cycle(critical=True)
            for leg in cycle.legs.values():
                self._persist_leg(leg, critical=True)
        except MultiLegDurabilityError:
            log.error(
                "%s: could not durably persist the intended entry before any order; refusing",
                self.label,
            )
            self._cycle = None
            return

        for leg in cycle.legs.values():
            if leg.contract is not None:
                self.feed.subscribe(leg.contract.security_id)
        self._log_event("cycle_entry_attempted", f"expiry={resolved_expiry}", ts)
        self._drive_entry(ts)

    def _drive_entry(self, ts: datetime) -> None:
        if self._cycle is None:
            return
        while True:
            role = next_entry_role(self._cycle)
            if role is None:
                break
            leg = self._cycle.original_leg(role)
            assert leg is not None
            if not self._open_leg_now(leg, ts):
                return  # no fresh quote yet — resume on the next evaluation

        if entry_is_complete(self._cycle):
            self._finalize_entry(ts)
        elif entry_has_blocking_leg(self._cycle):
            self._unwind_partial_entry(ts)

    def _open_leg_now(self, leg: LegInstance, ts: datetime) -> bool:
        """Attempt to fill ``leg`` now.

        Returns ``True`` once this role is resolved (OPEN or definitively
        FAILED) so the caller may move on; ``False`` only when there is
        simply no fresh, complete quote yet — never a failure, just "not
        yet" (spec section 3.6: reject stale/incomplete quotes rather than
        relax the requirement; the retry happens on the next evaluation).
        """
        assert self._cycle is not None
        assert leg.contract is not None
        quote = self._quotes.latest(leg.contract.security_id)
        quote_age = (ts - quote.quoted_at).total_seconds() if quote is not None else None
        if (
            quote is None
            or not quote.has_depth
            or quote_age is None
            or quote_age > self._max_quote_age_seconds
        ):
            log.info(
                "%s: no fresh two-sided quote yet for %s (role=%s, age=%s); will retry",
                self.label,
                leg.contract.symbol,
                leg.role.value,
                quote_age,
            )
            return False

        ref_price = leg.last_price if leg.last_price is not None else quote.last_price
        try:
            position = self.positions.open(
                leg.contract,
                leg.side,
                ref_price,
                ts,
                basket_id=self._cycle.cycle_id,
                leg_id=leg.leg_id,
                cycle_id=self._cycle.cycle_id,
            )
        except Exception as exc:
            log.exception("%s: opening leg %s raised; marking FAILED", self.label, leg.leg_id)
            leg.state = LegState.FAILED
            self._persist_leg(leg)
            self._log_event("leg_entry_failed", f"{leg.leg_id}: {exc}", ts)
            return True

        leg.state = LegState.OPEN
        leg.entry_price = position.entry_price
        leg.quantity = position.quantity
        leg.entry_time = ts
        leg.last_price = position.entry_price
        leg.entry_correlation_id = position.entry_correlation_id
        self._persist_leg(leg)
        self._notify(
            "fill",
            f"{leg.side.value} {leg.contract.symbol} @ {position.entry_price:.2f} "
            f"(cycle {self._cycle.cycle_id}, role={leg.role.value})",
        )
        return True

    def _finalize_entry(self, ts: datetime) -> None:
        assert self._cycle is not None
        hedge_put = self._cycle.original_leg(LegRole.HEDGE_PUT)
        hedge_call = self._cycle.original_leg(LegRole.HEDGE_CALL)
        short_put = self._cycle.original_leg(LegRole.SHORT_PUT)
        short_call = self._cycle.original_leg(LegRole.SHORT_CALL)
        hedges = [leg for leg in (hedge_put, hedge_call) if leg is not None]
        shorts = [leg for leg in (short_put, short_call) if leg is not None]

        hedge_cost = sum((leg.entry_price or 0.0) * (leg.quantity or 0) for leg in hedges)
        short_credit = sum((leg.entry_price or 0.0) * (leg.quantity or 0) for leg in shorts)
        entry_charges = self._position_charges_total()
        net_credit = short_credit - hedge_cost - entry_charges

        put_width = (
            short_put.contract.strike - hedge_put.contract.strike
            if short_put and hedge_put and short_put.contract and hedge_put.contract
            else None
        )
        call_width = (
            hedge_call.contract.strike - short_call.contract.strike
            if short_call and hedge_call and short_call.contract and hedge_call.contract
            else None
        )
        widths = [w for w in (put_width, call_width) if w is not None]
        wing_width = max(widths) if widths else self._cycle.original_wing_width
        put_quantity = short_put.quantity if short_put else 0
        call_quantity = short_call.quantity if short_call else 0
        quantity = put_quantity or call_quantity
        max_loss = (
            (wing_width * quantity - net_credit) if wing_width is not None and quantity else None
        )

        self._cycle.original_net_credit = net_credit
        self._cycle.original_max_loss = max_loss
        self._cycle.original_wing_width = wing_width
        self._cycle.state = CycleState.ACTIVE
        self._cycle.day_blocked_reason = None
        self._persist_cycle()
        self._log_event(
            "cycle_entry_completed",
            f"net_credit={net_credit:.2f} max_loss={max_loss} wing_width={wing_width}",
            ts,
        )
        self._notify(
            "cycle_entry_completed",
            f"cycle {self._cycle.cycle_id} active: "
            f"net_credit={net_credit:.2f}, max_loss={max_loss}",
        )

    def _unwind_partial_entry(self, ts: datetime) -> None:
        assert self._cycle is not None
        log.error(
            "%s: partial entry for cycle %s; unwinding every filled leg",
            self.label,
            self._cycle.cycle_id,
        )
        self._cycle.day_blocked_reason = "partial entry — unwinding"
        self._persist_cycle()

        for leg in list(self._cycle.open_legs()):
            self._close_leg_safely(
                leg, ts, ExitReason.STRATEGY_EXIT, reason_text="partial-entry unwind"
            )
        for leg in list(self._cycle.pending_legs()):
            leg.state = LegState.EXPIRED
            self._persist_leg(leg)

        if self._cycle.unresolved_legs():
            self._cycle.state = CycleState.CRITICAL_UNRESOLVED
            self.block_entries(
                f"cycle {self._cycle.cycle_id} partial-entry unwind left unresolved exposure"
            )
            self._record_incident(
                self._cycle.cycle_id,
                "partial-entry unwind left at least one leg CLOSE_SUBMISSION_UNKNOWN",
            )
        else:
            self._cycle.state = CycleState.FAILED
        self._persist_cycle()
        self._log_event("cycle_entry_failed", f"state={self._cycle.state.value}", ts)
        self._notify(
            "cycle_entry_failed",
            f"cycle {self._cycle.cycle_id} entry failed and was unwound "
            f"(state={self._cycle.state.value})",
        )

    def _position_charges_total(self) -> float:
        if self._cycle is None:
            return 0.0
        positions = self._repository.open_positions_for_cycle(cycle_id=self._cycle.cycle_id)
        return sum(p.charges for p in positions)

    # ---------------------------------------------------------- adjustment
    def _roll_short(self, signal: CycleSignal, ts: datetime) -> None:
        """The claim/submit/reconcile adjustment machine (spec section 7.3),
        reusing :class:`~common.engine.multi_leg_models.AdjustmentLifecycle`
        verbatim, applied to the cycle instead of a basket."""
        if self._cycle is None or signal.target_leg_id is None or len(signal.legs) != 1:
            log.error("%s: malformed ROLL_SHORT signal; ignoring", self.label)
            return
        old_leg = self._cycle.legs.get(signal.target_leg_id)
        if old_leg is None or old_leg.state is not LegState.OPEN:
            log.error(
                "%s: ROLL_SHORT target %s is not OPEN; ignoring", self.label, signal.target_leg_id
            )
            return

        self._cycle.adjustments_today += 1
        self._cycle.adjustments_this_cycle += 1
        self._cycle.pending_adjustment_role = old_leg.role
        self._cycle.pending_adjustment_state = AdjustmentLifecycle.EXIT_SUBMISSION_PENDING.value
        try:
            self._persist_cycle(critical=True)
        except MultiLegDurabilityError:
            log.error(
                "%s: could not durably claim the adjustment; leg %s stays open, no close attempted",
                self.label,
                old_leg.leg_id,
            )
            self._cycle.adjustments_today -= 1
            self._cycle.adjustments_this_cycle -= 1
            self._cycle.pending_adjustment_role = None
            self._cycle.pending_adjustment_state = None
            return

        claimed_at = ts.isoformat()
        closed = self._close_leg_safely(
            old_leg, ts, ExitReason.ADJUSTMENT, reason_text=signal.reason
        )
        self._cycle.pending_adjustment_state = (
            AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value
            if closed
            else AdjustmentLifecycle.EXIT_UNKNOWN.value
        )
        self._persist_cycle()

        if not closed:
            self._record_adjustment(
                signal, old_leg, None, claimed_at, AdjustmentLifecycle.EXIT_UNKNOWN.value
            )
            return

        self._cycle.last_adjustment_at = ts
        replacement_intent = signal.legs[0]
        replacement_leg = LegInstance(
            leg_id=self._cycle.next_leg_id(replacement_intent.role),
            basket_id=self._cycle.cycle_id,
            role=replacement_intent.role,
            sequence=self._cycle.next_sequence(replacement_intent.role),
            is_replacement=True,
            side=replacement_intent.side,
            contract=replacement_intent.contract,
            replaces_leg_id=old_leg.leg_id,
            state=LegState.PENDING_ORDER,
        )
        self._cycle.legs[replacement_leg.leg_id] = replacement_leg
        self._cycle.pending_adjustment_state = AdjustmentLifecycle.REPLACEMENT_PENDING.value
        try:
            self._persist_cycle(critical=True)
            self._persist_leg(replacement_leg, critical=True)
        except MultiLegDurabilityError:
            log.error(
                "%s: could not durably persist the replacement short before submitting; refusing",
                self.label,
            )
            replacement_leg.state = LegState.FAILED
            self._cycle.pending_adjustment_state = AdjustmentLifecycle.REPLACEMENT_FAILED.value
            self._persist_cycle()
            self._record_adjustment(
                signal,
                old_leg,
                replacement_leg,
                claimed_at,
                AdjustmentLifecycle.REPLACEMENT_FAILED.value,
            )
            return

        if replacement_leg.contract is not None:
            self.feed.subscribe(replacement_leg.contract.security_id)
        if not self._open_leg_now(replacement_leg, ts):
            return  # retried on a later evaluation — leg stays PENDING_ORDER

        outcome = (
            AdjustmentLifecycle.REPLACEMENT_FILLED
            if replacement_leg.state is LegState.OPEN
            else AdjustmentLifecycle.REPLACEMENT_FAILED
        )
        self._cycle.pending_adjustment_state = outcome.value
        self._cycle.pending_adjustment_role = None
        self._persist_cycle()
        self._record_adjustment(signal, old_leg, replacement_leg, claimed_at, outcome.value)
        self._log_event(
            "adjustment_completed",
            f"rolled {old_leg.leg_id} -> {replacement_leg.leg_id} ({outcome.value})",
            ts,
        )

    def _record_adjustment(
        self,
        signal: CycleSignal,
        old_leg: LegInstance,
        replacement_leg: LegInstance | None,
        claimed_at: str,
        lifecycle_state: str,
    ) -> None:
        assert self._cycle is not None
        self._repository.append_cycle_adjustment(
            runtime_id=self._runtime_id,
            strategy_id=self._strategy_id,
            execution_mode=self._mode,
            cycle_id=self._cycle.cycle_id,
            adjustment_sequence=self._cycle.adjustments_this_cycle,
            trigger_reason=signal.reason or "delta adjustment",
            target_leg_id=old_leg.leg_id,
            replacement_leg_id=replacement_leg.leg_id if replacement_leg is not None else None,
            claimed_at=claimed_at,
            pre_adjustment_net_delta=signal.pre_adjustment_net_delta,
            post_adjustment_net_delta=signal.post_adjustment_net_delta,
            realized_pnl=old_leg.realized_gross_pnl,
            charges=None,
            lifecycle_state=lifecycle_state,
        )

    def _repair_hedge(self, signal: CycleSignal, ts: datetime) -> None:
        """Spec section 7.2/12.3 step 2: repair a deficient hedge before a
        risk-increasing replacement is permitted. A simpler one-shot open —
        no old leg to close first, since a missing/deficient hedge is (by
        definition) not currently occupying that role."""
        if self._cycle is None or len(signal.legs) != 1:
            log.error("%s: malformed REPAIR_HEDGE signal; ignoring", self.label)
            return
        intent = signal.legs[0]
        existing = self._cycle.current_leg(intent.role)
        if existing is not None:
            log.error(
                "%s: REPAIR_HEDGE for role %s but a leg already occupies it (%s); ignoring",
                self.label,
                intent.role.value,
                existing.leg_id,
            )
            return

        leg = LegInstance(
            leg_id=self._cycle.next_leg_id(intent.role),
            basket_id=self._cycle.cycle_id,
            role=intent.role,
            sequence=self._cycle.next_sequence(intent.role),
            is_replacement=True,
            side=intent.side,
            contract=intent.contract,
            state=LegState.PENDING_ORDER,
        )
        self._cycle.legs[leg.leg_id] = leg
        try:
            self._persist_leg(leg, critical=True)
        except MultiLegDurabilityError:
            log.error(
                "%s: could not durably persist the hedge repair before submitting; refusing",
                self.label,
            )
            return
        if leg.contract is not None:
            self.feed.subscribe(leg.contract.security_id)
        if self._open_leg_now(leg, ts):
            self._log_event("hedge_repaired", f"{leg.leg_id} ({leg.role.value})", ts)

    # -------------------------------------------------------------- exits
    def _exit_all(self, signal: CycleSignal, ts: datetime) -> None:
        if self._cycle is None:
            return
        reason = signal.exit_reason or ExitReason.STRATEGY_EXIT
        self._cycle.day_blocked_reason = signal.reason or reason.value
        self._cycle.state = CycleState.EXITING
        self._cycle.square_off_state = "IN_PROGRESS"
        self._persist_cycle()
        self._log_event("exit_triggered", f"reason={reason.value}: {signal.reason}", ts)

        # Spec section 6.2/8: shorts closed and reconciled before hedges.
        for leg in list(self._cycle.legs.values()):
            if leg.role in SHORT_ROLES and leg.state is LegState.OPEN:
                self._close_leg_safely(leg, ts, reason, reason_text=signal.reason)
        for leg in list(self._cycle.legs.values()):
            if leg.role in HEDGE_ROLES and leg.state is LegState.OPEN:
                self._close_leg_safely(leg, ts, reason, reason_text=signal.reason)
        for leg in list(self._cycle.legs.values()):
            if leg.role is LegRole.GENERIC and leg.state is LegState.OPEN:
                self._close_leg_safely(leg, ts, reason, reason_text=signal.reason)
        for leg in list(self._cycle.pending_legs()):
            leg.state = LegState.EXPIRED
            self._persist_leg(leg)

        if self._cycle.unresolved_legs() or self._cycle.open_legs():
            # Never marked complete merely because retry limits were
            # exhausted (spec section 8) — an unresolved close OR any leg
            # still genuinely OPEN (e.g. a close was never attempted because
            # a durability write failed first) keeps the cycle critical.
            self._cycle.state = CycleState.CRITICAL_UNRESOLVED
            self.block_entries(f"cycle {self._cycle.cycle_id} exit left unresolved/open exposure")
            self._record_incident(
                self._cycle.cycle_id, "exit could not prove the cycle flat — never marked complete"
            )
            self._persist_cycle()
            self._log_event("exit_unresolved", "cycle left CRITICAL_UNRESOLVED", ts)
            return

        self._cycle.state = CycleState.COMPLETED
        self._cycle.square_off_state = "COMPLETED"
        self._persist_cycle()
        self._log_event("cycle_completed", f"net_pnl={self._cycle.total_gross_pnl():.2f}", ts)
        self._notify(
            "cycle_completed",
            f"cycle {self._cycle.cycle_id} closed flat: "
            f"gross_pnl={self._cycle.total_gross_pnl():.2f}",
        )

    def _exit_leg(self, signal: CycleSignal, ts: datetime) -> None:
        if self._cycle is None or signal.target_leg_id is None:
            return
        leg = self._cycle.legs.get(signal.target_leg_id)
        if leg is None or leg.state is not LegState.OPEN:
            return
        reason = signal.exit_reason or ExitReason.STRATEGY_EXIT
        self._close_leg_safely(leg, ts, reason, reason_text=signal.reason)

    def _close_leg(self, leg: LegInstance, price: float, ts: datetime, reason: ExitReason) -> None:
        assert self._cycle is not None
        if leg.contract is None:
            raise RuntimeError(f"leg {leg.leg_id} has no resolved contract to close")
        trade = self.positions.close(
            leg.contract.security_id,
            price,
            ts,
            reason,
            basket_id=self._cycle.cycle_id,
            leg_id=leg.leg_id,
            cycle_id=self._cycle.cycle_id,
        )
        leg.state = LegState.CLOSED
        leg.exit_price = trade.exit_price
        leg.exit_time = ts
        leg.exit_reason = reason
        leg.exit_correlation_id = trade.exit_correlation_id
        leg.realized_gross_pnl = trade.gross_pnl
        self._persist_leg(leg)
        self.feed.unsubscribe(leg.contract.security_id)
        self._notify(
            "exit",
            f"{trade.side.value} {trade.contract.symbol} @ {trade.exit_price:.2f} "
            f"reason={reason.value} gross={trade.gross_pnl:.2f}",
        )

    def _close_leg_safely(
        self, leg: LegInstance, ts: datetime, reason: ExitReason, *, reason_text: str = ""
    ) -> bool:
        """Never raises. Returns ``True`` on a confirmed close; ``False``
        leaves ``leg`` in ``CLOSE_SUBMISSION_UNKNOWN`` (never retried
        automatically) and blocks new entries — identical posture to
        ``MultiLegEngine._close_leg_safely``."""
        assert self._cycle is not None
        if leg.contract is None:
            return False
        price = leg.last_price if leg.last_price is not None else 0.0
        try:
            self._close_leg(leg, price, ts, reason)
            return True
        except Exception as exc:
            log.exception("%s: closing leg %s raised — outcome unknown", self.label, leg.leg_id)
            leg.state = LegState.CLOSE_SUBMISSION_UNKNOWN
            self._persist_leg(leg)
            self.block_entries(f"leg {leg.leg_id} close outcome unknown: {exc}")
            self._record_incident(
                self._cycle.cycle_id,
                f"leg {leg.leg_id} close raised, outcome unknown: {exc} ({reason_text})",
            )
            return False

    def _log_event(self, event_type: str, detail: str, ts: datetime) -> None:
        if self._cycle is None:
            return
        try:
            self._repository.append_cycle_event(
                runtime_id=self._runtime_id,
                strategy_id=self._strategy_id,
                execution_mode=self._mode,
                cycle_id=self._cycle.cycle_id,
                event_type=event_type,
                detail=detail,
                trading_date=self._lifecycle_policy.trading_date(ts),
            )
        except Exception:
            log.exception("%s: could not append cycle event %s", self.label, event_type)

    # -------------------------------------------------------------- helpers
    def _persist_cycle(self, *, critical: bool = False) -> None:
        if self._cycle is None or self._persist_cycle_cb is None:
            return
        try:
            self._persist_cycle_cb(self._cycle)
        except Exception as exc:
            if critical:
                self.block_entries(f"critical cycle persistence failed: {exc}")
                self._record_incident(self._cycle.cycle_id, f"critical cycle persist failed: {exc}")
                raise MultiLegDurabilityError(str(exc)) from exc
            log.exception("%s: best-effort cycle persistence failed", self.label)
            self._record_incident(self._cycle.cycle_id, f"cycle persist failed: {exc}")

    def _persist_leg(self, leg: LegInstance, *, critical: bool = False) -> None:
        if self._persist_cycle_leg_cb is None or self._cycle is None:
            return
        try:
            self._persist_cycle_leg_cb(leg)
        except Exception as exc:
            if critical:
                self.block_entries(f"critical leg persistence failed: {exc}")
                self._record_incident(
                    self._cycle.cycle_id, f"critical leg persist failed for {leg.leg_id}: {exc}"
                )
                raise MultiLegDurabilityError(str(exc)) from exc
            log.exception("%s: best-effort leg persistence failed for %s", self.label, leg.leg_id)
            self._record_incident(
                self._cycle.cycle_id, f"leg persist failed for {leg.leg_id}: {exc}"
            )

    def _record_incident(self, cycle_id: str, message: str) -> None:
        log.critical("%s: INCIDENT cycle=%s: %s", self.label, cycle_id, message)
        if self._record_incident_cb is not None:
            try:
                self._record_incident_cb(cycle_id, message)
            except Exception:
                log.exception("%s: record_incident callback itself failed", self.label)

    def _notify(self, event_type: str, message: str) -> None:
        self.notifier.send(
            NotificationEvent(
                event_type=event_type,
                message=message,
                runtime_id=self._runtime_id,
                strategy_id=self._strategy_id,
                execution_mode=self._mode,
            )
        )
