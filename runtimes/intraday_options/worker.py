"""One paper strategy worker process.

``run_worker`` is a **module-level function taking one picklable argument**, and
that shape is load-bearing rather than stylistic. macOS defaults to the ``spawn``
start method, so the child re-imports this module and unpickles its arguments;
a closure, a bound method, or a config object holding a live SQLite connection
would fail to pickle, or worse, would appear to work while handing the child a
connection it must not share.

Startup follows the spec's recovery order exactly:

    acquire lock → open database → integrity check → load previous incomplete
    session → load open paper positions → restore strategy state → consume

The lock comes first. Everything after it assumes single ownership of this
strategy's state, and that assumption has to be established before any read.

Two strategy shapes, and why one of them is imported lazily
-----------------------------------------------------------
A worker drives either the Phase 1 candle-shaped
:class:`~strategies.intraday_options.fixture_strategy.FixtureSignalStrategy` or, when
:attr:`WorkerConfig.engine` is set, the ported
:class:`~common.engine.engine.TradingEngine` off the hub's tick channel. All of the
second path lives in :mod:`runtimes.intraday_options.engine_worker`, which **this
module never imports at module level**.

That is a measured constraint, not a style choice. Importing ``common.engine`` costs
this module's import roughly +0.2 s (0.099 s → 0.301 s, 0 → 16 modules), every child
pays it on ``spawn``, and the equivalent drag in Part 2b-i pushed a spawned child
past the 0.5 s window in ``test_duplicate_worker_startup_is_refused``. The engine
branch below therefore reaches it through exactly one deferred import, and
``tests/unit/test_worker_import_boundary.py`` enforces that three ways — statically
against this file, against a real interpreter's ``sys.modules``, and positively, so
the boundary cannot be satisfied by an engine path that never loads.

``EngineWorkerConfig`` lives here rather than in ``common.engine`` for the same
reason: the child unpickles it, and unpickling it from ``common.engine`` would import
the very package the boundary exists to keep out.
"""

from __future__ import annotations

import enum
import os
import queue as queue_module
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common.broker import Broker, build_broker
from common.config import load_settings
from common.config.models import ExecutionMode
from common.execution import ExecutionRepository, OrderLifecycle
from common.health import DEFAULT_INTERVAL_SECONDS, HealthState, HeartbeatWriter
from common.logging import get_logger, setup_logging
from common.models import Candle, PositionStatus
from common.notifications import (
    NotificationEvent,
    Notifier,
    NullNotifier,
    SafeNotifier,
    build_notifier,
)
from common.persistence import Database, MigrationRunner
from common.process import (
    DuplicateProcessError,
    clear_square_off_request,
    read_square_off_request,
    square_off_request_path,
    worker_lock,
)
from common.process.square_off_requests import SquareOffRequest
from common.risk import SquareOffPolicy, SquareOffState, SquareOffTrigger
from strategies.intraday_options import FixtureSignalStrategy

_log = get_logger(__name__)

#: Exit code used when another worker already owns this strategy. Distinct from
#: 1 so a supervisor (or a test) can tell "refused as duplicate" from "crashed".
EXIT_DUPLICATE = 3


class _NotifierSentinel(enum.Enum):
    """Sentinel for ``notifier=``: build the production notifier here, in the
    spawned child, from a freshly-loaded :class:`~common.config.Settings`.

    **An ``Enum``, not a plain sentinel object, and that is load-bearing.**
    ``run_worker``'s arguments cross the ``spawn`` boundary through ``pickle``
    (module docstring). A plain ``object()`` sentinel does *not* survive that
    round trip with its identity intact — unpickling constructs a new
    instance, so ``notifier is NOTIFIER_FROM_SETTINGS`` inside the child
    silently evaluates ``False`` even when the parent passed exactly this
    sentinel, and the un-recognised value fell through to being used *as* a
    notifier (an ``AttributeError`` the moment anything tried to call
    ``.send()`` on it — caught by ``test_undelivered_ticks_do_not_wedge_the_
    supervisors_exit`` and several siblings the first time this shipped).
    ``Enum`` members are the standard-library's own pickle-safe singleton:
    unpickling one is guaranteed to return the *same* member object, which is
    exactly the property an identity-checked, cross-process sentinel needs.

    Deliberately **not** the same as leaving ``notifier`` at its ordinary
    default (``None``, which still means "no notifier — use ``NullNotifier``",
    unchanged). Overloading ``None`` for this was considered and rejected: this
    machine's own ``.env`` carries real Telegram credentials, and at least one
    existing test (``test_walking_skeleton.py``'s duplicate-worker gate) spawns
    a real child process via exactly ``run_worker(config, feed_queue)`` — bare
    default, no notifier argument. Had ``None`` triggered settings-based
    construction, that test would have quietly started making real network
    calls to Telegram on any machine with credentials configured. Only the
    supervisor's spawn call passes this sentinel, making "build a real
    notifier here" an explicit choice by the one caller that actually
    represents a production run, never an inferred one.

    The child cannot inherit the parent's already-built notifier object across
    the ``spawn`` boundary regardless (a fresh interpreter re-imports this
    module), and threading a bot token through the picklable ``WorkerConfig``
    would move a secret through a channel built for plain values — the OS
    already hands every spawned child the same environment the parent read
    ``.env`` from, so asking each process to build its own from ``Settings``
    is both simpler and safer than passing the credential across.
    """

    FROM_SETTINGS = enum.auto()


NOTIFIER_FROM_SETTINGS = _NotifierSentinel.FROM_SETTINGS

#: How long to wait for a candle before checking shutdown conditions again.
_QUEUE_POLL_SECONDS = 0.5


@dataclass
class EngineWorkerConfig:
    """The ported engine's configuration, as plain picklable values.

    Present on a :class:`WorkerConfig` means "drive :class:`~common.engine.engine.
    TradingEngine` off the tick channel"; absent means the Phase 1 fixture path.

    **Every field here is a primitive.** Nothing in this dataclass may be a type from
    ``common.engine``: the child unpickles it before any engine code is imported, and
    a single engine-owned type in a field would drag the whole package into the graph
    the module docstring exists to protect.

    The strategy travels as a dotted reference plus keyword arguments rather than a
    registry name. ``common.engine.strategy.get_strategy(name, cfg)`` takes exactly
    one positional config argument, which the keyword-only constructors the engine's
    strategies actually have do not fit — so the reference form is what can express
    them without changing the ported registry.
    """

    #: ``"package.module:ClassName"``. Resolved in the child, inside the engine
    #: branch, by :func:`runtimes.intraday_options.engine_worker.load_strategy`.
    strategy_ref: str
    strategy_kwargs: dict[str, Any] = field(default_factory=dict)
    #: Candle interval the engine builds its own bars at, e.g. ``"5m"`` (D23).
    timeframe: str = "5m"
    #: Human-readable name for the underlying; defaults to ``WorkerConfig.instrument``.
    underlying_instrument: str = ""
    lots: int = 1
    #: Gap between tradable strikes for this underlying (e.g. 50 for NIFTY).
    strike_step: int = 50
    #: Fallback lot size, used **only** by the ``simulated`` resolver. With
    #: ``contract_resolver="dhan"`` the exchange's own lot size wins, because a
    #: configured one that drifts from the contract is half of limitation 17.
    lot_size: int = 50
    expiry: str | None = None
    #: ``"simulated"`` (default, unchanged) or ``"dhan"``. ``"dhan"`` resolves
    #: real contracts out of the daily instrument master, so the ids the engine
    #: subscribes and fills are ones the broker recognises — runbook limitation
    #: 17. Left defaulted so every existing config keeps its current behaviour.
    contract_resolver: str = "simulated"
    #: Where the cached instrument master lives. Empty means the project's
    #: ``data/cache``. A primitive, like everything else here, so the config
    #: stays picklable for the spawned child.
    scrip_master_cache_dir: str = ""
    #: Overrides for an underlying absent from ``INDEX_REGISTRY``. All three are
    #: needed together; empty means "look it up in the registry".
    index_security_id: str = ""
    index_segment: str = ""
    fno_segment: str = ""
    starting_capital: float = 100_000.0
    max_daily_loss_percent: float | None = None
    regime_enabled: bool = False
    warmup_from_history: bool = True
    #: ``"none"`` (default, unchanged) or ``"dhan"``. ``"dhan"`` builds a
    #: ``WarmupManager`` + ``WarmupSource`` (Phase 4 Part 4) so a
    #: continuity-required strategy's indicators are seeded from real history
    #: instead of cold-starting — runbook limitation 16. Independent of
    #: ``contract_resolver``: this fetches the *underlying's* history via
    #: ``resolve_index_meta``, which needs no scrip master. Left defaulted so
    #: every existing config keeps today's cold-start behaviour.
    warmup_source: str = "none"
    #: How many prior trading sessions to fetch when a strategy's warm-up spec
    #: needs more bars than one session at this timeframe holds. Passed to
    #: ``WarmupManager(max_lookback_sessions=...)``.
    warmup_max_lookback_sessions: int = 3
    parameters: dict[str, Any] = field(default_factory=dict)
    #: The session's opening bound. Its *closing* bounds are derived from
    #: ``WorkerConfig.square_off_policy`` — see
    #: :meth:`common.engine.config.SessionConfig.from_square_off_policy` — so the two
    #: configured square-off times cannot drift apart.
    session_start_time: str = "09:15"
    holidays: tuple[str, ...] = ()
    #: How long ``HubTickFeed`` blocks on the queue before re-checking its flags —
    #: including whether a square-off has been requested, which is what bounds a
    #: shutdown arriving while the stream is silent.
    feed_poll_seconds: float = 0.5


@dataclass
class MultiLegEngineWorkerConfig:
    """The generic multi-leg engine's configuration, as plain picklable values.

    Present on a :class:`WorkerConfig` means "drive
    :class:`~common.engine.multi_leg_engine.MultiLegEngine` off the tick
    channel"; mirrors :class:`EngineWorkerConfig` field-for-field where the
    single-leg and multi-leg engines share a concept, and adds only what a
    multi-leg strategy needs beyond that — an auxiliary market-data
    instrument (India VIX for ``straddle_920``) alongside the underlying.

    **Every field here is a primitive**, for the same spawn-boundary reason
    :class:`EngineWorkerConfig` documents: the child unpickles this before any
    ``common.engine`` code is imported.
    """

    #: ``"package.module:ClassName"``, resolved by
    #: :func:`runtimes.intraday_options.multi_leg_engine_worker.
    #: load_multi_leg_strategy`.
    strategy_ref: str
    strategy_kwargs: dict[str, Any] = field(default_factory=dict)
    timeframe: str = "5m"
    underlying_instrument: str = ""
    lots: int = 1
    strike_step: int = 50
    lot_size: int = 50
    expiry: str | None = None
    contract_resolver: str = "simulated"
    scrip_master_cache_dir: str = ""
    index_security_id: str = ""
    index_segment: str = ""
    fno_segment: str = ""
    #: Auxiliary market-data-only instrument (e.g. India VIX). Empty
    #: ``vix_security_id`` means "this strategy has none" — a multi-leg
    #: strategy that needs no auxiliary index (a future one) simply leaves
    #: these unset.
    vix_security_id: str = ""
    #: Named segment (e.g. ``"IDX_I"``), resolved to a numeric code by the
    #: worker via :func:`common.market_data.scrip_master.segment_code` —
    #: never a fake ``fno_segment``, see
    #: :mod:`common.market_data.instruments`.
    vix_segment: str = "IDX_I"
    vix_mode: int | None = None
    starting_capital: float = 100_000.0
    max_daily_loss_percent: float | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    session_start_time: str = "09:15"
    holidays: tuple[str, ...] = ()
    feed_poll_seconds: float = 0.5


@dataclass
class WorkerConfig:
    """Everything a worker needs, and nothing that cannot be pickled.

    Deliberately holds paths and plain values — never an open database handle,
    a lock object or a logger. The child process builds those itself.
    """

    runtime_id: str
    strategy_id: str
    security_id: str
    instrument: str
    database_path: Path
    lock_dir: Path
    pid_dir: Path
    log_dir: Path
    trading_date: str
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    #: The exchange segment ``security_id`` (the underlying, not any option
    #: contract) lives in, as a numeric MarketFeed code — e.g. ``0`` for
    #: ``IDX_I``. Resolved by ``config_adapter.py::build_worker_config`` from
    #: ``common.market_data.scrip_master.resolve_index_meta``. ``None`` means
    #: "the adapter's default", which is wrong for a real index subscription —
    #: see ``common.feed.hub.WorkerChannel.segment`` for why this must be set
    #: explicitly rather than left to default.
    security_segment: int | None = None
    #: The subscription mode for ``security_id``. ``None`` keeps the adapter's
    #: default (Ticker), which is correct for an index — it has no order book.
    security_mode: int | None = None
    quantity: int = 50
    entry_on_candle: int = 1
    exit_on_candle: int = 3
    max_candles: int | None = None
    idle_timeout_seconds: float = 10.0
    paper_execution: dict[str, Any] = field(default_factory=dict)
    cost_rates: dict[str, Any] = field(default_factory=dict)
    square_off_policy: SquareOffPolicy = field(default_factory=SquareOffPolicy)
    config_fingerprint: str | None = None
    #: Set to drive the ported engine instead of the fixture strategy. See the module
    #: docstring for why the code behind it is imported lazily.
    engine: EngineWorkerConfig | None = None
    #: Set to drive the generic multi-leg engine instead. Mutually exclusive
    #: with ``engine`` in practice — ``config_adapter.py::build_worker_config``
    #: routes by ``StrategyConfig.engine`` (``EngineKind``) and only ever
    #: populates one of the two — but nothing here enforces that structurally;
    #: the worker checks ``multi_leg_engine is not None`` first.
    multi_leg_engine: MultiLegEngineWorkerConfig | None = None
    #: From ``RuntimeConfig.health.heartbeat_interval_seconds`` — see
    #: :mod:`common.config.models`. A primitive, like every other field here,
    #: so this stays picklable for the spawned child (module docstring).
    heartbeat_interval_seconds: float = DEFAULT_INTERVAL_SECONDS

    # ---------------------------------------------------------- Phase 10
    #: The real live-gate inputs, carried as primitives (not a ResolvedConfig
    #: — see the module docstring's picklability constraint) so
    #: ``resolved_config_from_worker`` can reconstruct the *actual* gate the
    #: operator configured, rather than the fixed ``live_trading_enabled=
    #: False`` stub every worker used to see regardless of real config. Every
    #: default here is the fail-closed value: a worker built without these
    #: explicitly set behaves exactly as conservatively as the old stub did.
    global_live_trading_enabled: bool = False
    runtime_enabled: bool = False
    runtime_live_execution_allowed: bool = False
    strategy_enabled: bool = False
    strategy_live_approved: bool = False
    #: Whether live preflight has already run and passed, computed by the
    #: process that admitted this worker (``__main__.py::build_supervisor``,
    #: via ``common.broker.live_preflight_gate.LivePreflightGate``) —
    #: **not** recomputed here. A worker process constructing its own real
    #: ``dhanhq`` client at startup, purely to run preflight, is deferred:
    #: it would pull the Dhan SDK into this module's measured import-time
    #: budget (module docstring; ``test_worker_import_boundary.py``) for a
    #: path that, with every committed config staying ``mode: paper``,
    #: never executes. Documented explicitly as a known gap versus the
    #: originally-approved "the worker re-runs preflight itself" design,
    #: not a silent deviation — see the Phase 10 report.
    live_preflight_passed: bool = False
    #: The controlled-live quantity cap, threaded through so a live
    #: ``ResolvedConfig`` can be reconstructed without inventing a value —
    #: see ``StrategyConfig.live_quantity_lots``.
    live_quantity_lots: int | None = None
    #: Enough of ``LivePreflightConfig`` to reconstruct a *valid* live
    #: ``ResolvedConfig`` (its own cross-field validator requires all of
    #: these whenever mode is live) — never invented here, always the real
    #: values from ``runtime.live_preflight`` in the source YAML, populated
    #: by ``config_adapter.py::build_worker_config``.
    live_expected_static_ip: str | None = None
    live_egress_ip_provider: str | None = None
    live_max_preflight_age_seconds: int | None = None
    live_rate_limit_rules: tuple[tuple[str, int, int], ...] = ()
    live_rate_limit_new_order_limit: int | None = None
    live_rate_limit_new_order_window_seconds: int | None = None
    live_max_daily_loss: float | None = None
    live_max_open_positions: int | None = None
    live_max_open_legs: int | None = None
    live_max_deployed_capital: float | None = None
    live_max_mtm_age_seconds: int | None = None
    account_shared_database_path: Path | None = None
    token_cache_dir: Path | None = None

    @property
    def requires_tick_channel(self) -> bool:
        """Whether this worker is tick-driven and must be registered with a
        tick (and control) channel.

        The single source of truth for that question. Both engine paths read
        their market data from the raw-tick channel and neither can run
        without one: :func:`runtimes.intraday_options.engine_worker.run_engine`
        and :func:`runtimes.intraday_options.multi_leg_engine_worker.
        run_multi_leg_engine` each refuse to start when the supervisor handed
        them no tick queue. Only the fixture (non-engine) path runs off the
        candle channel alone.

        This exists because the composition root
        (``runtimes/intraday_options/__main__.py::build_supervisor``) used to
        spell the predicate out as ``worker_config.engine is not None``, which
        silently assumed the single-leg engine was the only tick-driven one.
        ``straddle_920`` — the first ``multi_leg_engine`` strategy — was
        therefore registered without a tick channel and died at startup on
        exactly the refusal above, in a real paper session. Adding a
        ``multi_leg_engine`` term at that one call site would have fixed that
        strategy and left the next engine kind to rediscover the same defect,
        so the question is answered here, next to the fields it is asked
        about, and every caller asks it the same way.
        """
        return self.engine is not None or self.multi_leg_engine is not None


@dataclass
class WorkerOutcome:
    """What one worker run did — the return value tests assert against."""

    candles_processed: int = 0
    orders_placed: int = 0
    recovered_position: bool = False
    square_off_completed: bool = False
    exit_code: int = 0
    error: str | None = None
    # ---------------------------------------------------- the engine path only
    #: Ticks the engine's feed delivered. Zero on the fixture path.
    ticks_processed: int = 0
    #: Completed round trips the engine booked.
    trades_closed: int = 0
    #: Ticks the **hub** dropped for this worker before they ever arrived, as
    #: reported in band by a ``TickDropNotice``. Non-zero means this worker's own
    #: candles may differ from the hub's, and entries are latched off for the day.
    ticks_dropped_upstream: int = 0
    #: False when the engine did not finish within the grace period after a
    #: square-off was requested — a feed delivering nothing offers no boundary at
    #: which to close. The run is shut down in every other respect; see
    #: ``engine_worker._raise_silent_engine_alarm``.
    clean_engine_shutdown: bool = True
    #: Whether the run ended by honouring a square-off request rather than by the
    #: stream finishing.
    stopped_by_request: bool = False


def run_worker(
    config: WorkerConfig,
    candle_queue: Any,
    notifier: Notifier | _NotifierSentinel | None = None,
    tick_queue: Any = None,
    control_queue: Any = None,
) -> WorkerOutcome:
    """Run one strategy worker to completion.

    Args:
        config: picklable worker configuration.
        candle_queue: the bounded queue the hub publishes completed candles to.
        notifier: ``None`` (the default) means a null channel — every existing
            test relies on this. Pass :data:`NOTIFIER_FROM_SETTINGS` to build a
            real one from this process's own environment instead — see that
            sentinel's docstring for why the two are not the same thing. Any
            other :class:`~common.notifications.base.Notifier` is used as
            given, exactly as before (a test's ``RecordingNotifier``, say).
        tick_queue: the raw-tick channel, when the supervisor gave this worker one.
            Required by — and only used by — the engine path.
        control_queue: the upstream channel the engine's runtime subscriptions
            travel back to the supervisor on. Optional even on the engine path: a
            worker without one can still trade its configured instruments, it just
            cannot ask for new ones, and ``HubTickFeed`` says so loudly.
    """
    settings = load_settings()
    # settings= registers this worker's own secrets (a Telegram token now
    # possibly among them) with its own redaction filter. Before this, a
    # spawned worker's setup_logging call omitted settings entirely — spawn
    # starts a fresh interpreter, so the parent's registration never carried
    # over, and this worker's logs relied on pattern redaction alone.
    setup_logging(
        level=settings.algo_log_level,
        log_dir=config.log_dir,
        log_file_name=f"{config.strategy_id}.log",
        settings=settings,
    )
    outcome = WorkerOutcome()

    resolved_notifier: Notifier | None = (
        build_notifier(settings) if notifier is NOTIFIER_FROM_SETTINGS else notifier
    )

    lock = worker_lock(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        lock_dir=config.lock_dir,
        pid_dir=config.pid_dir,
    )
    try:
        lock.acquire()
    except DuplicateProcessError as exc:
        # Refusal, not a crash. The first worker keeps running untouched.
        _log.error("%s", exc)
        outcome.exit_code = EXIT_DUPLICATE
        outcome.error = str(exc)
        return outcome

    try:
        return _run_locked(
            config, candle_queue, resolved_notifier, outcome, tick_queue, control_queue
        )
    finally:
        lock.release()


def run_worker_process(
    config: WorkerConfig,
    candle_queue: Any,
    notifier: Notifier | _NotifierSentinel | None = None,
    tick_queue: Any = None,
    control_queue: Any = None,
) -> None:
    """The real ``multiprocessing.Process(target=...)`` entry point.

    Phase 7 Part 4 found that ``worker_process.exitcode`` — what the
    supervisor actually reads to decide whether a worker succeeded — was
    **always 0 for a worker that returned normally**, however
    :attr:`WorkerOutcome.exit_code` had been set. ``multiprocessing`` calls
    its ``target`` and discards the return value; only an uncaught exception
    (or an explicit ``sys.exit``) changes the child's real exit code. So
    ``EXIT_DUPLICATE``, an integrity-check failure, and the per-candle
    exception handler's ``exit_code = 1`` were all invisible to
    ``worker_exit_codes`` whenever a worker actually ran the way production
    runs it — through a spawned process — even though each one *looked*
    correctly recorded in :class:`WorkerOutcome`. Caught by
    ``test_a_duplicate_worker_is_reported_not_silent``
    (``tests/end_to_end/test_supervisor.py``), which failed with
    ``worker_exit_codes["skelfix"] == 0`` where ``EXIT_DUPLICATE`` (3) was
    expected, against a real spawned duplicate — recorded as D77.

    Deliberately **not** folded into :func:`run_worker` itself: every
    existing test calls that function directly and inspects the returned
    :class:`WorkerOutcome` synchronously, in the *same* process as the test.
    A ``sys.exit()`` there would raise ``SystemExit`` into the test's own
    call stack, not just the (nonexistent, in that case) child's. This
    function exists only to be the thing ``multiprocessing.Process`` calls;
    every direct caller keeps using :func:`run_worker`, unchanged.
    """
    outcome = run_worker(config, candle_queue, notifier, tick_queue, control_queue)
    sys.exit(outcome.exit_code)


def _run_locked(
    config: WorkerConfig,
    candle_queue: Any,
    notifier: Notifier | None,
    outcome: WorkerOutcome,
    tick_queue: Any = None,
    control_queue: Any = None,
) -> WorkerOutcome:
    database = Database(config.database_path)
    MigrationRunner(database).run_pending()

    problems = database.integrity_check()
    if problems:
        # A corrupt database must stop the worker, not be traded through.
        outcome.exit_code = 1
        outcome.error = f"integrity check failed: {problems}"
        _log.error("refusing to start: %s", outcome.error)
        return outcome

    repository = ExecutionRepository(database)
    # Deferred only when an engine (single-leg or multi-leg) is driving:
    # engine.py's/multi_leg_engine.py's on_tick calls notifier.send() from
    # what would otherwise be this same thread, and a 5s Telegram timeout
    # there is the tick-thread stall spec 2554's "small internal queue"
    # exists to prevent. The fixture path's own sends happen between
    # candle_queue.get() calls, never inside a callback a feed is waiting on,
    # so it stays synchronous — simpler, and deterministic for every existing
    # test that asserts on notifications right after a run.
    deferred = config.engine is not None or config.multi_leg_engine is not None
    safe_notifier = SafeNotifier(
        notifier or NullNotifier(),
        deferred=deferred,
        # Safe to touch `repository` from this callback regardless of mode:
        # the fixture path calls send() only from this thread, and the
        # engine path's on_tick also runs on this thread (engine_worker.
        # _drive() docstring: "Run the engine on this thread"). SafeNotifier
        # itself never calls on_failure from its own drain thread — see its
        # module docstring.
        on_failure=lambda event, reason: repository.record_notification(
            runtime_id=config.runtime_id,
            strategy_id=event.strategy_id,
            execution_mode=event.execution_mode,
            channel=safe_notifier.channel,
            event_type=event.event_type,
            message=event.rendered(),
            delivered=False,
            failure_reason=reason,
        ),
    )

    session = repository.open_session(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        process_role="worker",
        pid=os.getpid(),
        config_fingerprint=config.config_fingerprint,
    )
    heartbeat = HeartbeatWriter(
        repository,
        session_id=session.id,
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        interval_seconds=config.heartbeat_interval_seconds,
    )
    heartbeat.beat(HealthState.STARTING, force=True)

    if config.engine is not None:
        # ------------------------------------------------------------------
        # The only deferred import in this module, and the reason it exists.
        # Everything ``common.engine`` touches lives behind this one line, so a
        # worker on the fixture path never loads it and never pays the ~0.2 s
        # that cost Part 2b-i its duplicate-worker window. Enforced by
        # tests/unit/test_worker_import_boundary.py — do not hoist this.
        # ------------------------------------------------------------------
        from .engine_worker import run_engine

        try:
            return run_engine(
                config,
                config.engine,
                repository=repository,
                session_id=session.id,
                heartbeat=heartbeat,
                notifier=safe_notifier,
                outcome=outcome,
                candle_queue=candle_queue,
                tick_queue=tick_queue,
                control_queue=control_queue,
            )
        finally:
            # Order matters: close() drains any queued deferred sends and can
            # still call on_failure (repository.record_notification) while
            # doing so, so the database must still be open when it runs.
            safe_notifier.close()
            database.close()

    if config.multi_leg_engine is not None:
        # Same deferred-import discipline as the ``config.engine`` branch
        # above, and for the identical reason — ``common.engine`` (which
        # ``multi_leg_engine_worker`` also reaches into) must never load on
        # the fixture or single-leg-engine paths.
        from .multi_leg_engine_worker import run_multi_leg_engine

        try:
            return run_multi_leg_engine(
                config,
                config.multi_leg_engine,
                repository=repository,
                session_id=session.id,
                heartbeat=heartbeat,
                notifier=safe_notifier,
                outcome=outcome,
                candle_queue=candle_queue,
                tick_queue=tick_queue,
                control_queue=control_queue,
            )
        finally:
            safe_notifier.close()
            database.close()

    strategy = FixtureSignalStrategy(
        strategy_id=config.strategy_id,
        security_id=config.security_id,
        instrument=config.instrument,
        execution_mode=config.execution_mode,
        quantity=config.quantity,
        entry_on_candle=config.entry_on_candle,
        exit_on_candle=config.exit_on_candle,
    )

    outcome.recovered_position = _recover(config, repository, strategy, session.id)
    if outcome.recovered_position:
        safe_notifier.send(
            NotificationEvent(
                event_type="position_recovered",
                message=f"restored open position for {config.strategy_id}",
                runtime_id=config.runtime_id,
                strategy_id=config.strategy_id,
                execution_mode=config.execution_mode,
            )
        )

    # The gate lives here, not in the strategy: a live-mode strategy must fail
    # to obtain a broker at all rather than run with a paper one.
    broker = build_broker(
        resolved_config_from_worker(config),
        preflight_passed=config.live_preflight_passed,
        paper_execution=config.paper_execution,
        cost_rates=config.cost_rates,
    )
    _check_broker_health(config, broker, repository)
    lifecycle = OrderLifecycle(
        repository=repository,
        broker=broker,
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        session_id=session.id,
        config_fingerprint=config.config_fingerprint,
    )

    safe_notifier.send(
        NotificationEvent(
            event_type="worker_started",
            message=f"{config.strategy_id} started in {config.execution_mode.value} mode",
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
        )
    )
    heartbeat.beat(HealthState.running_for(config.execution_mode), force=True)

    square_off_state = _load_square_off_state(repository, config)
    # Phase 7 Part 4: scripts/square_off.py's only channel to this worker — a
    # file, never a write to `positions` (see the module's own docstring).
    # Checked once per candle rather than on the Empty-timeout branch below:
    # the fixture path's square-off signal needs a real candle to price
    # against (`strategy.square_off_signal(candle)`), so — unlike the ported
    # engine's `HubTickFeed.on_poll`, which has no such dependency and is
    # wired to this same request in `engine_worker.py` — a request made while
    # this worker is idle between candles is honoured on the next candle, not
    # instantly. Documented, not fixed: the fixture path is the Phase 1
    # deterministic test-only signal (CLAUDE.md), not the production one.
    square_off_request_file = square_off_request_path(
        config.pid_dir.parent, config.runtime_id, config.strategy_id
    )

    try:
        while True:
            try:
                item = candle_queue.get(timeout=_QUEUE_POLL_SECONDS)
            except queue_module.Empty:
                # No candle within the idle window: the tape is finished (or
                # the feed is silent). Either way, stop cleanly.
                break

            if item is None:  # sentinel: supervisor is shutting the worker down
                break

            candle: Candle = item
            outcome.candles_processed += 1

            operator_request = read_square_off_request(square_off_request_file)
            square_off_state, squared, handled = _maybe_square_off(
                config=config,
                candle=candle,
                strategy=strategy,
                lifecycle=lifecycle,
                repository=repository,
                notifier=safe_notifier,
                state=square_off_state,
                operator_request=operator_request,
            )
            if operator_request is not None and handled:
                # Cleared only once the request has actually been acted on —
                # see the module docstring on why "seen" and "acted" are kept
                # distinct; a crash between the two must not lose the request.
                clear_square_off_request(square_off_request_file)
            if squared:
                outcome.orders_placed += 1
                outcome.square_off_completed = True
                break

            # square_off_state is checked directly, not only the time-of-day
            # gate below: under the ordinary time trigger the two always agree
            # (square_off_at is configured after entry_cutoff, so time alone
            # already denies entries by the moment the state changes), but an
            # operator's request can move the state to COMPLETED *before* the
            # configured cutoff, and the time gate alone would not yet know
            # to refuse a fresh entry on the very candle that just squared off.
            if square_off_state is not SquareOffState.PENDING or not (
                config.square_off_policy.entries_allowed(candle.end_at)
            ):
                heartbeat.beat(HealthState.BLOCK_NEW_ENTRIES, last_tick_at=candle.end_at)
                continue

            signal = strategy.on_candle(candle)
            if signal is not None:
                stop, target = strategy.stop_and_target()
                result = lifecycle.handle_signal(
                    signal,
                    trading_date=config.trading_date,
                    stop_price=stop,
                    target_price=target,
                )
                if result.traded:
                    outcome.orders_placed += 1
                    safe_notifier.send(
                        NotificationEvent(
                            event_type="order_filled",
                            message=(
                                f"{signal.side.value} {signal.quantity} "
                                f"{signal.instrument} @ {result.order.average_fill_price}"
                                if result.order
                                else "order filled"
                            ),
                            runtime_id=config.runtime_id,
                            strategy_id=config.strategy_id,
                            execution_mode=config.execution_mode,
                            correlation_id=result.correlation_id,
                        )
                    )

            repository.save_strategy_state(
                runtime_id=config.runtime_id,
                strategy_id=config.strategy_id,
                execution_mode=config.execution_mode,
                trading_date=config.trading_date,
                last_candle_end_at=candle.end_at.isoformat(),
            )
            heartbeat.beat(
                HealthState.running_for(config.execution_mode),
                last_tick_at=candle.end_at,
            )

            if config.max_candles is not None and outcome.candles_processed >= config.max_candles:
                break

    except Exception as exc:
        outcome.exit_code = 1
        outcome.error = str(exc)
        repository.record_error(
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            severity="CRITICAL",
            component="worker",
            message=str(exc),
        )
        heartbeat.beat(HealthState.FAILED, force=True)
        _log.exception("worker failed strategy_id=%s", config.strategy_id)
        safe_notifier.close()
        database.close()
        return outcome

    heartbeat.beat(HealthState.STOPPING, force=True)
    safe_notifier.send(
        NotificationEvent(
            event_type="worker_stopped",
            message=f"{config.strategy_id} stopped cleanly",
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
        )
    )
    repository.close_session(session.id)
    heartbeat.beat(HealthState.STOPPED, force=True)
    safe_notifier.close()
    database.close()
    return outcome


def _check_broker_health(
    config: WorkerConfig, broker: Broker, repository: ExecutionRepository
) -> None:
    """Check the broker once at startup, and record it if it says it is unhealthy.

    ``PaperBroker.is_healthy()`` always returns ``True`` — it has no connection
    to be unhealthy about — so this never fires today. It exists so the check
    is not dead code (the Phase 7 audit's own finding) and so the pattern is
    already in place for the engine path and for ``DhanLiveBroker`` (Phase 10),
    where a real connectivity failure is exactly the thing worth knowing before
    the first candle rather than after the first failed order.

    Not called per-candle: a broker health check is a startup fact, not a tick
    -rate one, matching the "never per tick or per candle" rule migration 0002
    states for these diagnostic writes.
    """
    if broker.is_healthy():
        return
    message = f"broker {broker.name!r} reported unhealthy at worker startup"
    _log.error("%s", message)
    repository.record_error(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        severity="ERROR",
        component="broker",
        message=message,
    )


def _recover(
    config: WorkerConfig,
    repository: ExecutionRepository,
    strategy: FixtureSignalStrategy,
    session_id: int,
) -> bool:
    """Restore open paper state from the previous incomplete session.

    Returns True when an open position was adopted. The strategy's own counters
    are restored too, so a recovered worker does not re-fire its entry signal.
    """
    close_previous_session(config, repository, session_id)

    positions = repository.open_positions(
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
    )
    if not positions:
        return False

    position = positions[0]
    state = repository.load_strategy_state(
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
    )
    # Resume the candle count past the entry so the restored worker looks for
    # its exit, not another entry into a position it already holds.
    candles_seen = strategy.entry_on_candle if state is not None else 0
    strategy.restore(
        position_open=position.status is PositionStatus.OPEN and position.quantity != 0,
        entry_price=position.average_price,
        candles_seen=candles_seen,
    )
    _log.info(
        "restored position strategy_id=%s qty=%d avg=%.2f",
        config.strategy_id,
        position.quantity,
        position.average_price,
    )
    return True


def close_previous_session(
    config: WorkerConfig, repository: ExecutionRepository, session_id: int
) -> None:
    """Close the session a previous, crashed process left open.

    Shared by both strategy shapes: whichever one is driving, an incomplete session
    row from a dead process has to be closed before this run's own bookkeeping means
    anything. Public because :mod:`runtimes.intraday_options.engine_worker` is the
    other caller, and duplicating it there would let the two drift.
    """
    previous = repository.previous_incomplete_session(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        exclude_session_id=session_id,
    )
    if previous is not None:
        _log.info("recovering from incomplete session id=%s", previous["id"])
        repository.close_session(int(previous["id"]), reason="recovered_after_restart")


def _load_square_off_state(repository: ExecutionRepository, config: WorkerConfig) -> SquareOffState:
    row = repository.load_strategy_state(
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
    )
    if row is None:
        return SquareOffState.PENDING
    try:
        return SquareOffState(row["square_off_state"])
    except ValueError:
        return SquareOffState.PENDING


def _maybe_square_off(
    *,
    config: WorkerConfig,
    candle: Candle,
    strategy: FixtureSignalStrategy,
    lifecycle: OrderLifecycle,
    repository: ExecutionRepository,
    notifier: SafeNotifier,
    state: SquareOffState,
    operator_request: SquareOffRequest | None = None,
) -> tuple[SquareOffState, bool, bool]:
    """Square off if the candle clock says so, or if an operator asked.

    Returns ``(new_state, acted, handled)``: ``acted`` is whether an order was
    actually placed (unchanged meaning from before Phase 7 Part 4 — a trigger
    can fire with no open position to close); ``handled`` is new, and is
    whether this call's trigger body ran at all, which is what the caller
    needs to know before it is safe to clear an operator's request file —
    clearing on ``acted`` alone would leave the file behind forever on a
    request that found nothing open to close.
    """
    # expiry=None, explicitly: the fixture path (Phase 1's walking skeleton) holds
    # no OptionContract and has no expiry to know, so Phase 6 Part 4's expiry-lead
    # rule is inert here by construction, not by oversight.
    trigger = config.square_off_policy.trigger_at(candle.end_at, state=state, expiry=None)
    # An operator request forces the same trigger the time-of-day ladder would
    # eventually reach — never a second code path. Suppressed once already
    # COMPLETED for the same reason `trigger_at` itself suppresses re-firing:
    # a second attempt against an already-flat day has nothing to do.
    operator_forced = (
        operator_request is not None
        and trigger is not SquareOffTrigger.SQUARE_OFF
        and state is not SquareOffState.COMPLETED
    )
    if trigger is not SquareOffTrigger.SQUARE_OFF and not operator_forced:
        return state, False, False

    repository.save_strategy_state(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
        square_off_state=SquareOffState.IN_PROGRESS.value,
        entries_blocked=True,
    )

    signal = strategy.square_off_signal(candle)
    acted = False
    correlation_id: str | None = None
    if signal is not None:
        result = lifecycle.handle_signal(signal, trading_date=config.trading_date)
        acted = result.traded
        correlation_id = result.correlation_id

    repository.save_strategy_state(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
        square_off_state=SquareOffState.COMPLETED.value,
        entries_blocked=True,
        last_candle_end_at=candle.end_at.isoformat(),
    )
    notifier.send(
        NotificationEvent(
            event_type="square_off_completed",
            message=f"{config.strategy_id} squared off at {candle.end_at.isoformat()}",
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            correlation_id=correlation_id,
        )
    )
    if operator_request is not None:
        # The audit trail's other half: scripts/square_off.py recorded
        # `square_off_requested` when it wrote the file; this is the worker
        # recording that its own square-off path actually carried it out —
        # never the script, which never touches `positions`.
        repository.record_audit_event(
            runtime_id=config.runtime_id,
            action="square_off_completed",
            actor=operator_request.requested_by,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            detail=f"requested at {operator_request.requested_at}: {operator_request.reason}",
        )
    return SquareOffState.COMPLETED, acted, True


def resolved_config_from_worker(config: WorkerConfig) -> Any:
    """Build the real ``ResolvedConfig`` the broker factory gates on.

    Phase 10: this used to be ``resolved_config_stub``, which hard-coded
    ``GlobalConfig(live_trading_enabled=False)`` and ``enabled=True``
    regardless of what the operator actually configured — an extra
    fail-safe by accident, but one that also meant flipping every real gate
    to ``true`` in YAML still could not reach this call site, because
    nothing here ever looked at the real values. This reads the actual
    gate inputs ``config_adapter.py`` threaded through as picklable
    primitives (see ``WorkerConfig``'s Phase 10 fields) — every committed
    config still resolves to every gate closed, but now because the real
    values genuinely are ``false``/``paper``, not because this function
    refuses to look.

    Imported lazily and constructed here so ``WorkerConfig`` stays a plain
    picklable record; pydantic models cross process boundaries fine, but keeping
    the worker's own contract primitive makes the spawn constraint obvious.
    """
    from common.config.models import (
        AccountRiskConfig,
        GlobalConfig,
        LiveOrderRateLimitConfig,
        LivePreflightConfig,
        RateLimitCallClass,
        RateLimitRule,
        ResolvedConfig,
        RuntimeConfig,
        StrategyConfig,
    )

    rate_rules = tuple(
        RateLimitRule(
            call_class=RateLimitCallClass(call_class),
            limit=limit,
            window_seconds=window_seconds,
        )
        for call_class, limit, window_seconds in config.live_rate_limit_rules
    )
    if (
        not rate_rules
        and
        config.live_rate_limit_new_order_limit is not None
        and config.live_rate_limit_new_order_window_seconds is not None
    ):
        rate_rules = (
            RateLimitRule(
                call_class=RateLimitCallClass.NEW_ORDER,
                limit=config.live_rate_limit_new_order_limit,
                window_seconds=config.live_rate_limit_new_order_window_seconds,
            ),
        )

    return ResolvedConfig(
        global_config=GlobalConfig(live_trading_enabled=config.global_live_trading_enabled),
        runtime=RuntimeConfig(
            runtime_id=config.runtime_id,
            enabled=config.runtime_enabled,
            live_execution_allowed=config.runtime_live_execution_allowed,
            live_preflight=LivePreflightConfig(
                expected_static_ip=config.live_expected_static_ip,
                egress_ip_provider=config.live_egress_ip_provider,
                max_preflight_age_seconds=config.live_max_preflight_age_seconds,
                rate_limits=LiveOrderRateLimitConfig(rules=rate_rules),
                account_risk=AccountRiskConfig(
                    max_daily_loss=config.live_max_daily_loss,
                    max_open_positions=config.live_max_open_positions,
                    max_open_legs=config.live_max_open_legs,
                    max_deployed_capital=config.live_max_deployed_capital,
                    max_mtm_age_seconds=config.live_max_mtm_age_seconds,
                ),
            ),
        ),
        strategy=StrategyConfig(
            strategy_id=config.strategy_id,
            runtime_id=config.runtime_id,
            enabled=config.strategy_enabled,
            mode=config.execution_mode,
            live_approved=config.strategy_live_approved,
            live_quantity_lots=config.live_quantity_lots,
        ),
    )
