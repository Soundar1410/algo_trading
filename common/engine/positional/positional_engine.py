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
from datetime import datetime, timedelta

from common.broker.quotes import QuoteBook
from common.config.models import ExecutionMode
from common.execution import ExecutionRepository
from common.feed.queues import FeedGapNotice
from common.greeks import GreekSnapshot, GreeksService
from common.logging import get_logger
from common.margin import MarginEstimate, MarginEstimator
from common.models import ExitReason, Position, Tick
from common.notifications import NotificationEvent, Notifier, NullNotifier, SafeNotifier
from common.utils.timeutils import is_fresh, now_ist

from ..feed import MarketDataFeed
from ..gateway import LifecycleGateway
from ..positions import PositionManager
from ..reporting import EngineReporter, NullReporter, NullReportWriter, ReportWriter
from ..selection import OptionSelector
from ..session import MarketSession
from .lifecycle import PositionalLifecyclePolicy, expiry_settlement_at
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
    cycle_id_for,
    entry_has_blocking_leg,
    entry_is_complete,
    next_entry_role,
)
from .positional_state import ROLE_TO_OPTION_TYPE
from .positional_strategy import BasePositionalMultiLegStrategy, PositionalContext

log = get_logger(__name__)

#: Phase 5A: how long a single staged-entry step (one leg role) may wait for
#: its dynamically subscribed contract's first fresh Full-mode quote before
#: the step is durably failed and the partial entry unwound (spec section
#: 5.3's "bounded workflow"). Generous for a real dynamic-subscription/quote
#: round trip, still bounded — see ``_advance_entry_stage``.
DEFAULT_ENTRY_LEG_TIMEOUT_SECONDS = 180.0

#: ``_entry_submission_outcome``'s return vocabulary — mirrors
#: ``runtimes.positional_options.positional_multi_leg_engine_worker.
#: _resolve_intent_outcome`` exactly, duplicated rather than imported (this
#: module is common/engine; that one is a runtime composition root and must
#: never be imported from here).
_NEVER_PLACED = "NEVER_PLACED"
_SUBMISSION_UNKNOWN = "UNKNOWN"
_TERMINAL_NO_FILL = "TERMINAL_NO_FILL"
_FILLED = "FILLED"

#: Phase 6A: the signal kinds the market-data-degraded gate suppresses.
#: Every other action (EXIT_ALL/EXIT_LEG) always dispatches regardless —
#: exits/reconciliation must never be blocked by stale data.
_RISK_INCREASING_ACTIONS = frozenset(
    {CycleAction.ENTER_CYCLE, CycleAction.ROLL_SHORT, CycleAction.REPAIR_HEDGE}
)


class PositionalMultiLegEngine:
    def __init__(
        self,
        *,
        strategy: BasePositionalMultiLegStrategy,
        position_manager: PositionManager,
        gateway: LifecycleGateway,
        repository: ExecutionRepository,
        feed: MarketDataFeed,
        option_selector: OptionSelector | None = None,
        greeks_service: GreeksService,
        margin_estimator: MarginEstimator,
        quotes: QuoteBook,
        session: MarketSession,
        lifecycle_policy: PositionalLifecyclePolicy,
        underlying_security_id: str,
        underlying_instrument: str,
        underlying_segment: str,
        option_segment: str,
        runtime_id: str,
        strategy_id: str,
        execution_mode: ExecutionMode,
        max_adjustments_per_day: int = 1,
        max_adjustments_per_cycle: int = 3,
        min_minutes_between_adjustments: int = 90,
        evaluation_interval_seconds: float = 5.0,
        max_quote_age_seconds: float = 5.0,
        entry_leg_timeout_seconds: float = DEFAULT_ENTRY_LEG_TIMEOUT_SECONDS,
        notifier: Notifier | None = None,
        notify_deferred: bool = True,
        persist_notification_failure: Callable[[NotificationEvent, str], None] | None = None,
        reporter: EngineReporter | None = None,
        report: ReportWriter | None = None,
        square_off_event: threading.Event | None = None,
        recover_cycle: Callable[[], Cycle | None] | None = None,
        persist_cycle: Callable[[Cycle], None] | None = None,
        persist_cycle_leg: Callable[[LegInstance], None] | None = None,
        persist_margin_snapshot: Callable[[str, MarginEstimate], None] | None = None,
        record_incident: Callable[[str, str], None] | None = None,
        clock: Callable[[], datetime] = now_ist,
    ) -> None:
        self.feed = feed
        #: Unlike MultiLegEngine, this engine never resolves a contract
        #: itself — entry/adjustment/hedge-repair candidate selection is the
        #: strategy's own job (it needs live Greeks the engine does not
        #: otherwise fetch). Accepted and stored only for forward
        #: compatibility/observability; optional so a worker with nothing to
        #: hand it need not fabricate one.
        self.selector = option_selector
        self.strategy = strategy
        self.positions = position_manager
        self._gateway = gateway
        self._repository = repository
        self._greeks = greeks_service
        self._margin = margin_estimator
        self._quotes = quotes
        self.session = session
        self._lifecycle_policy = lifecycle_policy
        self.underlying_id = underlying_security_id
        self.underlying_instrument = underlying_instrument or underlying_security_id
        self.underlying_segment = underlying_segment
        #: The exchange segment every option leg lives in — distinct from
        #: ``underlying_segment`` (e.g. the underlying index's ``IDX_I``).
        #: Threaded through to ``PositionalContext`` so a strategy computing
        #: a margin estimate (spec section 3.7) never has to re-derive it or
        #: guess at the underlying's own segment for an option leg.
        self.option_segment = option_segment
        self._runtime_id = runtime_id
        self._strategy_id = strategy_id
        self._mode = execution_mode
        self._max_adjustments_per_day = max_adjustments_per_day
        self._max_adjustments_per_cycle = max_adjustments_per_cycle
        self._min_minutes_between_adjustments = min_minutes_between_adjustments
        self._evaluation_interval = evaluation_interval_seconds
        self._max_quote_age_seconds = max_quote_age_seconds
        self._entry_leg_timeout_seconds = entry_leg_timeout_seconds

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
        self._persist_margin_snapshot_cb = persist_margin_snapshot
        self._record_incident_cb = record_incident
        self._now = clock

        self._cycle: Cycle | None = None
        self._spot: float | None = None
        self._spot_fresh_at: datetime | None = None
        self._pending_by_security: dict[str, str] = {}
        self._last_evaluated_at: datetime | None = None

        #: Phase 6A: the recoverable market-data-degraded gate. Latched the
        #: instant a "disconnected" FeedGapNotice arrives; cleared only once
        #: every currently-required security id has shown a genuinely fresh
        #: tick since the outage's own resubscribe was confirmed — never on
        #: the reconnect/resubscribe/supervisor-recovered events alone. See
        #: on_feed_gap_notice/_record_tick_for_gate.
        self._market_data_degraded = False
        #: Wall-clock instant the current outage's own resubscription round
        #: trip was confirmed complete — ``None`` until a "resubscribed"
        #: notice arrives for it. A tick only counts towards clearing the
        #: gate once its own exchange_time is at/after this instant.
        self._resubscribed_at: datetime | None = None
        #: Security ids observed with a fresh tick since ``_resubscribed_at``
        #: was last set. Reset on every "disconnected"/"resubscribed" notice.
        self._fresh_since_resubscribe: set[str] = set()

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
        self._record_tick_for_gate(tick)
        if tick.security_id == self.underlying_id:
            self._on_underlying_tick(tick)
            return
        self._on_leg_tick(tick)

    # --------------------------------------------------- market-data gate
    def on_feed_gap_notice(self, notice: FeedGapNotice) -> None:
        """Phase 6A: the shared feed's own connection-state change, wired
        from ``HubTickFeed``'s ``on_feed_gap`` callback (broadcast to every
        worker sharing the feed — see ``FeedGapNotice``'s own docstring).

        ``"disconnected"`` latches the gate immediately, before any further
        evaluation can act — this runs on the same thread/callback path a
        tick would use, so it is processed in order, ahead of whatever tick
        comes next. ``"resubscribed"`` only records that this outage's own
        resubscription round trip completed; it is never by itself
        sufficient to clear the gate (a fresh, worker-selected security_id
        could still be silently missing its own subscription — the whole
        reason this gate does not simply trust the supervisor's own
        "recovered" health state).
        """
        if notice.kind == "disconnected":
            was_degraded = self._market_data_degraded
            self._market_data_degraded = True
            self._resubscribed_at = None
            self._fresh_since_resubscribe = set()
            if not was_degraded:
                log.warning(
                    "%s: market data degraded (feed disconnected) — new entries, staged-"
                    "entry continuation, hedge repair and adjustment are suppressed until "
                    "recovery",
                    self.label,
                )
                self._notify(
                    "market_data_degraded", f"{self.label}: feed disconnected at {notice.at}"
                )
            return
        if notice.kind == "resubscribed":
            self._resubscribed_at = notice.at
            self._fresh_since_resubscribe = set()

    def _record_tick_for_gate(self, tick: Tick) -> None:
        """A no-op unless the gate is latched *and* this outage's own
        resubscribe has already been confirmed — true for the overwhelming
        majority of ticks. Clears the gate once every currently-required
        security id has its own tick at or after that resubscribe instant —
        never inferred from one instrument alone."""
        if not self._market_data_degraded or self._resubscribed_at is None:
            return
        if tick.exchange_time.tzinfo is None or self._resubscribed_at.tzinfo is None:
            return  # fail closed: never trust a naive timestamp comparison
        if tick.exchange_time < self._resubscribed_at:
            return
        self._fresh_since_resubscribe.add(tick.security_id)
        if self._required_security_ids() <= self._fresh_since_resubscribe:
            self._market_data_degraded = False
            self._resubscribed_at = None
            self._fresh_since_resubscribe = set()
            log.info(
                "%s: market data recovered — every currently-required instrument has a "
                "fresh post-reconnect tick",
                self.label,
            )
            self._notify("market_data_recovered", f"{self.label}: market data recovered")

    def _required_security_ids(self) -> set[str]:
        """Everything the gate must see a fresh post-reconnect tick for
        before clearing: the underlying, plus every open and every pending
        (staged-entry/replacement/hedge-repair) leg's own contract."""
        required = {self.underlying_id}
        if self._cycle is not None:
            for leg in (*self._cycle.open_legs(), *self._cycle.pending_legs()):
                if leg.contract is not None:
                    required.add(leg.contract.security_id)
        return required

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

        self._resume_pending_entry(ts)
        if self._cycle is not None and self._cycle.state is CycleState.CRITICAL_UNRESOLVED:
            return  # an entry stage timed out with an unknown submission

        if self._cycle is not None and self._cycle.state not in TERMINAL_CYCLE_STATES:
            self._resume_pending_adjustment(ts)
            if self._cycle.state is CycleState.CRITICAL_UNRESOLVED:
                return  # the resume attempt itself found unmanageable state

        context = self._build_context(ts)
        signal = self.strategy.evaluate(cycle=self._cycle, context=context)
        if signal is None or signal.action is CycleAction.NONE:
            return
        self._apply_signal(signal, ts)

    def _resume_pending_entry(self, ts: datetime) -> None:
        """Phase 5A: cross-evaluation resume for a sequential four-leg entry
        still in progress (spec sections 5.2/5.3) — the entry-side
        counterpart of :meth:`_resume_pending_adjustment`.

        Before this method existed, ``_drive_entry`` was only ever invoked
        once, synchronously, from ``_enter_cycle`` — the instant a strategy
        first returned ``ENTER_CYCLE``. If the very first leg's dynamically
        subscribed contract had no fresh, complete quote yet,
        ``_open_leg_now`` correctly returned ``False`` and that one call
        correctly stopped, leaving the leg ``PENDING_ORDER`` for "the next
        evaluation" to retry — but nothing on any later evaluation ever
        called ``_drive_entry`` again. Every positional strategy's own
        ``evaluate`` deliberately returns ``None`` for a cycle already
        ``ENTERING``, on the documented assumption that "the engine is
        already driving this synchronously" (see
        ``WeeklyDeltaNeutralStrategy.evaluate``'s own comment) — an
        assumption this method now makes true. A cycle whose first hedge (or
        any later stage) missed its first fresh quote was previously stuck
        ``ENTERING`` forever, with no retry and no incident: the disclosed
        Phase 5 partial-entry defect this closes.

        Simply a re-entrant call into the same ``_drive_entry`` that
        ``_enter_cycle`` itself uses first — ``next_entry_role``/
        ``_advance_entry_stage`` are already idempotent and safe to call
        again on a still-in-flight stage. Restart-safe for the same reason
        ``_resume_pending_adjustment`` is: restart reconciliation
        (``positional_multi_leg_engine_worker._reconcile_leg``) already
        promotes any entry-stage ``PENDING_ORDER`` leg whose order in fact
        filled to ``OPEN`` (or leaves it ``PENDING_ORDER`` for a genuine
        retry) from authoritative order/position state before this method
        ever runs on a recovered cycle, and ``_adopt_recovered_cycle``
        already re-subscribes every pending leg's contract. Bounded, not
        resume-forever: ``_advance_entry_stage``'s own persisted
        ``entry_stage_deadline_at`` fails the stage closed once a fresh
        quote never arrives in time — see that method's own docstring.

        Safe to call every evaluation unconditionally — a no-op whenever no
        cycle is open or the cycle is not currently ``ENTERING`` (mirrors
        ``_resume_pending_adjustment``'s own "no-op on the overwhelming
        majority of evaluations" posture).
        """
        if self._cycle is None or self._cycle.state is not CycleState.ENTERING:
            return
        self._drive_entry(ts)

    def _resume_pending_adjustment(self, ts: datetime) -> None:
        """Spec section 7.3/9.5: restart must resume a uniquely safe
        pending adjustment action, never silently strand one.

        Called on **every** evaluation (Phase 3A correction — a genuine
        gap this closes, not a documented limitation): a replacement or
        hedge-repair leg left ``PENDING_ORDER`` by a prior *session* — or
        by this evaluation's own predecessor, if its attempt found no
        fresh quote yet — was previously never retried at all, because
        nothing but ``_roll_short``/``_repair_hedge`` themselves ever
        called ``_open_leg_now`` for it, and both are one-shot, called
        only at the instant the strategy first signals the roll/repair.
        A cycle whose replacement leg missed its first-attempt quote (or
        whose process simply restarted before the fill was confirmed)
        was therefore stuck for that role forever, with
        ``WeeklyDeltaNeutralStrategy._maybe_adjust`` finding the same
        non-``OPEN`` leg via ``Cycle.current_leg`` and silently returning
        ``None`` on every subsequent evaluation — no retry, no incident.
        This method is the missing retry driver, mirroring
        ``_drive_entry``'s own cross-evaluation retry of a pending entry
        leg. A no-op on the overwhelming majority of evaluations, where
        nothing is pending.
        """
        cycle = self._cycle
        assert cycle is not None
        pending_role = cycle.pending_adjustment_role
        pending_state = cycle.pending_adjustment_state
        if pending_role is None or pending_state is None:
            return

        if pending_state == AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value:
            # The old short is confirmed closed, but no replacement was
            # ever attempted before the process stopped — this state is
            # never the *final* one persisted by a normal, uninterrupted
            # _roll_short call (it is always immediately superseded, in
            # the same call, by REPLACEMENT_PENDING or REPLACEMENT_FAILED),
            # so finding it here can only mean a crash landed in that exact
            # gap. Auto-resuming would mean re-running the strategy's own
            # candidate search after an unknown gap in market conditions —
            # a risk-*increasing* decision this generic engine code must
            # never make unilaterally. Fail closed with a named incident
            # instead (spec section 9.5's own "block and raise a critical
            # incident for ... otherwise unmanageable exposure" — a hedge
            # with no matching short is exactly that). Checked *before* any
            # leg lookup below: by construction, this state means the old
            # short is already terminal (CLOSED) and no replacement leg
            # exists yet, so ``current_leg`` would return ``None`` here
            # every single time — that is the expected, unremarkable shape
            # of this state, never itself an anomaly to report.
            self._fail_closed_pending_adjustment(
                cycle,
                pending_role,
                f"pending adjustment for {pending_role.value} is AWAITING_NEXT_CANDLE "
                "(old short confirmed closed, no replacement was ever attempted) at "
                "process start — cannot safely auto-resume candidate selection",
            )
            return

        if pending_state in (
            AdjustmentLifecycle.EXIT_SUBMISSION_PENDING.value,
            AdjustmentLifecycle.EXIT_UNKNOWN.value,
        ):
            # The adjustment claim (counters, pending role/state) is
            # already durable either way — the only open question is what
            # actually happened to the old short's close. Restart recovery
            # (positional_multi_leg_engine_worker._reconcile_leg) has
            # *already* resolved that ambiguity from authoritative
            # order/position state before this method ever runs — a
            # genuinely irreconcilable mismatch raises
            # UnmanageableCycleState during recovery itself and this code
            # never executes at all — so ``leg.state`` here is trustworthy,
            # never guessed at fresh. Uses ``latest_leg``, not
            # ``current_leg``: the leg may already be the terminal CLOSED
            # state (reconciliation proved the close succeeded), which
            # ``current_leg`` would hide.
            leg = cycle.latest_leg(pending_role)
            if leg is None:
                self._fail_closed_pending_adjustment(
                    cycle,
                    pending_role,
                    f"pending_adjustment_state={pending_state} for role {pending_role.value} "
                    "but no matching leg exists on restart — cannot safely resume",
                )
                return
            if leg.state is LegState.OPEN:
                # The close was never actually applied (EXIT_SUBMISSION_
                # PENDING: never even attempted; EXIT_UNKNOWN: attempted
                # but reconciliation proved the position is still open) —
                # safe to attempt exactly the same close _roll_short itself
                # would attempt next, exactly once.
                closed = self._close_leg_safely(
                    leg, ts, ExitReason.ADJUSTMENT, reason_text="resumed pending adjustment"
                )
                cycle.pending_adjustment_state = (
                    AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value
                    if closed
                    else AdjustmentLifecycle.EXIT_UNKNOWN.value
                )
                self._persist_cycle()
                self._log_event(
                    "adjustment_resumed",
                    f"resumed close for {leg.leg_id} ({cycle.pending_adjustment_state})",
                    ts,
                )
                return
            if leg.state is LegState.CLOSED:
                # Reconciliation proved the close genuinely succeeded, but
                # no replacement was ever attempted — identical in every
                # respect to a crash landing in the AWAITING_NEXT_CANDLE
                # gap; fail closed the same way rather than silently
                # leaving a naked hedge with no offsetting short.
                self._fail_closed_pending_adjustment(
                    cycle,
                    pending_role,
                    f"pending adjustment for {pending_role.value} was {pending_state} but "
                    f"restart reconciliation confirmed {leg.leg_id} is CLOSED and no "
                    "replacement was ever attempted — cannot safely auto-resume candidate "
                    "selection",
                )
                return
            # Any other leg.state here (e.g. still CLOSE_SUBMISSION_UNKNOWN
            # — which recovery itself should already have resolved or
            # refused to start over) is left for an operator/controlled
            # reconciliation, never guessed at.
            return

        if pending_state != AdjustmentLifecycle.REPLACEMENT_PENDING.value:
            # A terminal outcome (REPLACEMENT_FILLED/_FAILED/_EXPIRED)
            # leaves nothing to resume.
            return

        # REPLACEMENT_PENDING: the "leg" in question is the replacement
        # itself, never yet terminal by construction of a healthy
        # ``_roll_short`` run (a FAILED outcome is always paired, in the
        # same call, with clearing pending_adjustment_role — so finding
        # REPLACEMENT_PENDING with the replacement already FAILED can only
        # mean a crash landed in that exact gap; ``current_leg`` correctly
        # returns ``None`` for it, handled by the fail-closed branch below,
        # identical in spirit to the AWAITING_NEXT_CANDLE case above).
        leg = cycle.current_leg(pending_role)
        if leg is None:
            self._fail_closed_pending_adjustment(
                cycle,
                pending_role,
                f"pending_adjustment_state={pending_state} for role {pending_role.value} "
                "but no matching leg exists on restart — cannot safely resume",
            )
            return

        if leg.state is LegState.OPEN:
            # Already filled — by an earlier evaluation's own call to this
            # same method, or by a crash that landed after the fill but
            # before the terminal pending_adjustment_state was persisted
            # (restart reconciliation's own PENDING_ORDER->OPEN promotion,
            # positional_multi_leg_engine_worker._reconcile_leg, already
            # proved this from authoritative order/position state before
            # this method ever runs). Finalize the bookkeeping only.
            cycle.pending_adjustment_state = AdjustmentLifecycle.REPLACEMENT_FILLED.value
            cycle.pending_adjustment_role = None
            self._persist_cycle()
            self._log_event(
                "adjustment_resumed", f"{leg.leg_id} already filled on restart", ts
            )
            return

        if leg.state is not LegState.PENDING_ORDER:
            # FAILED / CLOSE_SUBMISSION_UNKNOWN / anything else this method
            # cannot safely act on — left for an operator or controlled
            # reconciliation, exactly like any other unmanageable leg
            # state; never guessed at or retried blindly.
            return

        if leg.contract is not None:
            self.feed.subscribe(leg.contract.security_id)
        if not self._open_leg_now(leg, ts):
            return  # still no fresh quote; retried again next evaluation

        # _open_leg_now mutates leg.state in place; re-read it through a
        # fresh LegState(...) construction rather than the narrowed
        # `leg.state` expression above, which mypy would otherwise still
        # treat as the pre-call PENDING_ORDER literal.
        resumed_state = LegState(leg.state)
        outcome = (
            AdjustmentLifecycle.REPLACEMENT_FILLED
            if resumed_state is LegState.OPEN
            else AdjustmentLifecycle.REPLACEMENT_FAILED
        )
        cycle.pending_adjustment_state = outcome.value
        cycle.pending_adjustment_role = None
        self._persist_cycle()
        self._log_event(
            "adjustment_resumed", f"resumed pending replacement {leg.leg_id} ({outcome.value})", ts
        )

    def _fail_closed_pending_adjustment(
        self, cycle: Cycle, pending_role: LegRole, message: str
    ) -> None:
        """Shared fail-closed path for every ``_resume_pending_adjustment``
        case that cannot be safely auto-resumed: record a named incident,
        block further entries, and leave the cycle ``CRITICAL_UNRESOLVED``
        for an operator/controlled reconciliation — never guessed at."""
        self._record_incident(cycle.cycle_id, message)
        self.block_entries(
            f"cycle {cycle.cycle_id}: pending adjustment for {pending_role.value} "
            "needs operator/controlled reconciliation"
        )
        cycle.state = CycleState.CRITICAL_UNRESOLVED
        self._persist_cycle()

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
            # The contract's own actual settlement instant — never the
            # current evaluation timestamp, which would make every
            # model-fallback Greek evaluation look like it was pricing an
            # already-expired contract the moment chain-sourced Greeks ever
            # went stale (found and fixed in the gap-closing session's
            # Phase 3: a real defect, not a documented scope boundary — see
            # docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md section 11.10).
            expiry_at = expiry_settlement_at(
                self._cycle.resolved_expiry_date, timezone=self._lifecycle_policy.timezone
            )
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
                        expiry_at=expiry_at,
                    )
                except Exception:
                    continue

        return PositionalContext(
            now=ts,
            trading_date=self._lifecycle_policy.trading_date(ts),
            spot=self._spot,
            # Phase 6A: time-bounded, not merely "has ever arrived" — the
            # real defect that made this field unable to detect a stale
            # underlying at all. Fails closed on a naive or future-dated
            # tick timestamp too (common.utils.timeutils.is_fresh).
            spot_is_fresh=is_fresh(
                self._spot_fresh_at, now=ts, max_age_seconds=self._max_quote_age_seconds
            ),
            chain=chain,
            leg_greeks=leg_greeks,
            can_enter=self.session.can_enter(ts),
            is_holiday=self.session.is_holiday(ts),
            is_trading_day=self.session.is_trading_day(ts),
            greeks_service=self._greeks,
            underlying_security_id=self.underlying_id,
            underlying_segment=self.underlying_segment,
            option_segment=self.option_segment,
            margin_estimator=self._margin,
            record_pre_entry_incident=lambda message: self._record_incident("pre-entry", message),
            total_charges=self._total_cycle_charges(),
        )

    def _apply_signal(self, signal: CycleSignal, ts: datetime) -> None:
        if signal.action in _RISK_INCREASING_ACTIONS and self._market_data_degraded:
            log.info(
                "%s: risk-increasing signal %s suppressed — market data degraded",
                self.label,
                signal.action.value,
            )
            return
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
        cycle_id = cycle_id_for(
            runtime_id=self._runtime_id,
            strategy_id=self._strategy_id,
            execution_mode=self._mode,
            underlying=self.underlying_instrument,
            resolved_expiry_date=resolved_expiry,
        )
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
            if signal.margin_estimate is not None:
                self._persist_margin_snapshot(cycle_id, signal.margin_estimate, critical=True)
        except MultiLegDurabilityError:
            log.error(
                "%s: could not durably persist the intended entry before any order; refusing",
                self.label,
            )
            self._cycle = None
            return

        # Unlike the pre-effect persist above, every leg's own dynamic
        # subscription is requested per-stage, not all at once here — see
        # _advance_entry_stage, which arms and persists that stage's own
        # bounded deadline immediately before requesting it.
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
            if not self._advance_entry_stage(leg, role, ts):
                return  # no fresh quote yet, deadline not reached — resume later
            if self._cycle.state is CycleState.CRITICAL_UNRESOLVED:
                return  # this stage timed out with an unknown submission

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

    def _advance_entry_stage(self, leg: LegInstance, role: LegRole, ts: datetime) -> bool:
        """Phase 5A: the bounded staged-entry step this ``role`` is
        currently in (spec section 5.3's "bounded workflow").

        ``True`` once this role is resolved for now — filled, definitively
        FAILED, or its stage timed out (``_expire_entry_stage`` decides
        which) — so the caller may move on to derive the next role fresh.
        ``False`` only means "still waiting, no fresh quote yet, deadline
        not reached" — retried again on a later evaluation, exactly like
        ``_open_leg_now`` on its own already promised.

        The first attempt at a role arms and durably persists this stage's
        deadline *before* the dynamic subscription for its contract is ever
        requested — a crash between those two would otherwise leave a
        subscription in flight for a stage the engine no longer remembers
        timing. Every later attempt at the *same* role (this evaluation's
        resume, or a resumed cycle after a restart) checks the deadline
        *before* ever consulting the quote book: a quote that arrives after
        the deadline must never reopen or continue the entry, so once
        expired it is never even looked at.

        Phase 6A: arming/subscribing happens regardless of the market-data-
        degraded gate (pure data plumbing — the earlier the subscription is
        in flight, the sooner recovery can complete), and the deadline check
        above still runs unconditionally while degraded (a flapping feed
        must never extend the bounded-workflow guarantee indefinitely) —
        but the actual fill attempt (``_open_leg_now``, which can place a
        new order) is skipped while degraded, exactly like "no fresh quote
        yet": retried again once the gate clears.
        """
        assert self._cycle is not None
        if self._cycle.entry_stage_role is not role:
            self._cycle.entry_stage_role = role
            self._cycle.entry_stage_deadline_at = ts + timedelta(
                seconds=self._entry_leg_timeout_seconds
            )
            try:
                self._persist_cycle(critical=True)
            except MultiLegDurabilityError:
                log.error(
                    "%s: could not durably arm the entry stage for %s; refusing to "
                    "subscribe or attempt it this evaluation",
                    self.label,
                    role.value,
                )
                self._cycle.entry_stage_role = None
                self._cycle.entry_stage_deadline_at = None
                return False
            if leg.contract is not None:
                self.feed.subscribe(leg.contract.security_id)
            # entry_leg_timeout_seconds > 0, so a freshly-armed deadline is
            # always still in the future relative to ts — proceed straight
            # to the fill attempt below without re-checking it.
        else:
            if leg.contract is not None:
                self.feed.subscribe(leg.contract.security_id)
            deadline = self._cycle.entry_stage_deadline_at
            if deadline is not None and ts >= deadline:
                self._expire_entry_stage(leg, role, ts)
                return True

        if self._market_data_degraded:
            return False
        if self._open_leg_now(leg, ts):
            self._clear_entry_stage(ts)
            return True
        return False

    def _clear_entry_stage(self, ts: datetime) -> None:
        assert self._cycle is not None
        self._cycle.entry_stage_role = None
        self._cycle.entry_stage_deadline_at = None
        self._persist_cycle()

    def _expire_entry_stage(self, leg: LegInstance, role: LegRole, ts: datetime) -> None:
        """A staged-entry step's deadline has passed with no confirmed
        fill. Spec section 5.3: "a timeout or unknown response is not a
        failed order — reconcile before retrying." Reconciles against
        authoritative order/position state *before* deciding anything, so a
        genuine (if late) fill is adopted rather than discarded, and a
        genuinely ambiguous submission blocks rather than guesses."""
        assert self._cycle is not None
        outcome = self._entry_submission_outcome(leg)
        if outcome == _FILLED:
            position = self._open_position_for(leg.contract.security_id) if leg.contract else None
            if position is not None:
                leg.state = LegState.OPEN
                leg.entry_price = position.average_price
                leg.quantity = position.quantity
                leg.entry_time = ts
                leg.last_price = position.average_price
                leg.entry_correlation_id = position.entry_correlation_id
                self._persist_leg(leg)
                self._clear_entry_stage(ts)
                self._log_event(
                    "entry_stage_reconciled_filled",
                    f"{leg.leg_id} ({role.value}) had actually filled by its stage "
                    "deadline; adopted rather than failed",
                    ts,
                )
                return
            # A FILLED order row but no matching authoritative open position
            # — never guessed at; treated the same as a genuinely unknown
            # submission below.
            outcome = _SUBMISSION_UNKNOWN

        if outcome in (_NEVER_PLACED, _TERMINAL_NO_FILL):
            leg.state = LegState.FAILED
            self._persist_leg(leg)
            if leg.contract is not None:
                self.feed.unsubscribe(leg.contract.security_id)
            self._clear_entry_stage(ts)
            self._log_event(
                "entry_stage_timed_out",
                f"{leg.leg_id} ({role.value}) timed out waiting for a fresh quote "
                f"(submission_outcome={outcome}); no exposure was ever opened for it",
                ts,
            )
            return

        # UNKNOWN: this engine cannot safely tell whether an order was ever
        # submitted for this leg, let alone whether it filled — fail closed
        # exactly like _fail_closed_pending_adjustment's own posture.
        # entry_stage_role/entry_stage_deadline_at are deliberately left as
        # they are (a forensic record of exactly what timed out) — never
        # cleared, never auto-retried.
        self._record_incident(
            self._cycle.cycle_id,
            f"entry stage for {role.value} ({leg.leg_id}) timed out with an unknown "
            "order-submission outcome — cannot safely resume or unwind automatically",
        )
        self.block_entries(
            f"cycle {self._cycle.cycle_id}: entry stage {role.value} needs "
            "operator/controlled reconciliation"
        )
        self._cycle.state = CycleState.CRITICAL_UNRESOLVED
        self._persist_cycle()

    def _entry_submission_outcome(self, leg: LegInstance) -> str:
        """``NEVER_PLACED`` | ``UNKNOWN`` | ``TERMINAL_NO_FILL`` | ``FILLED``
        for ``leg``'s own entry-side order — mirrors
        ``positional_multi_leg_engine_worker._resolve_intent_outcome``
        exactly (duplicated, never imported — see this module's own
        docstring for why a runtime composition-root module is never a
        dependency of common/engine)."""
        assert self._cycle is not None
        history = self._repository.cycle_order_history(cycle_id=self._cycle.cycle_id)
        rows = [r for r in history if r["leg_id"] == leg.leg_id and r["side"] == leg.side.value]
        if not rows:
            return _NEVER_PLACED
        status = rows[-1]["order_status"]
        if status is None:
            return _SUBMISSION_UNKNOWN
        if status == "FILLED":
            return _FILLED
        if status in ("REJECTED", "CANCELLED", "EXPIRED"):
            return _TERMINAL_NO_FILL
        return _SUBMISSION_UNKNOWN

    def _open_position_for(self, security_id: str) -> Position | None:
        assert self._cycle is not None
        for position in self._repository.open_positions_for_cycle(cycle_id=self._cycle.cycle_id):
            if position.security_id == security_id:
                return position
        return None

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

        # Shorts before hedges, exactly like _exit_all (spec section 6.2/8):
        # a partial entry can genuinely have a short open here (e.g. both
        # hedges + the short put filled, the short call's own stage timed
        # out) — closing a hedge first would briefly leave that short naked.
        for leg in list(self._cycle.legs.values()):
            if leg.role in SHORT_ROLES and leg.state is LegState.OPEN:
                self._close_leg_safely(
                    leg, ts, ExitReason.STRATEGY_EXIT, reason_text="partial-entry unwind"
                )
        for leg in list(self._cycle.legs.values()):
            if leg.role in HEDGE_ROLES and leg.state is LegState.OPEN:
                self._close_leg_safely(
                    leg, ts, ExitReason.STRATEGY_EXIT, reason_text="partial-entry unwind"
                )
        for leg in list(self._cycle.legs.values()):
            if leg.role is LegRole.GENERIC and leg.state is LegState.OPEN:
                self._close_leg_safely(
                    leg, ts, ExitReason.STRATEGY_EXIT, reason_text="partial-entry unwind"
                )
        for leg in list(self._cycle.pending_legs()):
            leg.state = LegState.EXPIRED
            self._persist_leg(leg)
            if leg.contract is not None:
                self.feed.unsubscribe(leg.contract.security_id)

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

    def _total_cycle_charges(self) -> float:
        """Every real charge this cycle has accrued so far — currently
        open legs' own accrued entry charges, plus every closed leg's real
        entry+exit charges (spec section 6.1: "all_entry_adjustment_
        exit_charges", not just the open legs'). Handed to the strategy via
        ``PositionalContext.total_charges`` for its own P&L-driven exit
        decisions (found and wired in the gap-closing session's Phase 3 —
        this strategy's own P&L call previously always passed
        ``charges=0.0``; see docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md
        section 11.10)."""
        if self._cycle is None:
            return 0.0
        return self._position_charges_total() + self._repository.cycle_realized_charges(
            cycle_id=self._cycle.cycle_id
        )

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

    def _persist_margin_snapshot(
        self, cycle_id: str, estimate: MarginEstimate, *, critical: bool = False
    ) -> None:
        if self._persist_margin_snapshot_cb is None:
            return
        try:
            self._persist_margin_snapshot_cb(cycle_id, estimate)
        except Exception as exc:
            if critical:
                self.block_entries(f"critical margin snapshot persistence failed: {exc}")
                self._record_incident(cycle_id, f"critical margin snapshot persist failed: {exc}")
                raise MultiLegDurabilityError(str(exc)) from exc
            log.exception("%s: best-effort margin snapshot persistence failed", self.label)
            self._record_incident(cycle_id, f"margin snapshot persist failed: {exc}")

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
