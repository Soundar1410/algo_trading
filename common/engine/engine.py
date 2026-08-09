"""Trading engine — orchestrates one strategy process.

Ported from the reference repository's ``framework/execution/engine.py``
(Phase 3 Part 2b-i). Wires together: market-data feed -> candle builder ->
strategy -> option selection -> position manager -> risk manager -> report ->
notifier. The engine only ever talks to the abstract
:class:`~common.engine.strategy.BaseStrategy` / :class:`~common.engine.risk.
RiskManager` interfaces, so it needs no strategy-specific code — a strategy fully
owns its entry/exit conditions, option selection, BUY/SELL side, sizing and risk
policy.

Tick routing
------------
* **Underlying** ticks build candles (interval from ``cfg.timeframe``). On candle
  close the strategy is consulted for a signal.
* **Option** ticks (for an open or pending position) drive risk management (stop
  loss / profit lock / trailing / target) and pending-entry fills.

Timing
------
* New entries only inside ``[start, end)`` (``MarketSession.can_enter``).
* Open positions are managed until the hard square-off, when any position is
  force-closed and the feed is stopped. A shutdown request also forces square-off
  before the feed stops — a position must never be left unmanaged.

Shutdown, and who owns the signal
---------------------------------
The engine installs **no signal handler** — deviation D18, and the reason Part 2b
could not begin as a straight port. The reference installed
``signal.signal(SIGINT, ...)`` inside :meth:`TradingEngine.run`, nested inside the
supervisor's own handler from Phase 3 Part 1. Installing second means winning
delivery, so the supervisor's ordered feed shutdown would have been reached only
via the engine's re-raise, if at all.

Instead the engine is *told*: :meth:`TradingEngine.request_square_off` is
thread-safe, sets an event and returns. The engine acts on it at boundaries owned
by the thread already running it —

1. :meth:`TradingEngine.on_tick`, the boundary that matters. It runs on the feed's
   own callback thread, which under the Part 1 contract (see
   :mod:`common.market_data.adapter`) is the only thread permitted to close the
   connection. This is the Block 2 capture-script fix reused: move the stop
   decision into the callback the feed's own loop invokes.
2. :meth:`TradingEngine.run`'s ``finally``, for the case where the feed returns
   before any tick could carry the request into ``on_tick``. Safe there because
   ``feed.run()`` has returned, so no other thread is inside the engine.

A connected feed that delivers nothing and never returns offers neither boundary,
so the engine cannot square off — the engine-level twin of runbook limitation 13.
:meth:`TradingEngine.wait_until_stopped` exists so the owner bounds its wait and
reports that, rather than blocking forever.

The engine is dependency-injected (feed, gateway-backed position manager,
selector, strategy, ...), so the same orchestration runs offline or, in
Part 2b-ii, against the shared feed and the persisted order lifecycle.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from common.candles.builder import CandleBuilder, to_ohlc
from common.indicators.base import OHLC
from common.logging import get_logger
from common.models import Candle, ExitReason, Tick
from common.notifications import NotificationEvent, Notifier, NullNotifier, SafeNotifier
from common.utils.timeutils import now_ist, parse_timeframe_minutes
from common.warmup.requirements import validate_warmup_config

if TYPE_CHECKING:
    # Annotation only, per Phase 4 Part 4 -- a real (non-TYPE_CHECKING) import
    # here would cycle: common.warmup.manager imports common.engine.session,
    # and this module is what common/engine/__init__.py imports to reach
    # .session in the first place. The runtime seam stays duck-typed, as it
    # was before this part -- only the annotation is concrete now.
    from common.warmup.manager import WarmupManager
    from common.warmup.source import WarmupSource

from .config import EngineConfig
from .daily_guard import DailyRiskConfig, DailyRiskGuard, DailyRiskRecovery
from .feed import MarketDataFeed
from .models import (
    AdoptedPosition,
    OpenPosition,
    OptionContract,
    OrderSide,
    SignalAction,
    StrategySignal,
)
from .positions import PositionManager
from .regime import build_regime_tagger
from .reporting import (
    EngineReporter,
    NullReporter,
    NullReportWriter,
    ReportWriter,
    summarise,
)
from .risk import opt_float
from .selection import OptionSelector
from .session import MarketSession
from .square_off import SessionSquareOffAuthority, SquareOffAuthority
from .strategy import BaseStrategy

log = get_logger(__name__)


class TradingEngine:
    def __init__(
        self,
        cfg: EngineConfig,
        *,
        feed: MarketDataFeed,
        option_selector: OptionSelector,
        strategy: BaseStrategy,
        position_manager: PositionManager,
        underlying_security_id: str,
        underlying_instrument: str = "",
        runtime_id: str = "engine",
        notifier: Notifier | None = None,
        notify_deferred: bool = True,
        persist_notification_failure: Callable[[NotificationEvent, str], None] | None = None,
        history_provider: Callable[[], list[Candle]] | None = None,
        reporter: EngineReporter | None = None,
        report: ReportWriter | None = None,
        warmup_manager: WarmupManager | None = None,
        warmup_source: WarmupSource | None = None,
        square_off_event: threading.Event | None = None,
        square_off_authority: SquareOffAuthority | None = None,
        recover_position: Callable[[], AdoptedPosition | None] | None = None,
        recover_daily_risk: Callable[[], DailyRiskRecovery | None] | None = None,
        recover_exit_state: Callable[[], dict[str, Any] | None] | None = None,
        persist_exit_state: Callable[[dict[str, Any] | None, str | None], None] | None = None,
        persist_position_marks: Callable[[float, float], None] | None = None,
        clock: Callable[[], datetime] = now_ist,
    ) -> None:
        self.cfg = cfg
        self.feed = feed
        self.selector = option_selector
        self.strategy = strategy
        self.positions = position_manager
        self.report: ReportWriter = report or NullReportWriter()
        # Wrapped once, here. A notification channel must never be able to abort a
        # trade or a square-off; the reference hand-rolled a try/except around one
        # of its five notify calls, and this repository already has the wrapper
        # that guarantees it for all of them.
        #
        # **Reused, not re-wrapped, when the caller already handed in a
        # SafeNotifier.** worker.py builds exactly one SafeNotifier per worker
        # process and passes it straight through into this constructor on the
        # engine path (via engine_worker.run_engine); wrapping it again here
        # would silently double every count, apply two independent aggregation
        # windows to the same event, and mean a caller's `deferred=True` (the
        # property that actually matters — see `notify_deferred` below) lives
        # on an *inner* SafeNotifier that this class never touches directly.
        # A caller that hands in a bare Notifier (every offline/unit-test
        # construction of this class) gets the fallback wrap, as before.
        self.notifier = (
            notifier
            if isinstance(notifier, SafeNotifier)
            else SafeNotifier(
                notifier or NullNotifier(),
                # Defaults True: this notifier's send() is reached from
                # on_tick (see _notify below), and a slow channel must never
                # stall tick processing. Only relevant for the fallback wrap —
                # a caller supplying an already-built SafeNotifier decided its
                # own deferred-ness when it built it.
                deferred=notify_deferred,
                on_failure=persist_notification_failure,
            )
        )
        self._runtime_id = runtime_id
        self._reporter: EngineReporter = reporter or NullReporter()
        self.underlying_id = underlying_security_id
        self.underlying_instrument = underlying_instrument or underlying_security_id
        # Optional zero-arg callable returning today's already-closed candles to
        # prime the indicator on startup. None (offline/tests, or warm-up
        # disabled) => cold start.
        self.history_provider = history_provider
        self._warmup_manager = warmup_manager
        self._warmup_source = warmup_source

        self.session = MarketSession(cfg.session)
        # Who decides square-off. The default reads the session clock, exactly as
        # this class did inline before Part 2b-ii-B-1, so an offline run is
        # unchanged. A worker injects the persisted-state implementation, which is
        # the only one that survives a restart — see common.engine.square_off.
        self._square_off: SquareOffAuthority = square_off_authority or SessionSquareOffAuthority(
            self.session
        )
        # Optional zero-arg callable returning a position a previous process left
        # open, for this one to manage rather than re-enter. None (offline/tests, and
        # every run with no prior state) => nothing to adopt. See _start_day.
        self._recover_position = recover_position
        # Optional zero-arg callable returning today's already-booked daily P&L and
        # trade count, for the daily guard to resume from rather than re-zero. None
        # (offline/tests, and every run with no prior state) => nothing to restore.
        # Phase 6 Part 1 — see _restore_daily_risk.
        self._recover_daily_risk = recover_daily_risk
        # Optional zero-arg callable returning a previous process's exit-policy
        # snapshot for the adopted position, and an optional callable this engine
        # calls with the current snapshot (or None to clear it) after every candle
        # a position is open for. Both None (offline/tests, and every run with no
        # persistence layer) => the strategy's own reset() state is all there is,
        # exactly as before Phase 6 Part 2. See _restore_exit_state, _persist_exit_state.
        # The callable's second argument (Phase 6 Part 3) is the candle boundary
        # just processed, written in the *same* call as the exit-state payload
        # rather than a second transaction at the same checkpoint — see
        # _persist_exit_state.
        self._recover_exit_state = recover_exit_state
        self._persist_exit_state_cb = persist_exit_state
        # Optional callable this engine calls with the open position's current
        # (max_favorable_pnl, max_adverse_pnl) at the same checkpoint as the
        # exit-state write, Phase 6 Part 3. None => marks are not persisted
        # incrementally (offline/tests) — a restart then restores them at
        # whatever the positions row's own fill-time value was (0.0, if never
        # touched), exactly as before this part existed. See _persist_position_marks.
        self._persist_position_marks_cb = persist_position_marks
        # The last candle this strategy-day fully processed before a restart,
        # restored (only when a position was actually adopted) from
        # AdoptedPosition.last_candle_end_at -- Phase 6 Part 3. None => no
        # candle is skipped, exactly as before this part existed.
        self._last_processed_candle_end: datetime | None = None
        interval = parse_timeframe_minutes(cfg.timeframe)
        self.candles = CandleBuilder(
            interval,
            security_id=self.underlying_id,
            instrument=self.underlying_instrument,
        )
        self._lots = position_manager.lots

        # Per-position premium (traded-instrument) candle stream — opt-in via
        # strategy.needs_option_candles, so strategies that don't ask for it (the
        # default) pay zero cost and see no behaviour change. Same interval as the
        # underlying; rebuilt fresh on every entry/re-entry (see _open/_close) so a
        # stale candle never leaks across contracts.
        self._option_candles: CandleBuilder | None = (
            CandleBuilder(interval) if getattr(strategy, "needs_option_candles", False) else None
        )
        self._option_candle_contract_id: str | None = None
        self._last_option_tick_ts: datetime | None = None
        self._premium_gap_logged = False
        self._premium_candle_interval = interval

        # Label used in heartbeat logs; a multi-track runner can override this with
        # the track name to tell the streams apart.
        self.label = strategy.name

        self._spot: float | None = None
        self._pending: OptionContract | None = None  # awaiting first option tick
        self._pending_side: OrderSide | None = None
        self._squared_off = False

        # Shutdown coordination. The event is injectable so the process's single
        # signal owner can hand in the one it already holds, rather than this
        # class growing a second way to hear about a shutdown.
        self._square_off_requested = square_off_event or threading.Event()
        self._stopped = threading.Event()
        self._shutdown_reason = "external request"
        self._stopped_by_request = False
        self._now = clock
        # Set when a required historical warm-up did not return WARMED. Entry side
        # only, day-latched, cleared solely by _start_day.
        self._entry_blocked: str | None = None

        # Market Regime Engine: observes the underlying candles (the same ones the
        # strategy sees) and tags each trade with the detected regime. A no-op null
        # tagger unless enabled — never affects trading.
        self._regime = build_regime_tagger(cfg)

        # Optional strategy-wide daily loss cap (max_daily_loss_percent of
        # starting_capital). None => disabled. Enforced on live MTM (realised +
        # open) so it can trip mid-trade, square off, and halt new entries for the
        # day. Independent of the per-position risk manager.
        self._daily_guard = self._build_daily_guard(cfg)

        # Fail fast on a config that would cold-start an indicator that cannot be
        # cold-started (see common.warmup.requirements) -- checks both the config
        # flag and, since Phase 4 Part 4, whether a real warm-up manager was
        # actually supplied.
        validate_warmup_config(strategy, cfg, warmup_manager)

    @staticmethod
    def _build_daily_guard(cfg: EngineConfig) -> DailyRiskGuard | None:
        pct = opt_float(cfg.max_daily_loss_percent)
        if pct is None or pct <= 0:
            return None
        capital = float(cfg.starting_capital)
        return DailyRiskGuard(DailyRiskConfig(daily_max_loss=pct / 100.0 * capital))

    # -------------------------------------------------------------- shutdown
    def request_square_off(self, reason: str = "external request") -> None:
        """Ask the engine to square off and stop at its next boundary.

        **Safe from any thread.** It sets a flag and returns: it never touches the
        broker, the feed or position state, because those belong to the thread
        running the engine — the same ownership rule the feed contract states in
        :mod:`common.market_data.adapter`.

        That restraint is the whole design. A caller that squared off here would
        be mutating positions from an interrupted stack while the feed thread is
        inside :meth:`on_tick`, and would then call ``feed.stop()`` cross-thread —
        which for a live adapter is an unbounded wait on a loop only the blocked
        owner can advance. The reference engine's ``SIGINT`` handler did exactly
        that; see the module docstring and runbook deviation D18.
        """
        self._shutdown_reason = reason
        self._square_off_requested.set()

    @property
    def square_off_requested(self) -> bool:
        """Whether a shutdown has been asked for. Says nothing about it happening."""
        return self._square_off_requested.is_set()

    @property
    def stopped_by_request(self) -> bool:
        """Whether the run actually ended by honouring a square-off request.

        Replaces the reference's ``raise KeyboardInterrupt`` tail. A worker returns
        an outcome rather than an exit code derived from an exception, and a
        shutdown that was asked for and completed is an orderly end, not a failure.
        """
        return self._stopped_by_request

    def wait_until_stopped(self, timeout: float | None = None) -> bool:
        """Block until the run loop has finished. ``False`` means it has not.

        The bounded half of the residual case: a feed delivering nothing and never
        returning leaves the engine with no boundary at which to square off, so an
        owner must be able to give up and report rather than wait forever.
        """
        return self._stopped.wait(timeout)

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        """Connect, start a fresh day, stream ticks until square-off, then report.

        Interrupt safety: some feeds catch ``KeyboardInterrupt`` internally to
        close their socket gracefully and simply *return* rather than let the
        exception propagate — from here that is indistinguishable from a transient
        disconnect. That is why a shutdown is not inferred from the feed returning:
        it is carried by the event :meth:`request_square_off` sets, checked at
        every tick and once more when the feed returns.
        """
        self._stopped.clear()
        self._start_day()
        self._warm_up()
        # Phase 9: tell the strategy warm-up is over, whatever it did (real
        # replay, cold start, or opted out) — see BaseStrategy.on_warmup_complete.
        # After _warm_up() and before the first live tick, so a day-scoped
        # detector reset here can never see a warm-up candle nor miss a live one.
        self.strategy.on_warmup_complete()
        self.feed.on_tick(self.on_tick)
        self._reporter.start()
        log.info(
            "engine running mode=%s strategy=%s side=%s timeframe=%s",
            self.cfg.execution_mode.value,
            self.strategy.name,
            self.strategy.trade_side.value,
            self.cfg.timeframe,
        )

        try:
            self.feed.run()
        except (KeyboardInterrupt, SystemExit):
            # Not a handler — a safety net for a feed that *does* propagate. Kept
            # exactly as the reference had it.
            log.warning("interrupted; forcing square-off before shutdown")
            self._handle_square_off(self._now())
            raise
        except Exception as exc:  # must still square off & report
            self._notify("engine_error", str(exc))
            log.exception("unhandled error; forcing square-off before shutdown")
            self._handle_square_off(self._now())
            raise
        finally:
            # The last boundary. If a square-off was requested but no tick ever
            # arrived to carry it into on_tick, this is where it is honoured — and
            # it is safe here precisely because feed.run() has returned, so no
            # other thread is inside the engine.
            if self._square_off_requested.is_set():
                self._stopped_by_request = True
                self._handle_square_off(self._now())
            self._end_day()
            self._reporter.stopped(self.positions.positions, self.positions.trades)
            self._stopped.set()

    def _warm_up(self) -> None:
        """Replay today's already-closed candles through the strategy so its
        indicator holds the correct current trend before the first live candle.

        Signals produced during replay are intentionally discarded: those entries
        belong to earlier moments whose prices are gone, so we never trade on
        historical candles — we only rebuild indicator *state* so the *next* live
        flip is detected correctly. Any failure degrades gracefully to a cold
        start: warm-up must never prevent the strategy from trading.
        """
        try:
            spec = self.strategy.warmup_spec()
        except Exception as exc:  # fails closed just below
            # NOT the same as warmup_spec() returning None. None is a deliberate
            # opt-out ("no history needed, cold start is fine"); a raise means we
            # cannot tell whether this strategy is continuity-required. Treating
            # both as None let a SuperTrend strategy trade cold through the one
            # path the gate did not cover, so this fails closed instead.
            log.error(
                "%s: warmup_spec() raised (%s); cannot establish whether a cold "
                "start is safe, so entries fail closed.",
                self.label,
                exc,
            )
            self._block_entries(
                "cannot determine the warm-up requirement (warmup_spec() raised) — "
                "unable to establish that a cold start is safe"
            )
            return

        if (
            self._warmup_manager is not None
            and self._warmup_source is not None
            and spec is not None
        ):
            interval = parse_timeframe_minutes(self.cfg.timeframe)

            def _sink(candle: Candle) -> None:
                ohlc = to_ohlc(candle)
                self._regime.observe(ohlc)
                self.strategy.on_candle(ohlc, candle.start_at)

            result = self._warmup_manager.warm(
                _sink,
                self._warmup_source,
                spec,
                session=self.session,
                timeframe_minutes=interval,
            )
            log.info("%s indicator warm-up: %s — %s", self.label, result.status, result.detail)
            if spec.entry_blocked_by(result.status):
                self._block_entries(f"required warm-up returned {result.status} — {result.detail}")
            return

        # Below here: no warm-up manager -> the today's-history fallback. It is
        # still a cold start, and today's-history replay cannot establish
        # continuity across the overnight gap anyway, so say so loudly rather than
        # let the output look trustworthy.
        if spec is not None and spec.continuity_required:
            log.warning(
                "%s: cold start — no warm-up manager injected, and the "
                "today's-history fallback cannot establish continuity across the "
                "overnight gap. This strategy is continuity-required, so signals "
                "from this run may be manufactured by the cold seed and must not "
                "be read as strategy edge.",
                self.label,
            )

        if self.history_provider is None:
            return
        try:
            candles = self.history_provider() or []
        except Exception as exc:  # warm-up is best-effort
            log.warning(
                "historical warm-up unavailable (%s); cold-starting the indicator. "
                "The first live signal may lag a bot that ran since the open.",
                exc,
            )
            return

        if not candles:
            log.info("no prior candles to warm up from today; cold-starting the indicator")
            return

        for c in candles:
            ohlc = to_ohlc(c)
            # Warm the regime classifier from history too, so early-session trades
            # get a real regime rather than UNCLASSIFIED.
            self._regime.observe(ohlc)
            self.strategy.on_candle(ohlc, c.start_at)  # signal deliberately ignored

        last = candles[-1]
        log.info(
            "warm-up complete: replayed %d candle(s) up to %s | %s",
            len(candles),
            last.start_at.strftime("%H:%M"),
            self.strategy.status(),
        )

    @property
    def entries_blocked(self) -> str | None:
        """Why new entries are refused for the rest of the day, or ``None``."""
        return self._entry_blocked

    def block_entries(self, reason: str) -> None:
        """Refuse new entries for the rest of the day. Safe to call repeatedly.

        The public half of the latch below, added in Part 2b-ii-B-1 so the feed
        layer can report a dropped tick without reaching into a private. A drop
        means this worker's own candles may be silently wrong (runbook limitation
        14 / deviation D23), and trading on bars that might be wrong is worse than
        not trading.
        """
        self._block_entries(reason)

    def _block_entries(self, reason: str) -> None:
        """Latch entries off for the rest of the day.

        Entry side only -- an open position keeps its exits and square-off, so a
        block can never trap one. Alerting never gates the block: the latch is set
        first, then announced, so a disabled or throwing notifier cannot turn
        trading back on.
        """
        if self._entry_blocked is not None:
            return
        self._entry_blocked = reason
        msg = f"BLOCKED: {reason} — no new entries today"
        log.error("%s %s", self.label, msg)
        self._notify("entries_blocked", f"{self.label} {msg}")

    def _start_day(self) -> None:
        """Fresh state each day, ignoring prior signals/positions."""
        self.strategy.reset()
        self.strategy.risk_manager.reset()
        self.candles.reset()
        self._regime.reset()
        self._spot = None
        self._pending = None
        self._pending_side = None
        self._squared_off = False
        # A new day is the ONLY thing that clears a warm-up block; _warm_up() runs
        # straight after this and may set it again.
        self._entry_blocked = None
        # State must never leak between days (spec section 12) -- cleared
        # unconditionally here, before _adopt_recovered_position() below might
        # set it from a genuinely-adopted position's own last_candle_end_at.
        self._last_processed_candle_end = None
        if self._daily_guard is not None:
            self._daily_guard.reset()
            self._restore_daily_risk()
        if self._option_candles is not None:
            self._option_candles.reset()
            self._option_candle_contract_id = None
            self._last_option_tick_ts = None
            self._premium_gap_logged = False
        self.feed.subscribe(self.underlying_id)
        # Last, and only here. Everything above resets; adopting before any of it
        # would be undone by risk_manager.reset(), and adopting after _warm_up()
        # would let the strategy see candles while the engine still believed it was
        # flat.
        self._adopt_recovered_position()
        log.info("new trading day initialised (fresh state)")

    def _restore_daily_risk(self) -> None:
        """Seed the daily guard from what a previous process already booked today.

        Phase 6 Part 1. Runs after :meth:`~common.engine.daily_guard.DailyRiskGuard.reset`
        and before :meth:`_adopt_recovered_position`, so a day that had already hit
        its loss cap or trade limit before a restart stays halted rather than
        re-opening for one more trade — and so an adopted position is managed under
        the *correct* halt state, not a fresh one. Without this, a worker restarting
        after a loss began the day's loss cap from zero on every restart; nothing
        read the ``daily_realised_pnl`` this repository already persisted.

        A provider that **raises** fails closed, exactly as :meth:`_adopt_recovered_position`
        does for a position: we cannot establish today's already-booked P&L and
        trade count, and trading a fresh cap against an unknown true one is the
        outcome worth preventing. Only new entries are affected — nothing here
        touches exits or square-off.
        """
        if self._daily_guard is None or self._recover_daily_risk is None:
            return
        try:
            recovered = self._recover_daily_risk()
        except Exception as exc:
            log.exception(
                "%s: could not establish today's already-booked daily risk state", self.label
            )
            self._block_entries(
                f"daily-risk restart recovery failed ({exc}) — unable to establish "
                "today's already-booked realised P&L and trade count"
            )
            return
        if recovered is None:
            return
        halt_reason = self._daily_guard.restore(recovered)
        if halt_reason is not None:
            log.warning(
                "%s: restored daily risk state is already past a limit (%s) — "
                "no new entries today",
                self.label,
                halt_reason,
            )

    def _adopt_recovered_position(self) -> None:
        """Take over a position a previous process opened, placing no order.

        Phase 3 Part 2b-ii-B-2. Three things happen together, and all three are the
        engine's rather than the caller's, because only the engine knows the order
        they have to happen in:

        1. the position enters the book via
           :meth:`~common.engine.positions.PositionManager.adopt`, which touches no
           gateway — placing an order here would double the exposure recovery exists
           to prevent;
        2. the traded contract is subscribed, on the same path the underlying just
           was, so its premium ticks arrive and its exits can fire;
        3. the risk manager is armed, which has to be *after* ``_start_day``'s
           ``reset()`` and so cannot be done by whoever supplied the position.

        A provider that **raises** fails closed, exactly as a raising
        ``warmup_spec()`` does: we cannot establish whether a position is carried
        over, and entering a new one while an old one is open is the outcome worth
        preventing. Exits and square-off are untouched, so the block can never trap a
        position — including one that was adopted.
        """
        if self._recover_position is None:
            return
        try:
            recovered = self._recover_position()
        except Exception as exc:
            log.exception("%s: could not establish whether a position was carried over", self.label)
            self._block_entries(
                f"restart recovery failed ({exc}) — unable to establish whether a "
                "position from a previous run is still open"
            )
            return
        if recovered is None:
            return

        self.positions.adopt(
            recovered.contract,
            recovered.side,
            recovered.lots,
            recovered.entry_price,
            recovered.entry_time,
            entry_charges=recovered.entry_charges,
            last_price=recovered.last_price,
            max_favorable_pnl=recovered.max_favorable_pnl,
            max_adverse_pnl=recovered.max_adverse_pnl,
        )
        self.feed.subscribe(recovered.contract.security_id)
        self.strategy.risk_manager.new_position(recovered.lots, entry_price=recovered.entry_price)
        if self._option_candles is not None:
            # A fresh premium stream for the adopted contract. Its history is gone
            # with the process that built it, so the first exit needing consecutive
            # bars re-primes rather than comparing across the restart.
            self._option_candles.retarget(security_id=recovered.contract.security_id)
            self._option_candle_contract_id = recovered.contract.security_id
            self._last_option_tick_ts = recovered.entry_time
            self._premium_gap_logged = False
        self._restore_exit_state(recovered.contract.security_id)
        # Phase 6 Part 3. Only set when a position was actually adopted, matching
        # the write side's own "only while a position is open" gate -- a day with
        # nothing to adopt guards against nothing, exactly as before this part.
        self._last_processed_candle_end = recovered.last_candle_end_at
        log.info(
            "adopted %s %s x%d @ %.2f from a previous run; no order placed",
            recovered.side.value,
            recovered.contract.symbol,
            recovered.lots,
            recovered.entry_price,
        )
        self._notify(
            "position_recovered",
            f"adopted {recovered.side.value} {recovered.contract.symbol} "
            f"@ {recovered.entry_price:.2f} from a previous run",
        )

    def _restore_exit_state(self, adopted_security_id: str) -> None:
        """Reapply a previous process's exit-policy snapshot for the adopted position.

        Phase 6 Part 2. Deliberately **fail-open**, unlike position and daily-risk
        recovery: a missing, foreign or unusable snapshot degrades exit timing
        quality (a trailing stop re-arms from the current price instead of its
        true peak), never safety — no entry is at stake here, only how well an
        *already-adopted* position is managed. So a problem is logged and the
        strategy's already-:meth:`~common.engine.strategy.BaseStrategy.reset`
        (empty) exit state is left in place, exactly the philosophy
        :meth:`~common.engine.positions.PositionManager.adopt` already documents
        for MFE/MAE restarting at zero.
        """
        if self._recover_exit_state is None:
            return
        try:
            snapshot = self._recover_exit_state()
        except Exception:
            log.exception(
                "%s: could not restore exit-policy state; continuing with fresh "
                "(reset) state — exit timing may differ from before the restart",
                self.label,
            )
            return
        if not snapshot:
            return
        stored_security_id = snapshot.get("security_id")
        if stored_security_id != adopted_security_id:
            log.warning(
                "%s: exit-state snapshot names %r but the adopted position is %r; "
                "the snapshot is stale and is being ignored",
                self.label,
                stored_security_id,
                adopted_security_id,
            )
            return
        state = snapshot.get("state")
        if not isinstance(state, dict):
            log.warning(
                "%s: exit-state snapshot carries no usable state; ignoring it", self.label
            )
            return
        try:
            self.strategy.restore_exit_state(state)
        except Exception:
            log.exception(
                "%s: restore_exit_state raised; continuing with fresh (reset) state",
                self.label,
            )

    def _persist_exit_state(self, candle_end_at: datetime | None = None) -> None:
        """Write the strategy's current exit-policy state, or clear it once flat.

        Phase 6 Part 2. Called after every candle (underlying and premium) that
        could have changed exit-policy state while a position is open, and once
        more when a position closes, to clear the key so a later, different
        contract can never inherit a stale snapshot (mirrors
        ``LifecycleGateway._record_contract``'s clear-on-close for
        ``OPEN_POSITION_KEY``). A no-op if no callback was injected — every
        offline/test construction, unchanged from before this part.

        ``candle_end_at`` (Phase 6 Part 3, optional) is the candle boundary just
        processed, written in the **same** call as the exit-state payload — the
        callback threads it straight to ``merge_payload``'s own
        ``last_candle_end_at`` parameter, so this stays one write per checkpoint
        rather than two. ``None`` from the close-time caller: closing does not
        represent a new candle boundary being processed in the sense this guards,
        and once flat there is nothing left for the guard to protect anyway (see
        ``_on_candle_close``'s and ``_check_premium_candle_exit``'s guards).

        A failure to write is logged, not raised, for the same reason
        ``_record_contract`` gives for the same choice: this is an observability
        write, and turning it into an exception here would abandon a position
        that genuinely exists and is otherwise being managed correctly.
        """
        if self._persist_exit_state_cb is None:
            return
        pos = self._current_position()
        payload = (
            None
            if pos is None
            else {
                "security_id": pos.contract.security_id,
                "state": self.strategy.exit_state_snapshot(),
            }
        )
        try:
            self._persist_exit_state_cb(
                payload, None if candle_end_at is None else candle_end_at.isoformat()
            )
        except Exception:
            log.exception(
                "%s: could not persist exit-policy state; a restart may re-arm "
                "this position's exit rules from scratch",
                self.label,
            )

    def _persist_position_marks(self) -> None:
        """Write the open position's running MFE/MAE excursion. Phase 6 Part 3.

        Same checkpoint as :meth:`_persist_exit_state`, and for the same
        reason: a mid-position crash must not lose an excursion already
        observed in memory. No-op if no callback was injected, or nothing is
        open — unlike exit-state there is nothing to *clear* on close, since
        ``highest_favourable``/``lowest_favourable`` are plain columns on the
        position's own (now ``CLOSED``) row, not a cross-contract-leakable
        blob key, and the final values are exactly the audit trail a closed
        trade's MFE/MAE should carry.
        """
        if self._persist_position_marks_cb is None:
            return
        pos = self._current_position()
        if pos is None:
            return
        try:
            self._persist_position_marks_cb(pos.max_favorable_pnl, pos.max_adverse_pnl)
        except Exception:
            log.exception(
                "%s: could not persist position marks; a restart may lose the "
                "excursion already observed",
                self.label,
            )

    def _persist_open_position_checkpoint(self, candle_end_at: datetime) -> None:
        """Everything that must survive a restart while a position is open.

        Phase 6 Part 3, extending Part 2's single exit-state write into the one
        shared per-candle checkpoint all three quantities (exit state, MFE/MAE,
        last-processed-candle) now use. No new call sites and no new write
        frequency beyond what limitation 24 already scopes — same reasoning as
        the choice to gate ``last_candle_end_at`` on "position open" rather than
        write it unconditionally.

        **Guarded here, once, on "is anything open" — not by each caller.**
        ``_on_candle_close``'s "no signal"/"same leg" branches reach this call
        whether or not a position exists (idle days included), and without this
        guard a flat day would still write a clearing exit-state payload and
        advance the watermark on every candle — exactly the unconditional
        per-candle write the position-gated design was chosen specifically to
        avoid. Close-time clearing is unaffected: ``_close()`` calls
        :meth:`_persist_exit_state` directly, not through here.
        """
        if not self.positions.has_position():
            return
        self._persist_exit_state(candle_end_at)
        self._persist_position_marks()

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
        # A requested shutdown takes precedence over everything, including the
        # session clock. This runs on the feed's own callback thread, which is the
        # one thread permitted to close the connection — which is why the decision
        # lives here rather than wherever the request came from.
        if self._square_off_requested.is_set():
            self._shutdown(tick.exchange_time)
            return

        # Hard square-off takes precedence over everything else. The authority,
        # not the session clock: only it can tell "15:25 and nothing done yet" from
        # "15:25 and the process that died at 15:16 already closed the book".
        if self._square_off.due(tick.exchange_time):
            self._handle_square_off(tick.exchange_time)
            return

        if tick.security_id == self.underlying_id:
            self._on_underlying_tick(tick)
        else:
            self._on_option_tick(tick)

        # Publish live state + any newly-closed trades.
        self._reporter.beat(self.positions.positions, self.positions.trades)

    def _on_underlying_tick(self, tick: Tick) -> None:
        self._spot = tick.last_price
        if not self.session.is_open(tick.exchange_time):
            return
        completed = self.candles.add(tick.last_price, tick.exchange_time)
        if completed is not None:
            self._check_premium_candle_gap(tick.exchange_time)
            bar = to_ohlc(completed)
            if bar.spans_gap:
                # Runbook limitation 4, Phase 4 Part 3. This bar's OHLC was
                # stitched across a hole in the tick stream, so it is not fed to
                # indicators and produces no signal. The hub discards such a bar
                # outright; this builder cannot (D23), so the bar exists and the
                # engine declines to trade on it instead.
                log.warning(
                    "[CANDLE_GAP] %s bar %s-%s was built across a hole in the tick "
                    "stream; skipping indicators and any signal for it. Exits, stop "
                    "loss and square-off are unaffected.",
                    self.label,
                    completed.start_at.isoformat(),
                    completed.end_at.isoformat(),
                )
                self.strategy.on_candle_gap(bar, tick.exchange_time)
                return
            self._on_candle_close(bar, tick.exchange_time)

    def _check_premium_candle_gap(self, ts: datetime) -> None:
        """Detect a stalled option-tick feed for the currently tracked contract.

        Runs on every underlying candle close (a reliable, regular checkpoint) so a
        broken/absent subscription for the traded instrument is reported loudly
        instead of silently starving the premium-candle exit of data or falling
        back to the underlying candle. Other exits (stop loss, daily loss cap,
        square-off) are untouched by this — only the premium-candle exit is skipped
        for candles evaluated while the gap persists.
        """
        if self._option_candles is None or self._option_candle_contract_id is None:
            return
        if self._premium_gap_logged:
            return
        gap_ok = self._last_option_tick_ts is not None and (
            ts - self._last_option_tick_ts
        ) <= timedelta(minutes=self._premium_candle_interval)
        if not gap_ok:
            self._premium_gap_logged = True
            log.error(
                "[PREMIUM_CANDLE_GAP] %s instrument=%s last_tick=%s now=%s — no option "
                "ticks received for the tracked contract within the last candle "
                "interval; the premium-candle exit is skipped until fresh ticks "
                "resume (stop loss / daily loss cap / square-off remain active).",
                self.label,
                self._option_candle_contract_id,
                self._last_option_tick_ts,
                ts,
            )

    def _check_premium_candle_exit(self, tick: Tick) -> ExitReason | None:
        """Feed this option tick into the per-position premium candle builder (a
        no-op unless the strategy opted in via ``needs_option_candles`` and this
        tick belongs to the currently tracked contract) and, on candle close,
        consult the strategy's premium-candle exit hook. Never evaluates a forming
        candle — only fires from a value ``CandleBuilder.add()`` returns."""
        if self._option_candles is None or tick.security_id != self._option_candle_contract_id:
            return None
        last_tick = self._last_option_tick_ts
        if last_tick is not None and tick.exchange_time - last_tick > timedelta(
            minutes=self._premium_candle_interval
        ):
            if not self._premium_gap_logged:
                log.error(
                    "[PREMIUM_CANDLE_GAP] %s instrument=%s last_tick=%s now=%s — "
                    "resetting the incomplete candle; consecutive-candle exits will "
                    "re-prime on fresh bars.",
                    self.label,
                    self._option_candle_contract_id,
                    last_tick,
                    tick.exchange_time,
                )
            self._option_candles.reset()
            self.strategy.on_option_candle_gap()
        self._last_option_tick_ts = tick.exchange_time
        self._premium_gap_logged = False
        completed = self._option_candles.add(tick.last_price, tick.exchange_time)
        if completed is None:
            return None
        if (
            self._last_processed_candle_end is not None
            and tick.exchange_time <= self._last_processed_candle_end
        ):
            # Phase 6 Part 3: a replay landing on (or before) a candle this
            # strategy-day already fully processed before a restart. Refused
            # outright — not even consulting the strategy — so a
            # session-cumulative indicator (or the exit-policy state below)
            # cannot double-count what it already saw.
            log.info(
                "%s: skipping an already-processed premium candle at %s (restored "
                "watermark %s)",
                self.label,
                tick.exchange_time.isoformat(),
                self._last_processed_candle_end.isoformat(),
            )
            return None
        strat_signal = self.strategy.on_option_candle(to_ohlc(completed), tick.exchange_time)
        if strat_signal is not None and strat_signal.action is SignalAction.EXIT:
            # No persist call here: the caller closes the position on this
            # return value, and _close() clears the exit-state key itself once
            # it does — persisting the pre-close state here would just be
            # overwritten a moment later.
            return strat_signal.exit_reason or ExitReason.STRATEGY_EXIT
        # Still open: this candle may have advanced exit-policy state (a peak, a
        # streak) or the MFE/MAE excursion without firing — persist both, and
        # this candle as the new watermark, so a restart resumes from all three.
        self._persist_open_position_checkpoint(tick.exchange_time)
        return None

    def _on_option_tick(self, tick: Tick) -> None:
        pos = self.positions.get(tick.security_id)
        if pos is not None:
            pos.update_price(tick.last_price)
            reason = self.strategy.risk_manager.on_pnl(pos.unrealised_pnl)
            if reason is None:
                strat_signal = self.strategy.on_position_tick(pos, tick)
                if strat_signal is not None and strat_signal.action is SignalAction.EXIT:
                    reason = ExitReason.STRATEGY_EXIT
            # Strategy-wide daily loss cap on live MTM (realised + this open
            # position): trips mid-trade, squares off, and halts entries below.
            # Flattened from the reference's nested ifs; identical short-circuit
            # order, so check_open_mtm is still called only when nothing has
            # already decided to exit.
            if (
                reason is None
                and self._daily_guard is not None
                and self._daily_guard.check_open_mtm(pos.unrealised_pnl)
            ):
                reason = self._daily_guard.square_off_reason
            # Combined candle-structure exit on the traded instrument's own premium
            # candle (opt-in; see needs_option_candles). Lower priority than the
            # hard risk exits above.
            if reason is None:
                reason = self._check_premium_candle_exit(tick)
            if reason is not None:
                self._close(tick.security_id, tick.last_price, tick.exchange_time, reason)
            return

        # Pending entry: fill at the first available price for the chosen contract.
        if (
            self._pending is not None
            and not self.positions.has_position()
            and tick.security_id == self._pending.security_id
        ):
            if self.session.can_enter(tick.exchange_time) and self._pending_side is not None:
                self._open(self._pending, self._pending_side, tick.last_price, tick.exchange_time)
            self._pending = None
            self._pending_side = None

    # ----------------------------------------------------------- candle/signal
    def _on_candle_close(self, candle: OHLC, ts: datetime) -> None:
        if self._last_processed_candle_end is not None and ts <= self._last_processed_candle_end:
            # Phase 6 Part 3: a replay landing on (or before) a candle this
            # strategy-day already fully processed before a restart. Refused
            # outright, before the regime tagger or the strategy see it, so a
            # session-cumulative indicator cannot double-count what it already saw.
            log.info(
                "%s: skipping an already-processed candle at %s (restored watermark %s)",
                self.label,
                ts.isoformat(),
                self._last_processed_candle_end.isoformat(),
            )
            return
        # Update the regime classifier with the just-closed underlying candle
        # before consulting the strategy, so an entry this candle is tagged with
        # the regime as of now. Purely observational — no effect on the signal.
        self._regime.observe(candle)
        strat_signal = self.strategy.on_candle(candle, ts)
        # Heartbeat: one line per closed candle so the run is visibly alive even
        # when no signal fires (e.g. during indicator warm-up).
        o = candle.open if candle.open is not None else candle.close
        interval = parse_timeframe_minutes(self.cfg.timeframe)
        bar_start = candle.start
        bar_label = (
            f"{bar_start:%H:%M}-{bar_start + timedelta(minutes=interval):%H:%M}"
            if bar_start is not None
            else "unknown"
        )
        log.info(
            "%s bar %s evaluated_at=%s O/H/L/C=%.2f/%.2f/%.2f/%.2f | %s",
            self.label,
            bar_label,
            ts.strftime("%H:%M:%S"),
            o,
            candle.high,
            candle.low,
            candle.close,
            self.strategy.status(),
        )
        if strat_signal is None:
            # No actionable signal, but the strategy's own on_candle() may still
            # have advanced its exit-policy state (e.g. a trailing peak driven off
            # the underlying) or the MFE/MAE excursion without producing one —
            # persist both, and this candle as the new watermark, either way.
            self._persist_open_position_checkpoint(ts)
            return
        log.info(
            "SIGNAL %s bar=%s evaluated_at=%s | %s",
            strat_signal.action.value,
            bar_label,
            ts.time(),
            strat_signal.reason,
        )

        pos = self._current_position()

        if strat_signal.action is SignalAction.EXIT:
            if pos is not None:
                # _close() persists (clears) the exit-state key itself, once the
                # position it belonged to is actually gone.
                self._close(
                    pos.contract.security_id,
                    pos.last_price,
                    ts,
                    strat_signal.exit_reason or ExitReason.STRATEGY_EXIT,
                )
            return

        # ENTER: already in this exact leg -> nothing to do, but persist for the
        # same reason as the "no signal" branch above.
        if (
            pos is not None
            and pos.contract.option_type == strat_signal.option_type
            and pos.side == strat_signal.side
        ):
            self._persist_open_position_checkpoint(ts)
            return

        # Different/opposite leg open -> exit it first (_close() persists/clears).
        if pos is not None:
            self._close(pos.contract.security_id, pos.last_price, ts, ExitReason.OPPOSITE_SIGNAL)

        # Enter the new leg if still within the entry window. Nothing to persist
        # here yet -- _enter() only queues a pending contract; the exit-state
        # write happens once _open() actually opens it, via the premium/underlying
        # candle checkpoints above, the same as any other open position.
        if self.session.can_enter(ts):
            self._enter(strat_signal, ts)

    def _current_position(self) -> OpenPosition | None:
        positions = self.positions.positions
        return positions[0] if positions else None

    def _enter(self, strat_signal: StrategySignal, ts: datetime) -> None:
        # Checked here (not at candle close) so the indicator still updates and any
        # exit above still runs -- only the entry is refused.
        if self._entry_blocked is not None:
            log.info("entry suppressed — blocked for the day: %s", self._entry_blocked)
            return
        if self._daily_guard is not None and self._daily_guard.halted:
            log.info(
                "daily loss cap already hit (%s); skipping entry for the rest of the day",
                self._daily_guard.halt_reason,
            )
            return
        if self._spot is None:
            log.warning("no spot price yet; cannot select option contract")
            return
        if strat_signal.option_type is None:
            log.warning("ENTER signal carries no option type; ignoring it")
            return
        selection = strat_signal.option_selection or self.strategy.option_selection
        contract = self.selector.select(
            self._spot, strat_signal.option_type, selection.moneyness, selection.steps
        )
        self.feed.subscribe(contract.security_id)
        # Always fill on the next *fresh* tick for the contract. We deliberately do
        # NOT use any cached price: a just-(re)subscribed contract may have a stale
        # one (e.g. from an earlier trade of the same strike), which would fill at a
        # wrong price and book a phantom gap loss/gain.
        self._pending = contract
        self._pending_side = strat_signal.side
        log.info(
            "pending entry queued: %s %s (awaiting first tick)",
            strat_signal.side.value,
            contract.symbol,
        )

    def _open(self, contract: OptionContract, side: OrderSide, price: float, ts: datetime) -> None:
        # Armed BEFORE the position opens (Phase 6 Part 2, reordered from before):
        # a manager needs the chance to compute stop_price/target_price off the
        # reference price so positions.open() can persist them on the same fill
        # that creates the row. Safe to reorder — new_position() doesn't consult
        # the position and positions.open() doesn't consult the risk manager, so
        # no existing behaviour depended on the old order.
        self.strategy.risk_manager.new_position(self._lots, entry_price=price)
        self.positions.open(
            contract,
            side,
            price,
            ts,
            regime=self._regime.current_regime(),
            regime_features=self._regime.current_features(),
            stop_price=self.strategy.risk_manager.stop_price,
            target_price=self.strategy.risk_manager.target_price,
        )
        st = self.strategy.risk_manager.state
        log.info("risk armed for %s: %s", contract.symbol, st)
        self._notify("entry", f"{side.value} {contract.symbol} @ {price:.2f}")
        # Fresh premium-candle state for this contract — a new entry, re-entry or
        # strike change must never inherit a prior contract's candle.
        if self._option_candles is not None:
            self._option_candles.retarget(security_id=contract.security_id)
            self._option_candle_contract_id = contract.security_id
            self._last_option_tick_ts = ts
            self._premium_gap_logged = False

    def _close(self, position_id: str, price: float, ts: datetime, reason: ExitReason) -> None:
        pos = self.positions.get(position_id)
        contract = pos.contract if pos else None
        trade = self.positions.close(
            position_id,
            price,
            ts,
            reason,
            exit_regime=self._regime.current_regime(),
            session_tags=self._regime.session_tags(contract, ts),
        )
        # Book realised P&L into the daily guard (so a big losing exit that lands
        # past the cap also halts the day, even if the per-tick MTM check didn't
        # trip first). No-op once already halted.
        if self._daily_guard is not None and trade is not None:
            self._daily_guard.register_trade(trade.net_pnl)
        self.strategy.risk_manager.reset()
        if contract is not None:
            self.feed.unsubscribe(contract.security_id)
        self._notify(
            "exit",
            f"{trade.side.value} {trade.contract.symbol} @ {trade.exit_price:.2f} "
            f"reason={trade.exit_reason.value} net={trade.net_pnl:.2f}",
        )
        # Optional strategy hook (default no-op) — lets a strategy react to a
        # closed trade's outcome, e.g. a consecutive-loss cooldown.
        self.strategy.on_position_closed(trade)
        if self._option_candles is not None:
            self._option_candles.reset()
            self._option_candle_contract_id = None
            self._last_option_tick_ts = None
        # Clears the persisted exit-state key now that the book is flat again —
        # after on_position_closed's reset(), so what's written (nothing) agrees
        # with what a restart would find in memory.
        self._persist_exit_state()

    def _shutdown(self, ts: datetime) -> None:
        """Honour a square-off request, on the thread that owns the feed.

        Idempotent: :meth:`_handle_square_off` latches ``_squared_off`` and ends
        with ``feed.stop()``, so a repeated request closes nothing twice. The one
        case that needs more is a request arriving *after* the session square-off
        has already run — then ``_handle_square_off`` returns early having stopped
        the feed on an earlier call, and the run is already ending anyway; stopping
        again is harmless and covers a feed that ignored the first one.

        The timestamp is the tick's, not wall-clock. Every other close path in this
        engine prices from the tick that caused it; the reference used ``now_ist()``
        only because a signal handler had no tick to hand.
        """
        if not self._stopped_by_request:
            self._stopped_by_request = True
            log.warning(
                "square-off requested (%s); closing positions and stopping the feed",
                self._shutdown_reason,
            )
        already_squared_off = self._squared_off
        self._handle_square_off(ts)
        if already_squared_off:
            self.feed.stop()

    def _handle_square_off(self, ts: datetime) -> None:
        if self._squared_off:
            return
        for pos in list(self.positions.positions):
            log.info("square-off reached; force-closing %s", pos.contract.symbol)
            self._close(pos.contract.security_id, pos.last_price, ts, ExitReason.SQUARE_OFF)
        self._squared_off = True
        # Only now, and only if every close above returned: a completion recorded
        # optimistically would let a restart skip a book that is still open.
        self._square_off.completed(ts)
        log.info("trading day complete; stopping feed")
        # The engine path's own success event — before this, only the fixture
        # path (worker.py's _maybe_square_off) notified on completion; the
        # engine path had only the *failure* alarm
        # (engine_worker._raise_silent_engine_alarm), never this. Guarded by
        # the same _squared_off latch as the rest of this method, so it fires
        # exactly once per engine instance, the same way every other
        # once-per-run event in this codebase is guarded.
        self._notify("square_off_completed", f"{self.label} squared off at {ts.isoformat()}")
        self.feed.stop()

    def _notify(self, event_type: str, message: str) -> None:
        """One operational event, through the wrapper that cannot raise."""
        self.notifier.send(
            NotificationEvent(
                event_type=event_type,
                message=message,
                runtime_id=self._runtime_id,
                strategy_id=self.strategy.name,
                execution_mode=self.cfg.execution_mode,
            )
        )
