"""The intraday options strategy-group supervisor.

Owns the shared feed and the worker registry. One supervisor per strategy group,
one shared feed inside it, one child process per enabled strategy — never one
feed per strategy.

The supervisor deliberately does **not** touch the operational database's
**trading** tables. Workers own their own state, and a supervisor that also wrote
positions would reintroduce the shared-mutable-state problem that
process-per-strategy exists to avoid. It runs migrations once at startup (so
workers race nothing) and otherwise stays out of the way.

It does publish its own **health**, which is a different thing: the spec makes
"group persistence and health publication" a supervisor responsibility, and
`runtime_sessions`/`runtime_heartbeats` are keyed for it already — both accept a
null `strategy_id`, which is what a group-level row is. Without this the dashboard
has no way to distinguish "the group is running" from "the group died", because
every heartbeat it can see belongs to a worker.

The feed is driven on a dedicated thread after the workers are spawned, and the
supervisor's own thread stays free to coordinate. That is what makes the runtime
stoppable: a live ``DhanMarketFeedAdapter.start()`` never returns by itself, so a
supervisor that drove the feed inline would have no thread left to notice a
signal — which is exactly how it hung before Phase 3 Part 1. Either way the
parent is the only process holding the socket.

Shutdown, and why it is shaped like this
----------------------------------------
1. A ``SIGTERM``/``SIGINT`` handler sets an event and returns. Nothing else: a
   handler that tried to close the feed would run on this thread while the feed
   thread owns the adapter's loop, which is the cross-thread close that hangs.
   Installed via :func:`common.process.shutdown_signals`, which is where this
   supervisor's own handler-installing code moved in Part 2b-i so that the engine's
   process could reuse it rather than grow a second, competing installer.
2. The main thread then *asks* the feed to stop (``hub.request_stop()``) and
   waits for the feed thread to return on its own.
3. Only once it has does the main thread flush partial bars (``hub.stop()``),
   drain the workers and release the lock.

If the feed thread does not come back within the grace period — a connected
socket delivering no frames has no boundary at which to notice the request — the
supervisor completes the rest of the shutdown anyway and **raises the alarm
through every channel an operator actually watches**: a `DEGRADED` heartbeat
(which is deliberately the *last* one written, so the dashboard tile keeps showing
it), a `CRITICAL` row in `errors`, and a notification. It never escalates to
closing the connection from this thread: that is the failure mode this design
exists to prevent, and the feed thread is a daemon, so process exit reclaims the
socket regardless. See runbook limitation 13.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from common.config.models import ExecutionMode
from common.execution import ExecutionRepository
from common.feed import SharedFeedHub
from common.feed.hub import WorkerChannel, build_channel
from common.health import HealthState, HeartbeatWriter
from common.logging import get_logger
from common.market_data.adapter import MarketFeedAdapter
from common.notifications import NotificationEvent, Notifier, NullNotifier, SafeNotifier
from common.persistence import Database, MigrationRunner
from common.process import DuplicateProcessError, shutdown_signals, supervisor_lock

from .worker import WorkerConfig, run_worker

_log = get_logger(__name__)

#: Queue depth per worker. Roughly an hour of one-minute candles: deep enough
#: that a briefly busy worker loses nothing, shallow enough that a wedged one
#: is detected rather than accumulating a day of stale bars.
DEFAULT_QUEUE_DEPTH = 64

#: How long to wait for the feed thread to return after a stop is requested.
#: Generous: a live adapter notices at its next frame, and during market hours
#: frames arrive continuously. Exceeding it means the socket has gone quiet, not
#: that the feed is slow — waiting longer would not help.
DEFAULT_SHUTDOWN_GRACE = 10.0

#: How often the idle main thread beats while the feed runs. The writer is
#: rate-limited anyway; this only has to be finer than that to keep the
#: dashboard's heartbeat age honest.
HEARTBEAT_POLL_SECONDS = 1.0

#: `process_role` for the group-level session. Distinguishes it from the
#: `worker` sessions in the same table.
SUPERVISOR_ROLE = "supervisor"


@dataclass
class SupervisorConfig:
    """Group-level configuration."""

    runtime_id: str
    database_path: Path
    lock_dir: Path
    pid_dir: Path
    log_dir: Path
    candle_interval_seconds: int = 60
    queue_depth: int = DEFAULT_QUEUE_DEPTH


@dataclass
class SupervisorResult:
    """What one supervised run produced."""

    workers_started: int = 0
    candles_published: int = 0
    ticks_received: int = 0
    worker_exit_codes: dict[str, int] = field(default_factory=dict)
    dropped_events: dict[str, int] = field(default_factory=dict)
    #: Whether a shutdown signal ended the run, as opposed to the feed finishing.
    stopped_by_signal: bool = False
    #: False when the feed thread was still blocked after the grace period. The
    #: run is still shut down in every other respect; this records that the
    #: connection was left for process exit to reclaim rather than closed.
    clean_feed_shutdown: bool = True


class IntradayOptionsSupervisor:
    """Runs one shared feed and a set of paper workers to completion."""

    def __init__(
        self,
        config: SupervisorConfig,
        adapter: MarketFeedAdapter,
        notifier: Notifier | None = None,
    ) -> None:
        self._config = config
        self._hub = SharedFeedHub(adapter, interval_seconds=config.candle_interval_seconds)
        self._workers: list[tuple[WorkerConfig, WorkerChannel]] = []
        self._processes: dict[str, mp.process.BaseProcess] = {}
        # Wrapped once, here: a notification channel must never be able to abort a
        # shutdown, and the alarm this class raises is sent while the runtime is
        # already in trouble — the worst moment to discover an unwrapped timeout.
        self._notifier = SafeNotifier(notifier or NullNotifier())

    @property
    def hub(self) -> SharedFeedHub:
        return self._hub

    def add_worker(self, worker_config: WorkerConfig) -> WorkerChannel:
        """Register a strategy. Its queue is created here, before any spawn."""
        if worker_config.execution_mode is not ExecutionMode.PAPER:
            # Phase 1 has no live path at all. The broker factory would refuse
            # anyway; refusing here too means the supervisor never even spawns
            # a process that is guaranteed to fail.
            raise ValueError(
                f"Strategy {worker_config.strategy_id!r} requests "
                f"{worker_config.execution_mode.value} mode; Phase 1 is paper-only "
                "and live execution is not implemented."
            )
        channel = build_channel(
            worker_config.strategy_id,
            [worker_config.security_id],
            max_depth=self._config.queue_depth,
        )
        self._hub.register(channel)
        self._workers.append((worker_config, channel))
        return channel

    def run(
        self,
        *,
        join_timeout: float = 30.0,
        shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE,
    ) -> SupervisorResult:
        """Migrate, spawn workers, drive the feed, then shut down cleanly.

        Returns when the feed finishes on its own — an exhausted tape — or when a
        shutdown signal arrives. See the module docstring for the shutdown order.
        """
        result = SupervisorResult()
        lock = supervisor_lock(
            runtime_id=self._config.runtime_id,
            lock_dir=self._config.lock_dir,
            pid_dir=self._config.pid_dir,
        )
        try:
            lock.acquire()
        except DuplicateProcessError:
            _log.error("another supervisor already owns %s", self._config.runtime_id)
            raise

        # Migrate once, in the parent, before any worker opens the file. The
        # connection stays open afterwards now: the group's own session and
        # heartbeats are written through it for the life of the run.
        database = Database(self._config.database_path)
        MigrationRunner(database).run_pending()
        repository = ExecutionRepository(database)
        session = repository.open_session(
            runtime_id=self._config.runtime_id,
            strategy_id=None,  # a group-level row, not a strategy's
            execution_mode=ExecutionMode.PAPER,
            process_role=SUPERVISOR_ROLE,
            pid=os.getpid(),
        )
        heartbeat = HeartbeatWriter(
            repository,
            session_id=session.id,
            runtime_id=self._config.runtime_id,
            strategy_id=None,
        )
        heartbeat.beat(HealthState.STARTING, force=True)

        try:
            context = mp.get_context("spawn")
            for worker_config, channel in self._workers:
                process = context.Process(
                    target=run_worker,
                    args=(worker_config, channel.queue.raw),
                    name=f"{self._config.runtime_id}:{worker_config.strategy_id}",
                    daemon=False,
                )
                process.start()
                self._processes[worker_config.strategy_id] = process
                result.workers_started += 1
                _log.info(
                    "spawned worker strategy_id=%s pid=%s",
                    worker_config.strategy_id,
                    process.pid,
                )

            self._run_feed(
                result,
                heartbeat=heartbeat,
                repository=repository,
                shutdown_grace=shutdown_grace,
            )

            result.ticks_received = self._hub.tick_count
            result.candles_published = self._hub.candle_count

            # Sentinel per worker: tells a blocked consumer to stop waiting
            # rather than relying on its idle timeout.
            for _, channel in self._workers:
                channel.queue.publish(None)

            for strategy_id, worker_process in self._processes.items():
                worker_process.join(timeout=join_timeout)
                if worker_process.is_alive():
                    _log.warning("worker %s did not exit; terminating", strategy_id)
                    worker_process.terminate()
                    worker_process.join(timeout=5.0)
                result.worker_exit_codes[strategy_id] = worker_process.exitcode or 0

            for _, channel in self._workers:
                result.dropped_events[channel.strategy_id] = channel.queue.dropped

            self._publish_final_health(
                result,
                heartbeat=heartbeat,
                repository=repository,
                session_id=session.id,
            )
            return result
        finally:
            database.close()
            lock.release()

    def _publish_final_health(
        self,
        result: SupervisorResult,
        *,
        heartbeat: HeartbeatWriter,
        repository: ExecutionRepository,
        session_id: int,
    ) -> None:
        """Leave the group's health saying what actually happened.

        The dashboard reads the **latest** heartbeat, so the order here is the
        whole point: a run that ended with an unclosable feed must not overwrite
        its own ``DEGRADED`` alarm with a reassuring ``STOPPED`` on the way out. An
        operator looking at the tile an hour later needs to see the alarm, not the
        tidy exit that followed it.
        """
        if result.clean_feed_shutdown:
            heartbeat.beat(HealthState.STOPPING, force=True)
            heartbeat.beat(HealthState.STOPPED, force=True)
            reason = "signal" if result.stopped_by_signal else "clean_shutdown"
        else:
            # DEGRADED was already written, forced, when the feed failed to stop.
            # It stays the last word.
            reason = "feed_did_not_stop"
        repository.close_session(session_id, reason=reason)

    # ------------------------------------------------------------- the feed
    def _run_feed(
        self,
        result: SupervisorResult,
        *,
        heartbeat: HeartbeatWriter,
        repository: ExecutionRepository,
        shutdown_grace: float,
    ) -> None:
        """Drive the feed on its own thread until it finishes or is signalled."""
        #: Why a signal gets its own flag rather than being inferred from the feed
        #: thread still being alive: a thread that has just finished its work is
        #: briefly still alive while it unwinds, so inferring would report an
        #: ordinary end-of-tape run as signalled.
        signalled = threading.Event()
        #: Either of the two things worth waking up for.
        wake = threading.Event()
        failure: list[BaseException] = []

        def _drive() -> None:
            try:
                self._hub.start()
            except BaseException as exc:  # re-raised on the caller's thread below
                failure.append(exc)
            finally:
                # Whether it ended well or badly, the waiter must be released.
                wake.set()

        def _on_signal() -> None:
            signalled.set()
            wake.set()

        feed_thread = threading.Thread(
            target=_drive,
            name=f"{self._config.runtime_id}:feed",
            daemon=True,
        )

        with self._shutdown_signals(_on_signal):
            feed_thread.start()
            heartbeat.beat(HealthState.running_for(ExecutionMode.PAPER), force=True)
            # The main thread has nothing else to do while the feed runs, so it
            # spends the wait keeping the group's heartbeat fresh. Without this the
            # dashboard would show one beat at startup and then a heartbeat age
            # that climbs all session — indistinguishable from a dead supervisor.
            while not wake.wait(timeout=HEARTBEAT_POLL_SECONDS):
                self._beat_running(heartbeat)
            if signalled.is_set():
                result.stopped_by_signal = True
                _log.info("shutdown requested; asking the feed to finish")
                # Signal only. This thread does not own the adapter's loop.
                self._hub.request_stop()
            feed_thread.join(timeout=shutdown_grace)

        if feed_thread.is_alive():
            result.clean_feed_shutdown = False
            self._raise_silent_feed_alarm(heartbeat, repository, shutdown_grace)
        else:
            # The feed thread has returned, so nothing is writing to the
            # aggregators any more and the partial bars can be flushed safely.
            self._hub.stop()

        if failure:
            raise failure[0]

    def _beat_running(self, heartbeat: HeartbeatWriter) -> None:
        """One rate-limited liveness beat, carrying the group's queue picture."""
        stats = self._hub.queue_stats()
        heartbeat.beat(
            HealthState.running_for(ExecutionMode.PAPER),
            last_tick_at=self._hub.last_tick_at,
            # The worst queue in the group, not the sum: one wedged worker is the
            # thing worth seeing, and summing would let three healthy queues hide it.
            queue_depth=max((s.depth for s in stats), default=0),
            dropped_events=sum(s.dropped for s in stats),
        )

    def _raise_silent_feed_alarm(
        self,
        heartbeat: HeartbeatWriter,
        repository: ExecutionRepository,
        shutdown_grace: float,
    ) -> None:
        """Make an unclosable feed impossible to miss (runbook limitation 13).

        Three channels, because a log line is not an alarm: nobody is tailing the
        file at 15:31. The heartbeat drives the dashboard tile, the ``errors`` row
        gives that tile its message, and the notification reaches a human who is
        not looking at either.
        """
        message = (
            f"feed did not finish within {shutdown_grace:.1f}s of being asked to stop; "
            "the socket is connected but delivering nothing. The rest of the shutdown "
            "completed and the connection is left to process exit — it is NOT closed "
            "from this thread, which would hang. Check that the process actually exited."
        )
        _log.error("%s", message)
        # Forced, and deliberately the last heartbeat this run writes: see
        # _publish_final_health for why nothing may overwrite it with STOPPED.
        heartbeat.beat(HealthState.DEGRADED, force=True)
        repository.record_error(
            runtime_id=self._config.runtime_id,
            strategy_id=None,
            execution_mode=ExecutionMode.PAPER,
            severity="CRITICAL",
            component="feed",
            message=message,
        )
        self._notifier.send(
            NotificationEvent(
                event_type="feed_shutdown_unclean",
                message=message,
                runtime_id=self._config.runtime_id,
                execution_mode=ExecutionMode.PAPER,
            )
        )

    @contextmanager
    def _shutdown_signals(self, on_signal: Callable[[], None]) -> Iterator[None]:
        """Install shutdown handlers for the feed's lifetime, then put them back.

        Delegates to :func:`common.process.shutdown_signals`, which is where this
        method's body moved in Phase 3 Part 2b-i. Behaviour is unchanged; what
        changed is that it is no longer the *only* place a shutdown handler is
        installed. Part 2b gives the worker process an engine that must be told to
        square off, and two hand-rolled installers in one codebase is how the
        collision this phase exists to fix gets reintroduced — so there is now one
        implementation, and this is a caller of it.
        """
        with shutdown_signals(on_signal):
            yield
