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
import uuid
from collections.abc import Callable
from dataclasses import replace as dataclass_replace
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
from .models import OptionContract
from .multi_leg_models import (
    AdjustmentLifecycle,
    AdjustmentRequest,
    AdjustmentTarget,
    Basket,
    BasketAction,
    BasketRollState,
    BasketSignal,
    BasketStateCommit,
    LegInstance,
    LegIntent,
    LegRole,
    LegState,
    MultiLegDurabilityError,
    RollClaim,
    RollLedgerPort,
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
        record_incident: Callable[[str, str], None] | None = None,
        roll_ledger: RollLedgerPort | None = None,
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
        # Independent of persist_basket/persist_leg deliberately: a critical
        # incident (a durability failure, an unresolved close) must still be
        # recorded/alerted even when the very reason it's firing is that the
        # primary persistence path is failing. A production worker wires this
        # to a separate write path (see runtimes.intraday_options.
        # multi_leg_engine_worker._build) — an offline/test engine leaves it
        # None and incidents are logged only, never silently dropped either way
        # (see _record_incident).
        self._record_incident_cb = record_incident
        # Phase 2 (strategy-rolling-strangle-otm1). None (every existing
        # offline/test engine construction) is an explicit, supported "roll
        # ledger not available" mode — see RollLedgerPort's own docstring —
        # under which _close_adjusted_legs falls back to the single-target
        # _close_adjusted_leg it replaces, unchanged.
        self._roll_ledger = roll_ledger
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
                    # P0-3 correction: carry the leg's own entry_correlation_id
                    # (already durable on the strategy_legs row) onto the
                    # adopted OpenPosition, so an exit this same process
                    # submits still has the original entry's correlation ID
                    # available end to end — not just for legs opened fresh
                    # this run.
                    entry_correlation_id=leg.entry_correlation_id,
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
        # Correction (P0-1): the primary attempt must be durably consumed
        # *before* filter evaluation can create retry ambiguity (spec section
        # 9.2), and more generally any basket-level bookkeeping a strategy
        # touches directly (entries_consumed, day_blocked_reason, an expired
        # pending replacement) must be durable before the engine acts on
        # whatever signal (or None) came back — this is a genuine pre-effect
        # checkpoint, not observability: if it cannot be persisted, no order
        # this candle's signal implies may be submitted, because a restart
        # could then re-derive the same "consume the primary attempt" decision
        # and place a second, undetectable order. Critical, and cheap, since
        # this runs once per closed underlying candle, not per tick.
        #
        # A strategy that instead returns a typed BasketStateCommit (Phase 1's
        # command surface, generic to any multi-leg strategy — see that
        # class's own docstring) never mutates basket directly, so its
        # bookkeeping is applied and durably committed here rather than
        # merely re-persisted from whatever the strategy already wrote onto
        # `self._basket` in place. straddle_920 never sets state_commit, so
        # it always takes the unchanged plain-persist branch below.
        if strat_signal is not None and strat_signal.state_commit is not None:
            if not self._apply_state_commit(strat_signal.state_commit, ts):
                return
        else:
            try:
                self._persist_basket(critical=True)
            except MultiLegDurabilityError:
                log.error(
                    "%s: basket bookkeeping from this candle could not be durably "
                    "persisted; suppressing any signal it produced — no order will "
                    "be submitted for this candle",
                    self.label,
                )
                return

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

    # ------------------------------------------------------- state commits
    def _apply_state_commit(self, commit: BasketStateCommit, ts: datetime) -> bool:
        """Durably apply a strategy's :class:`BasketStateCommit` — the
        primary-entry/day-bookkeeping counterpart of a roll claim's own
        atomic commit (:meth:`_close_adjusted_legs_with_ledger`).

        Generic to any multi-leg strategy — no strategy-name branch. This
        completes the typed command surface :class:`BasketStateCommit`
        (migration ``0013``'s header, Phase 1) was designed for: a strategy
        built on the "never mutates basket" contract otherwise has no
        durable write path at all for the reference-spot anchor at primary
        entry (:attr:`Basket.roll_state` is read-only by design), which
        Phase 2 wired for a roll claim's own anchor
        (:attr:`~common.engine.multi_leg_models.AdjustmentRequest.anchor`)
        but not yet for this earlier, entry-time case — found while
        implementing rolling_strangle_otm1 (spec section 8 step 3).
        ``straddle_920`` never returns a ``state_commit`` (it keeps mutating
        ``basket`` directly, per its own module docstring), so this method
        is never reached on its path — zero behavioural change there.

        Returns ``True`` once every requested piece of bookkeeping is
        durable and the caller may proceed to apply the signal's own order
        effect (if any). Returns ``False`` if the commit could not be made
        durable — entries are already blocked and an incident already
        recorded, mirroring a failed critical ``_persist_basket`` exactly;
        the caller must suppress the whole signal, not just retry the
        commit.
        """
        previous_entries_consumed = self._basket.entries_consumed
        previous_block_reason = self._basket.day_blocked_reason
        if commit.consume_entry_attempt:
            self._basket.entries_consumed = True
        if commit.block_day_reason is not None:
            self._basket.day_blocked_reason = commit.block_day_reason

        def _rollback() -> None:
            self._basket.entries_consumed = previous_entries_consumed
            self._basket.day_blocked_reason = previous_block_reason

        if commit.anchor is not None and self._roll_ledger is None:
            log.error(
                "%s: state commit carries an anchor update but no roll ledger "
                "is wired; refusing (the anchor has no durable home without "
                "one) — suppressing this candle's signal entirely",
                self.label,
            )
            _rollback()
            return False

        if self._roll_ledger is None:
            # No anchor requested (checked above) — the existing plain
            # critical persist already durably commits everything else this
            # method may have set (entries_consumed, day_blocked_reason).
            try:
                self._persist_basket(critical=True)
            except MultiLegDurabilityError:
                _rollback()
                return False
        else:
            claim_group_id = f"{self._basket.basket_id}:state:{uuid.uuid4().hex[:12]}"
            claimed_at = self._now()
            try:
                # Zero targets: this reuses the roll ledger's own atomic
                # basket-row + anchor writer with no claim rows, rather than
                # duplicating that transaction's shape here.
                self._roll_ledger.commit_claims(
                    self._basket,
                    claim_group_id=claim_group_id,
                    targets=(),
                    anchor=commit.anchor,
                    claim_candle_ts=ts,
                    claimed_at=claimed_at,
                )
            except Exception as exc:
                log.error(
                    "%s: could not durably commit basket state (%s); "
                    "suppressing this candle's signal entirely",
                    self.label,
                    exc,
                )
                _rollback()
                self._block_entries(f"basket state commit failed: {exc}")
                self._record_incident(
                    self._basket.basket_id, f"basket state commit failed: {exc}"
                )
                self._rehydrate_basket()
                return False
            if commit.anchor is not None:
                self._append_local_claims(
                    claim_group_id,
                    AdjustmentRequest(targets=(), anchor=commit.anchor),
                    ts,
                    claimed_at,
                    {},
                )

        if commit.expire_replacement_for:
            self._expire_replacements(commit.expire_replacement_for)

        return True

    def _expire_replacements(self, roles: tuple[LegRole, ...]) -> None:
        """Spec section 9.4: a claim genuinely ``AWAITING_NEXT_CANDLE`` whose
        next completed candle lands at or after the cutoff must expire
        rather than attempt a replacement. Best-effort — see
        :meth:`_record_roll_outcome`'s own docstring: whichever side is
        expiring is *not* opening a new order either way, so a failure to
        durably record the expiry never risks a duplicate/undetected effect,
        only a stale read-model until the next successful write.
        """
        awaiting = AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value
        if self._roll_ledger is not None:
            roll_state = self._basket.roll_state
            for role in roles:
                claim = roll_state.active_claim(role) if roll_state is not None else None
                if claim is None or claim.lifecycle_state != awaiting:
                    continue
                self._record_roll_outcome(
                    role, claim.roll_sequence, AdjustmentLifecycle.REPLACEMENT_EXPIRED.value
                )
        else:
            # Legacy single-slot projection (no roll ledger wired) — mirrors
            # straddle_920's own cutoff-expiry assignment exactly, though
            # straddle_920 itself never reaches this path (it never sets
            # state_commit).
            if (
                self._basket.pending_replacement_role in roles
                and self._basket.pending_replacement_state == awaiting
            ):
                self._basket.pending_replacement_role = None
                self._basket.pending_replacement_state = (
                    AdjustmentLifecycle.REPLACEMENT_EXPIRED.value
                )

    # ----------------------------------------------------------- signal apply
    def _apply_signal(self, signal: BasketSignal, ts: datetime) -> None:
        if signal.action is BasketAction.ENTER_BASKET or signal.action is BasketAction.ENTER_LEG:
            self._enter_legs(signal, ts)
        elif signal.action is BasketAction.EXIT_LEG:
            self._exit_leg_signal(signal, ts)
        elif signal.action is BasketAction.EXIT_ALL:
            self._exit_all_signal(signal, ts)
        # Phase 2 (strategy-rolling-strangle-otm1): a strategy's own native
        # multi-target roll request. The legacy EXIT_LEG + ExitReason.
        # ADJUSTMENT form (straddle_920's own signal) is normalised into
        # this same call from _exit_leg_signal below — one internal path,
        # no strategy branch.
        elif signal.action is BasketAction.ADJUST_LEGS and signal.adjustment is not None:
            self._close_adjusted_legs(signal.adjustment, ts)

    def _enter_legs(self, signal: BasketSignal, ts: datetime) -> None:
        if self._entry_blocked is not None or self._basket.day_blocked_reason is not None:
            log.info("%s: entry/replacement suppressed — day is blocked", self.label)
            return
        if self._spot is None:
            log.warning("%s: no spot price yet; cannot select option contract(s)", self.label)
            return

        if signal.action is BasketAction.ENTER_LEG:
            if self._roll_ledger is None:
                # Unchanged legacy single-slot gate (P0-2): only a basket
                # genuinely AWAITING_NEXT_CANDLE may produce a replacement
                # (spec section 12.3 step 5) — the engine is the last line
                # of defence against a stray/duplicate ENTER_LEG signal,
                # independent of whatever gate the strategy itself applies.
                awaiting = AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value
                if self._basket.pending_replacement_state != awaiting:
                    log.error(
                        "%s: ENTER_LEG signal received while "
                        "pending_replacement_state=%s (expected "
                        "AWAITING_NEXT_CANDLE) — refusing to enter a replacement",
                        self.label,
                        self._basket.pending_replacement_state,
                    )
                    return
                # The one-shot replacement attempt is consumed *before* any
                # contract resolution/subscription — durably, so a crash
                # between "decided to replace" and "leg pending" cannot
                # leave this basket able to retry the replacement on a
                # later candle (spec section 12.3 step 5: the attempt
                # happens once, on this candle, never again). Critical:
                # this is the same "consume before acting" pre-effect
                # checkpoint as the primary entry's in _on_candle_close.
                self._basket.pending_replacement_role = None
                self._basket.pending_replacement_state = (
                    AdjustmentLifecycle.REPLACEMENT_PENDING.value
                )
                try:
                    self._persist_basket(critical=True)
                except MultiLegDurabilityError:
                    log.error(
                        "%s: could not durably consume the replacement attempt; "
                        "refusing to enter a replacement leg this candle",
                        self.label,
                    )
                    return
            else:
                # Phase 2: per-role/claim-group-aware gate. Every requested
                # role must have its own eligible (AWAITING_NEXT_CANDLE)
                # claim; every member's one-shot attempt is consumed
                # durably, atomically together (all-or-nothing —
                # _consume_replacement_claims), before any contract
                # resolution/subscription, and a replacement leg must never
                # coexist with the adjusted-out leg it replaces still OPEN
                # or unresolved — enforced structurally: the adjusted-out
                # leg reached CLOSED (never OPEN/CLOSE_SUBMISSION_UNKNOWN)
                # before its claim could ever reach AWAITING_NEXT_CANDLE.
                if not self._consume_replacement_claims(signal.legs):
                    return

        # Resolve every requested leg's contract *before* creating or
        # committing any of them (spec section 6.4 point 8): a primary
        # ENTER_BASKET must fail the whole entry closed if its resolved
        # legs disagree on lot size, rather than open a basket with
        # mismatched per-leg quantities. Generic to any multi-leg strategy
        # (keyed off ENTER_BASKET's own multi-leg semantics, not a
        # strategy name) — straddle_920's CE+PE are checked identically
        # and, since NIFTY CE/PE always share one lot size in both the
        # simulated and Dhan resolvers, this changes nothing about its
        # observed behaviour (see test_straddle_920_engine.py, unchanged).
        resolutions: list[tuple[LegIntent, OptionType | None, OptionContract | None]] = []
        for intent in signal.legs:
            option_type = _ROLE_TO_OPTION_TYPE.get(intent.role)
            if option_type is None:
                log.error(
                    "%s: leg role %s has no default option-type mapping; skipping",
                    self.label,
                    intent.role,
                )
                resolutions.append((intent, None, None))
                continue
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
                resolutions.append((intent, option_type, None))
                continue
            resolutions.append((intent, option_type, contract))

        if signal.action is BasketAction.ENTER_BASKET:
            resolved_lot_sizes = {
                resolved.lot_size for _, _, resolved in resolutions if resolved is not None
            }
            if len(resolved_lot_sizes) > 1:
                message = (
                    f"resolved contracts disagree on lot size {sorted(resolved_lot_sizes)}; "
                    "refusing the whole primary entry rather than open a basket with "
                    "mismatched per-leg quantities"
                )
                log.error("%s: %s", self.label, message)
                self._block_entries(message)
                self._record_incident(self._basket.basket_id, message)
                return

        for intent, option_type, resolved_contract in resolutions:
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
            if option_type is None or resolved_contract is None:
                leg.state = LegState.FAILED
                # Best-effort: no order was ever possible without a resolved
                # contract, so there is no pre-effect claim to fail closed on.
                self._persist_leg(leg)
                continue
            contract = resolved_contract
            leg.contract = contract
            leg.state = LegState.PENDING_ORDER
            try:
                # Pre-effect checkpoint (P0-1): the pending leg's identity and
                # contract must be durable *before* it is subscribed — a
                # subscription leads directly to an order on the next fresh
                # tick (_try_fill_pending -> _open_leg), so a crash here must
                # never be able to leave an order placed with no durable
                # record it was ever intended.
                self._persist_leg(leg, critical=True)
            except MultiLegDurabilityError:
                log.error(
                    "%s: could not durably persist pending leg %s (%s) before "
                    "subscribing; refusing to subscribe/enter it this candle",
                    self.label,
                    leg_id,
                    contract.symbol,
                )
                leg.state = LegState.FAILED
                continue
            self.feed.subscribe(contract.security_id)
            self._pending_by_security[contract.security_id] = leg_id
            log.info(
                "%s: pending %s entry queued: %s %s (awaiting first fresh tick)",
                self.label,
                "replacement" if intent.is_replacement else "leg",
                intent.side.value,
                contract.symbol,
            )

    def _consume_replacement_claims(self, legs: tuple[LegIntent, ...]) -> bool:
        """Phase 2: the per-role/claim-group-aware replacement gate. Every
        requested role in ``legs`` must have its own eligible
        (``AWAITING_NEXT_CANDLE``) claim; every member's one-shot attempt
        is consumed atomically together — see
        :meth:`~common.engine.multi_leg_models.RollLedgerPort.
        consume_group_replacement` for why a sequence of independent
        single-row updates is not safe here. Returns ``False`` (nothing
        consumed) if any requested role is not eligible — all-or-nothing,
        matching the legacy single-slot gate's own refusal behaviour.
        """
        assert self._roll_ledger is not None
        roll_state = self._basket.roll_state
        if roll_state is None:
            log.error(
                "%s: ENTER_LEG requested but no roll state is available; refusing",
                self.label,
            )
            return False
        eligible: list[RollClaim] = []
        awaiting = AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value
        for intent in legs:
            claim = roll_state.active_claim(intent.role)
            if claim is None or claim.lifecycle_state != awaiting:
                log.error(
                    "%s: ENTER_LEG requested for role %s with no eligible "
                    "(AWAITING_NEXT_CANDLE) claim; refusing the whole replacement "
                    "request (all-or-nothing)",
                    self.label,
                    intent.role.value,
                )
                return False
            eligible.append(claim)

        members = tuple((claim.leg_role, claim.roll_sequence) for claim in eligible)
        try:
            self._roll_ledger.consume_group_replacement(
                basket_id=self._basket.basket_id, members=members
            )
        except Exception as exc:
            log.error(
                "%s: could not durably consume %d replacement attempt(s) (%s); "
                "refusing to enter any replacement leg this candle",
                self.label,
                len(members),
                exc,
            )
            self._block_entries(f"replacement attempt consumption failed: {exc}")
            self._record_incident(
                self._basket.basket_id, f"replacement attempt consumption failed: {exc}"
            )
            return False

        pending = AdjustmentLifecycle.REPLACEMENT_PENDING.value
        for claim in eligible:
            self._update_local_claim(claim.leg_role, claim.roll_sequence, pending)
        return True

    def _open_leg(self, leg: LegInstance, price: float, ts: datetime) -> None:
        assert leg.contract is not None
        self.positions.open(
            leg.contract,
            leg.side,
            price,
            ts,
            basket_id=self._basket.basket_id,
            leg_id=leg.leg_id,
        )
        position = self.positions.get(leg.contract.security_id)
        assert position is not None
        leg.state = LegState.OPEN
        leg.entry_price = position.entry_price
        leg.quantity = position.quantity
        leg.entry_time = ts
        leg.last_price = position.entry_price
        leg.entry_correlation_id = position.entry_correlation_id
        self._pending_by_security.pop(leg.contract.security_id, None)
        # Best-effort (P0-1): the entry order has already executed
        # (positions.open() above completed) — a projection-write failure
        # here changes only what this process's own read-model shows, not
        # what happened. Recorded as an incident and reconciled by restart
        # recovery (see multi_leg_engine_worker.recover_basket), never raised
        # — there is nothing left to abort.
        self._persist_leg(leg)
        self._maybe_capture_original_basis()
        self._maybe_record_replacement_filled(leg)
        self._notify("fill", f"{leg.side.value} {leg.contract.symbol} @ {price:.2f}")

    def _maybe_record_replacement_filled(self, leg: LegInstance) -> None:
        """Spec section 9.5 / ``AdjustmentLifecycle``'s own docstring (item
        7): a replacement leg's confirmed fill advances its originating
        claim from ``REPLACEMENT_PENDING`` to ``REPLACEMENT_FILLED`` —
        completing the one transition Phase 2 left unwired. Generic (no
        strategy-name branch): any replacement leg for any multi-leg
        strategy using the roll ledger reaches this, including
        ``straddle_920``'s own — found while implementing
        rolling_strangle_otm1's own end-to-end replacement test, since
        Phase 2's suite never exercised a replacement fill's roll-ledger
        outcome this far. Best-effort, like every other post-fill
        bookkeeping write here: the fill already happened durably through
        the order/fill tables regardless of whether this write lands.
        """
        if self._roll_ledger is None or not leg.is_replacement or leg.replaces_leg_id is None:
            return
        roll_state = self._basket.roll_state
        if roll_state is None:
            return
        pending = AdjustmentLifecycle.REPLACEMENT_PENDING.value
        for claim in roll_state.claims:
            if (
                claim.leg_role is leg.role
                and claim.target_leg_id == leg.replaces_leg_id
                and claim.lifecycle_state == pending
            ):
                self._record_roll_outcome(
                    leg.role,
                    claim.roll_sequence,
                    AdjustmentLifecycle.REPLACEMENT_FILLED.value,
                    replacement_leg_id=leg.leg_id,
                )
                return

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
            # Normalise the legacy single-target signal into the generic
            # AdjustmentRequest form — the same claim machinery a native
            # ADJUST_LEGS signal drives (Phase 2). straddle_920 keeps
            # emitting this exact EXIT_LEG/ADJUSTMENT signal unchanged.
            request = AdjustmentRequest(
                targets=(AdjustmentTarget(leg_id=leg.leg_id, role=leg.role),)
            )
            self._close_adjusted_legs(request, ts)
        else:
            self._close_leg_safely(
                leg, leg.last_price if leg.last_price is not None else 0.0, ts, reason
            )

    def _close_adjusted_leg(self, leg: LegInstance, ts: datetime) -> None:
        """The corrected adjustment-close state machine (P0-2, spec section
        12.3 steps 1-4), in the required order:

        1. durably claim the sole adjustment (``adjustment_count`` + 1)
        2. persist that this leg's exit must now be resolved
           (``EXIT_SUBMISSION_PENDING``) — 1 and 2 land in one durable
           checkpoint, critical: if this cannot be persisted, the leg stays
           untouched and open, and the day is blocked, rather than risking a
           second, undetectable adjustment on restart.
        3. submit/reconcile the close (:meth:`_close_leg_safely`)
        4. only a *confirmed* closing fill may reach
           ``AWAITING_NEXT_CANDLE`` — the only state
           :meth:`_enter_legs` will accept a replacement from. An unresolved
           close instead reaches ``EXIT_UNKNOWN`` and blocks entries, so an
           open-or-unknown adjusted leg can never coexist with a newly
           entered replacement (correction requirement 6).
        """
        self._basket.adjustment_count += 1
        self._basket.pending_replacement_role = leg.role
        self._basket.pending_replacement_state = AdjustmentLifecycle.EXIT_SUBMISSION_PENDING.value
        try:
            self._persist_basket(critical=True)
        except MultiLegDurabilityError:
            log.error(
                "%s: could not durably claim the day's one adjustment; leg %s "
                "stays open, no close attempted",
                self.label,
                leg.leg_id,
            )
            return

        closed = self._close_leg_safely(
            leg, leg.last_price if leg.last_price is not None else 0.0, ts, ExitReason.ADJUSTMENT
        )
        if closed:
            self._basket.pending_replacement_state = AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value
        else:
            self._basket.pending_replacement_state = AdjustmentLifecycle.EXIT_UNKNOWN.value
            # _close_leg_safely already blocked entries and recorded the
            # incident for the leg itself; this basket-level state is what
            # _enter_legs' AWAITING_NEXT_CANDLE gate reads, so it must be set
            # even though entries are already blocked for the day regardless.
        # Best-effort: the close either genuinely happened (a real fill — the
        # trade is already durable via the order/fill tables) or definitively
        # did not resolve (CLOSE_SUBMISSION_UNKNOWN, itself already made
        # durable — best-effort — inside _close_leg_safely). This write is
        # bookkeeping about *which* of those two happened, not the trading
        # event itself.
        self._persist_basket()

    # --------------------------------------------------- durable roll claims
    # Phase 2 (strategy-rolling-strangle-otm1). Generic — no strategy-name
    # branch. See RollLedgerPort's own docstring for the None-wired
    # fallback contract, and migration 0013's header for the full design.
    def _find_resumable_claim_group(
        self, request: AdjustmentRequest
    ) -> tuple[str, dict[str, RollClaim]] | None:
        """If every target in ``request`` already has a matching ``CLAIMED``
        claim (same ``target_leg_id``, same role) sharing one
        ``claim_group_id``, return that id and the per-leg claim mapping so
        the caller can resume it instead of claiming fresh. ``None`` means
        the normal, fresh-claim path applies.

        Only a ``CLAIMED`` match is resumable — see the call site's own
        comment for why ``EXIT_SUBMISSION_PENDING`` must never reach this
        method's "resume" treatment. A partial match (some targets have one,
        some do not) or a match spanning more than one ``claim_group_id`` is
        refused (``None``, logged) rather than guessed at — ``commit_claims``'s
        own group-wide atomicity means a genuine crash cannot produce either
        situation; if one is observed, it is a code defect, not a race.
        """
        roll_state = self._basket.roll_state
        if roll_state is None:
            return None
        matches: dict[str, RollClaim] = {}
        for target in request.targets:
            claim = roll_state.active_claim(target.role)
            if claim is None or claim.target_leg_id != target.leg_id:
                continue
            if claim.lifecycle_state != AdjustmentLifecycle.CLAIMED.value:
                log.error(
                    "%s: adjustment target %s already has an active claim in "
                    "state %s (expected CLAIMED to safely resume, or nothing at "
                    "all) — refusing rather than risk a duplicate reservation",
                    self.label,
                    target.leg_id,
                    claim.lifecycle_state,
                )
                return None
            matches[target.leg_id] = claim
        if not matches:
            return None
        if len(matches) != len(request.targets):
            log.error(
                "%s: %d of %d requested roll targets already have an active CLAIMED "
                "claim and %d do not — refusing to resume a partial group (commit_"
                "claims' own atomicity means this should not arise from a genuine "
                "crash; treating as an inconsistency)",
                self.label,
                len(matches),
                len(request.targets),
                len(request.targets) - len(matches),
            )
            return None
        group_ids = {claim.claim_group_id for claim in matches.values()}
        if len(group_ids) != 1:
            log.error(
                "%s: requested roll targets resolve to more than one existing "
                "claim_group_id (%s) — refusing to resume",
                self.label,
                sorted(group_ids),
            )
            return None
        return next(iter(group_ids)), matches

    def _close_adjusted_legs(self, request: AdjustmentRequest, ts: datetime) -> None:
        if not request.targets:
            return
        if self._roll_ledger is None:
            if len(request.targets) != 1:
                log.error(
                    "%s: a %d-target adjustment was requested but no roll ledger is "
                    "wired; refusing (only a single-target roll is representable "
                    "without one — see RollLedgerPort)",
                    self.label,
                    len(request.targets),
                )
                return
            target = request.targets[0]
            leg = self._basket.legs.get(target.leg_id)
            if leg is None or leg.state is not LegState.OPEN or leg.contract is None:
                return
            self._close_adjusted_leg(leg, ts)
            return
        self._close_adjusted_legs_with_ledger(request, ts)

    def _close_adjusted_legs_with_ledger(self, request: AdjustmentRequest, ts: datetime) -> None:
        """The durable, repeated-roll-capable claim/close/reconcile flow
        (Phase 2 required lifecycle, sections 1-4):

        1. every target is validated before anything changes; the atomic
           claim (every target's row, the anchor, and the basket
           compatibility projection) commits in one transaction, or none of
           it does;
        2. each target is then reserved (durably, atomically associated
           with its own claim row) and submitted independently;
        3. a confirmed fill reaches EXIT_CONFIRMED; a definitively
           rejected/cancelled close reaches FAILED (leg stays OPEN, budget
           consumed, no replacement); anything else reaches EXIT_UNKNOWN
           (leg marked CLOSE_SUBMISSION_UNKNOWN, entries blocked, never
           retried on the strength of an open position);
        4. the group advances to AWAITING_NEXT_CANDLE only once every
           target is EXIT_CONFIRMED — one FAILED/EXIT_UNKNOWN target blocks
           replacement for the whole group.
        """
        assert self._roll_ledger is not None
        legs: list[LegInstance] = []
        for target in request.targets:
            leg = self._basket.legs.get(target.leg_id)
            if leg is None or leg.state is not LegState.OPEN or leg.contract is None:
                log.error(
                    "%s: adjustment target %s is not a currently open leg; refusing "
                    "the whole claim group (all-or-nothing)",
                    self.label,
                    target.leg_id,
                )
                return
            legs.append(leg)

        # A crash after an earlier attempt's claim commits, but before its
        # reservation, must resume that same claim — never consume another
        # roll for it. Only a CLAIMED match is resumable this way: a match
        # already at EXIT_SUBMISSION_PENDING means its close was already
        # reserved/authorised, and must be resolved (via reconciliation's
        # own close_intent_id lookup, never re-reserved here) rather than
        # re-attempted — reaching that state at this point indicates
        # startup reconciliation was skipped or failed, a problem worth
        # refusing loudly rather than risking a duplicate reservation for.
        resumable = self._find_resumable_claim_group(request)
        if resumable is not None:
            group_id, claims_by_leg = resumable
            log.info(
                "%s: resuming existing roll claim group %s for %d target(s) — no "
                "new claim, no roll count consumed again",
                self.label,
                group_id,
                len(request.targets),
            )
            for target, leg in zip(request.targets, legs, strict=True):
                self._close_one_roll_target(
                    leg, target, group_id, claims_by_leg[target.leg_id].roll_sequence, ts
                )
            self._maybe_advance_claim_group(group_id)
            return

        claim_group_id = f"{self._basket.basket_id}:{uuid.uuid4().hex[:12]}"
        claimed_at = self._now()

        # Speculative in-memory mutation of the scalar compatibility
        # projection — generalises _close_adjusted_leg's own `+= 1` to N
        # targets. Rolled back below if the durable commit fails.
        previous_count = self._basket.adjustment_count
        previous_role = self._basket.pending_replacement_role
        previous_state = self._basket.pending_replacement_state
        self._basket.adjustment_count += len(request.targets)
        self._basket.pending_replacement_role = (
            request.targets[0].role if len(request.targets) == 1 else None
        )
        self._basket.pending_replacement_state = AdjustmentLifecycle.EXIT_SUBMISSION_PENDING.value

        try:
            assigned = self._roll_ledger.commit_claims(
                self._basket,
                claim_group_id=claim_group_id,
                targets=request.targets,
                anchor=request.anchor,
                claim_candle_ts=ts,
                claimed_at=claimed_at,
            )
        except Exception as exc:
            log.error(
                "%s: could not durably claim %d roll target(s) (%s); no close will "
                "be attempted this candle; entries blocked",
                self.label,
                len(request.targets),
                exc,
            )
            self._basket.adjustment_count = previous_count
            self._basket.pending_replacement_role = previous_role
            self._basket.pending_replacement_state = previous_state
            self._block_entries(f"roll claim commit failed: {exc}")
            self._record_incident(self._basket.basket_id, f"roll claim commit failed: {exc}")
            self._rehydrate_basket()
            return

        self._append_local_claims(claim_group_id, request, ts, claimed_at, assigned)

        for target, leg in zip(request.targets, legs, strict=True):
            self._close_one_roll_target(leg, target, claim_group_id, assigned[target.leg_id], ts)

        self._maybe_advance_claim_group(claim_group_id)

    def _close_one_roll_target(
        self,
        leg: LegInstance,
        target: AdjustmentTarget,
        claim_group_id: str,
        roll_sequence: int,
        ts: datetime,
    ) -> None:
        assert self._roll_ledger is not None
        assert leg.contract is not None
        price = leg.last_price if leg.last_price is not None else 0.0
        try:
            reserved = self.positions.reserve_close(
                leg.contract.security_id,
                price,
                ts,
                ExitReason.ADJUSTMENT,
                leg_role=target.role.value,
                roll_sequence=roll_sequence,
                basket_id=self._basket.basket_id,
                leg_id=leg.leg_id,
            )
        except Exception as exc:
            # The reservation's own atomic write failed — nothing was
            # authorised, so the row stays CLAIMED, safely resumable (never
            # NEVER_PLACED-then-retried blindly; see migration 0013's
            # header). No close was ever attempted for this target.
            log.error(
                "%s: could not durably reserve the roll close for %s (%s); leg "
                "stays open, claim remains CLAIMED and resumable",
                self.label,
                leg.leg_id,
                exc,
            )
            self._block_entries(f"roll close reservation failed for {leg.leg_id}: {exc}")
            self._record_incident(
                self._basket.basket_id, f"roll close reservation failed for {leg.leg_id}: {exc}"
            )
            return

        close_intent_id = reserved.close_intent_id
        try:
            trade = self.positions.submit_close(reserved)
        except Exception as exc:
            outcome = "UNKNOWN"
            if close_intent_id is not None:
                try:
                    outcome = self._roll_ledger.resolve_close_intent(close_intent_id)
                except Exception:
                    log.exception(
                        "%s: could not resolve the roll close intent's authoritative "
                        "outcome for %s",
                        self.label,
                        leg.leg_id,
                    )
            self._resolve_roll_target_failure(leg, target, roll_sequence, outcome, exc)
            return

        leg.state = LegState.CLOSED
        leg.exit_price = trade.exit_price
        leg.exit_time = ts
        leg.exit_reason = ExitReason.ADJUSTMENT
        leg.exit_correlation_id = trade.exit_correlation_id
        leg.realized_gross_pnl = trade.gross_pnl
        self._persist_leg(leg)
        if self._daily_guard is not None:
            self._daily_guard.register_trade(trade.net_pnl)
        self.feed.unsubscribe(leg.contract.security_id)
        self._notify(
            "exit",
            f"{trade.side.value} {trade.contract.symbol} @ {trade.exit_price:.2f} "
            f"reason=ADJUSTMENT gross={trade.gross_pnl:.2f}",
        )
        self.strategy.on_leg_closed(trade, leg, self._basket)
        self._record_roll_outcome(
            target.role, roll_sequence, AdjustmentLifecycle.EXIT_CONFIRMED.value
        )

    def _resolve_roll_target_failure(
        self,
        leg: LegInstance,
        target: AdjustmentTarget,
        roll_sequence: int,
        outcome: str,
        exc: Exception,
    ) -> None:
        if outcome == "FILLED":
            # Rare race: the close may genuinely have landed despite the
            # in-process exception. Conservative and safe: do not fabricate
            # a Trade here — degrade to the unresolved branch below so
            # nothing retries it; a restart's own reconciliation reads the
            # authoritative fill directly and fully resolves it.
            outcome = "UNKNOWN"
        if outcome == "TERMINAL_NO_FILL":
            # Authoritative proof the close did not happen. The leg was
            # never mutated above — it remains OPEN, untouched, exactly as
            # a later hard square-off's own (separate, non-roll) close
            # attempt on it requires (spec section 10.1/target-leg-id
            # multi-attempt reconciliation).
            self._record_roll_outcome(target.role, roll_sequence, AdjustmentLifecycle.FAILED.value)
            log.warning(
                "%s: roll close for %s was definitively rejected/cancelled; leg "
                "remains OPEN, roll budget consumed, no replacement for this claim",
                self.label,
                leg.leg_id,
            )
            return
        # UNKNOWN: never retried merely because the position is still open.
        # Mark the leg itself unresolved too — mirrors _close_leg_safely
        # exactly — so nothing, including a later square-off sweep,
        # attempts a second close on it.
        leg.state = LegState.CLOSE_SUBMISSION_UNKNOWN
        self._persist_leg(leg)
        self._record_roll_outcome(
            target.role, roll_sequence, AdjustmentLifecycle.EXIT_UNKNOWN.value
        )
        self._block_entries(f"leg {leg.leg_id} roll close outcome unknown: {exc}")
        self._record_incident(
            self._basket.basket_id,
            f"leg {leg.leg_id} roll close raised and its outcome is unknown: {exc}",
        )

    def _maybe_advance_claim_group(self, claim_group_id: str) -> None:
        roll_state = self._basket.roll_state
        if roll_state is None:
            return
        members = roll_state.claims_for_group(claim_group_id)
        if not members:
            return
        confirmed = AdjustmentLifecycle.EXIT_CONFIRMED.value
        if any(claim.lifecycle_state != confirmed for claim in members):
            return  # a FAILED/EXIT_UNKNOWN (or not-yet-processed) member blocks the group
        for claim in members:
            self._record_roll_outcome(
                claim.leg_role,
                claim.roll_sequence,
                AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value,
            )
        self._basket.pending_replacement_role = members[0].leg_role if len(members) == 1 else None
        self._basket.pending_replacement_state = AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value
        self._persist_basket()

    def _record_roll_outcome(
        self,
        role: LegRole,
        roll_sequence: int,
        lifecycle_state: str,
        *,
        replacement_leg_id: str | None = None,
    ) -> None:
        """Best-effort: the trading outcome this records has already
        happened (a confirmed fill, a definitive rejection, ...) — a
        failure to durably persist the record must never undo or reverse
        it, matching :meth:`_persist_leg`'s own non-critical write
        philosophy exactly."""
        self._update_local_claim(
            role, roll_sequence, lifecycle_state, replacement_leg_id=replacement_leg_id
        )
        if self._roll_ledger is None:
            return
        try:
            self._roll_ledger.record_outcome(
                basket_id=self._basket.basket_id,
                leg_role=role,
                roll_sequence=roll_sequence,
                lifecycle_state=lifecycle_state,
                replacement_leg_id=replacement_leg_id,
            )
        except Exception as exc:
            log.exception(
                "%s: could not durably record roll outcome %s for %s roll #%d "
                "(best-effort)",
                self.label,
                lifecycle_state,
                role.value,
                roll_sequence,
            )
            self._record_incident(
                self._basket.basket_id,
                f"roll outcome record failed ({role.value} #{roll_sequence} -> "
                f"{lifecycle_state}): {exc}",
            )

    def _append_local_claims(
        self,
        claim_group_id: str,
        request: AdjustmentRequest,
        ts: datetime,
        claimed_at: datetime,
        assigned: dict[str, int],
    ) -> None:
        """Mirror freshly-committed CLAIMED rows onto ``self._basket.roll_state``
        locally, so the rest of this call (and any later same-day read)
        sees them without a full reload. ``assigned`` (from
        :meth:`RollLedgerPort.commit_claims`) is the authoritative
        ``target_leg_id -> roll_sequence`` mapping — never recomputed here."""
        roll_state = self._basket.roll_state
        if roll_state is None:
            roll_state = BasketRollState(reference_price=None, anchor_candle_ts=None)
        reference_price = (
            request.anchor.price if request.anchor is not None else roll_state.reference_price
        )
        anchor_candle_ts = (
            request.anchor.candle_ts if request.anchor is not None else roll_state.anchor_candle_ts
        )
        new_claims = list(roll_state.claims)
        for target in request.targets:
            new_claims.append(
                RollClaim(
                    claim_group_id=claim_group_id,
                    leg_role=target.role,
                    roll_sequence=assigned[target.leg_id],
                    lifecycle_state=AdjustmentLifecycle.CLAIMED.value,
                    target_leg_id=target.leg_id,
                    close_correlation_id=None,
                    close_intent_id=None,
                    replacement_leg_id=None,
                    reference_price_at_claim=reference_price,
                    claim_candle_ts=ts,
                    claimed_at=claimed_at,
                )
            )
        self._basket.roll_state = BasketRollState(
            reference_price=reference_price, anchor_candle_ts=anchor_candle_ts,
            claims=tuple(new_claims),
        )

    def _update_local_claim(
        self,
        role: LegRole,
        roll_sequence: int,
        lifecycle_state: str,
        *,
        replacement_leg_id: str | None = None,
    ) -> None:
        roll_state = self._basket.roll_state
        if roll_state is None:
            return
        new_claims = []
        for claim in roll_state.claims:
            if claim.leg_role is role and claim.roll_sequence == roll_sequence:
                claim = dataclass_replace(
                    claim,
                    lifecycle_state=lifecycle_state,
                    replacement_leg_id=(
                        replacement_leg_id
                        if replacement_leg_id is not None
                        else claim.replacement_leg_id
                    ),
                )
            new_claims.append(claim)
        self._basket.roll_state = BasketRollState(
            reference_price=roll_state.reference_price,
            anchor_candle_ts=roll_state.anchor_candle_ts,
            claims=tuple(new_claims),
        )

    def _rehydrate_basket(self) -> None:
        """Re-fetch durable state after a critical roll-claim write
        failure: in-memory state must never diverge from what is actually
        durable. Reuses the same ``recover_basket`` callable
        :meth:`_adopt_recovered_basket` uses at startup — calling it again
        mid-session is safe, a pure read+reconcile against the repository
        with no side effect of its own. A ``None`` result (nothing durable
        to adopt — should not happen here, since a basket already exists)
        or a callback that itself fails leaves the in-memory basket as is,
        with entries already blocked by the caller."""
        if self._recover_basket is None:
            return
        try:
            recovered = self._recover_basket()
        except UnmanageableBasketState:
            raise
        except Exception:
            log.exception(
                "%s: could not rehydrate basket state after a roll claim commit "
                "failure; continuing with the in-memory view, entries remain blocked",
                self.label,
            )
            return
        if recovered is not None:
            self._basket = recovered

    def _exit_all_signal(self, signal: BasketSignal, ts: datetime) -> None:
        reason = signal.exit_reason or ExitReason.STRATEGY_EXIT
        self._basket.day_blocked_reason = signal.reason or reason.value
        # Best-effort, deliberately: this only records *why* the basket is
        # blocked; the actual closes below happen unconditionally regardless
        # of whether this bookkeeping write succeeds — never let an
        # observability write block an emergency exit.
        self._persist_basket()
        self._close_all(ts, reason)

    def _close_leg(self, leg: LegInstance, price: float, ts: datetime, reason: ExitReason) -> None:
        """Raising primitive: closes ``leg`` or raises.

        Callers that must never duplicate-close and must never propagate a
        gateway failure use :meth:`_close_leg_safely` instead — this exists
        underneath it (and is exercised directly by tests) so a genuine
        close failure is observable rather than swallowed here.
        """
        if leg.contract is None:
            raise RuntimeError(f"leg {leg.leg_id} has no resolved contract to close")
        trade = self.positions.close(
            leg.contract.security_id,
            price,
            ts,
            reason,
            basket_id=self._basket.basket_id,
            leg_id=leg.leg_id,
        )
        leg.state = LegState.CLOSED
        leg.exit_price = trade.exit_price
        leg.exit_time = ts
        leg.exit_reason = reason
        leg.exit_correlation_id = trade.exit_correlation_id
        leg.realized_gross_pnl = trade.gross_pnl
        # Best-effort (P0-1): the closing fill has already happened
        # (positions.close() above completed) — same reasoning as
        # _open_leg's persist.
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

    def _close_leg_safely(
        self, leg: LegInstance, price: float, ts: datetime, reason: ExitReason
    ) -> bool:
        """Attempt to close ``leg``; never raises.

        Returns ``True`` on a confirmed close, ``False`` if the close's
        outcome could not be established — in which case ``leg`` is left in
        :attr:`~common.engine.multi_leg_models.LegState.CLOSE_SUBMISSION_UNKNOWN`
        (never plain ``OPEN``, so nothing — including a later
        :meth:`_close_all` sweep — retries the close automatically and risks
        a duplicate) and the day is blocked (correction requirement 8: never
        issue a duplicate close when the first close's outcome is unknown).
        """
        if leg.contract is None:
            return False
        try:
            self._close_leg(leg, price, ts, reason)
            return True
        except Exception as exc:
            log.exception(
                "%s: closing leg %s raised — its outcome cannot be "
                "established; marking CLOSE_SUBMISSION_UNKNOWN rather than "
                "retrying automatically",
                self.label,
                leg.leg_id,
            )
            leg.state = LegState.CLOSE_SUBMISSION_UNKNOWN
            # Best-effort: there is no pending action left to abort here (the
            # close attempt already happened, ambiguously) — unlike a
            # pre-effect checkpoint, blocking entries below does not depend
            # on this write succeeding.
            self._persist_leg(leg)
            self._block_entries(f"leg {leg.leg_id} close outcome unknown: {exc}")
            self._record_incident(
                self._basket.basket_id,
                f"leg {leg.leg_id} close raised and its outcome is unknown: {exc}",
            )
            return False

    def _close_all(self, ts: datetime, reason: ExitReason) -> None:
        # _close_leg_safely never raises, so one leg's unresolved close
        # (P0-2) can never prevent an attempt on the rest of the basket —
        # required for square-off/shutdown to still reduce whatever exposure
        # it safely can.
        for leg in list(self._basket.open_legs()):
            price = leg.last_price if leg.last_price is not None else 0.0
            self._close_leg_safely(leg, price, ts, reason)
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
        # Best-effort, deliberately: a failed persist here must never
        # suppress the actual square-off below — reducing real exposure
        # always takes priority over recording that it is happening.
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
    def _persist_basket(self, *, critical: bool = False) -> None:
        """Persist the current basket projection.

        ``critical=True`` marks a *pre-effect* checkpoint (P0-1): a durable
        claim that must exist before an irreversible action is allowed to
        proceed. On failure it blocks further entries/adjustments and raises
        :class:`~common.engine.multi_leg_models.MultiLegDurabilityError`,
        which the caller must catch to abort *before* taking that action.

        ``critical=False`` (the default) is a best-effort projection write —
        used only after the actual trading event (an order, a fill) has
        already happened durably through the order/fill tables — logged and
        reported as an incident on failure, never raised.

        No-ops silently if no ``persist_basket`` callback was configured at
        all: "no persistence configured" (an offline/test engine) is a
        distinct, supported mode from "persistence configured but failing".
        """
        if self._persist_basket_cb is None:
            return
        try:
            self._persist_basket_cb(self._basket)
        except Exception as exc:
            if critical:
                log.error(
                    "%s: CRITICAL — could not durably persist basket state (%s); "
                    "blocking further entries/adjustments until resolved",
                    self.label,
                    exc,
                )
                self._block_entries(f"basket persistence failure: {exc}")
                self._record_incident(
                    self._basket.basket_id, f"critical basket persist failed: {exc}"
                )
                raise MultiLegDurabilityError(
                    f"failed to durably persist basket {self._basket.basket_id}: {exc}"
                ) from exc
            log.exception("%s: could not persist basket projection (best-effort)", self.label)
            self._record_incident(
                self._basket.basket_id, f"basket projection persist failed (non-critical): {exc}"
            )

    def _persist_leg(self, leg: LegInstance, *, critical: bool = False) -> None:
        """Persist one leg's projection — see :meth:`_persist_basket` for the
        ``critical`` contract, identical here at leg granularity."""
        if self._persist_leg_cb is None:
            return
        try:
            self._persist_leg_cb(leg)
        except Exception as exc:
            if critical:
                log.error(
                    "%s: CRITICAL — could not durably persist leg %s (%s); "
                    "blocking further entries/adjustments until resolved",
                    self.label,
                    leg.leg_id,
                    exc,
                )
                self._block_entries(f"leg {leg.leg_id} persistence failure: {exc}")
                self._record_incident(
                    self._basket.basket_id,
                    f"critical leg {leg.leg_id} persist failed: {exc}",
                )
                raise MultiLegDurabilityError(
                    f"failed to durably persist leg {leg.leg_id}: {exc}"
                ) from exc
            log.exception(
                "%s: could not persist leg %s projection (best-effort)", self.label, leg.leg_id
            )
            self._record_incident(
                self._basket.basket_id,
                f"leg {leg.leg_id} projection persist failed (non-critical): {exc}",
            )

    def _record_incident(self, basket_id: str, message: str) -> None:
        """Independent, best-effort incident path (P0-1) — always logged at
        CRITICAL regardless of whether a ``record_incident`` callback was
        configured, and never lets a failure in that callback itself
        propagate (there is nothing further to fail closed on beyond what the
        caller has already done)."""
        log.critical("%s: INCIDENT basket=%s: %s", self.label, basket_id, message)
        if self._record_incident_cb is None:
            return
        try:
            self._record_incident_cb(basket_id, message)
        except Exception:
            log.exception(
                "%s: could not record incident through the independent path either", self.label
            )

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
