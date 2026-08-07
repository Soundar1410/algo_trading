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
from common.process import DuplicateProcessError, worker_lock
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
    #: From ``RuntimeConfig.health.heartbeat_interval_seconds`` — see
    #: :mod:`common.config.models`. A primitive, like every other field here,
    #: so this stays picklable for the spawned child (module docstring).
    heartbeat_interval_seconds: float = DEFAULT_INTERVAL_SECONDS


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
        log_dir=config.log_dir, log_file_name=f"{config.strategy_id}.log", settings=settings
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
    # Deferred only when the engine is driving: engine.py's on_tick calls
    # notifier.send() from what would otherwise be this same thread, and a
    # 5s Telegram timeout there is the tick-thread stall spec 2554's "small
    # internal queue" exists to prevent. The fixture path's own sends happen
    # between candle_queue.get() calls, never inside a callback a feed is
    # waiting on, so it stays synchronous — simpler, and deterministic for
    # every existing test that asserts on notifications right after a run.
    deferred = config.engine is not None
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
        resolved_config_stub(config),
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

            square_off_state, squared = _maybe_square_off(
                config=config,
                candle=candle,
                strategy=strategy,
                lifecycle=lifecycle,
                repository=repository,
                notifier=safe_notifier,
                state=square_off_state,
            )
            if squared:
                outcome.orders_placed += 1
                outcome.square_off_completed = True
                break

            if not config.square_off_policy.entries_allowed(candle.end_at):
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
) -> tuple[SquareOffState, bool]:
    """Square off if the candle clock says so. Returns the new state and whether it acted."""
    # expiry=None, explicitly: the fixture path (Phase 1's walking skeleton) holds
    # no OptionContract and has no expiry to know, so Phase 6 Part 4's expiry-lead
    # rule is inert here by construction, not by oversight.
    trigger = config.square_off_policy.trigger_at(candle.end_at, state=state, expiry=None)
    if trigger is not SquareOffTrigger.SQUARE_OFF:
        return state, False

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
    return SquareOffState.COMPLETED, acted


def resolved_config_stub(config: WorkerConfig) -> Any:
    """Build the ResolvedConfig the broker factory gates on.

    Imported lazily and constructed here so ``WorkerConfig`` stays a plain
    picklable record; pydantic models cross process boundaries fine, but keeping
    the worker's own contract primitive makes the spawn constraint obvious.
    """
    from common.config.models import (
        GlobalConfig,
        ResolvedConfig,
        RuntimeConfig,
        StrategyConfig,
    )

    return ResolvedConfig(
        global_config=GlobalConfig(live_trading_enabled=False),
        runtime=RuntimeConfig(runtime_id=config.runtime_id, enabled=True),
        strategy=StrategyConfig(
            strategy_id=config.strategy_id,
            enabled=True,
            mode=config.execution_mode,
        ),
    )
