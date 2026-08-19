"""Composition root for the ``positional_options`` runtime — one shared
Dhan feed hub, one child worker process per enabled strategy (Phase 5,
runtime generalization).

Mirrors ``runtimes.intraday_options.supervisor.IntradayOptionsSupervisor``'s
own shape directly: one :class:`~common.feed.hub.SharedFeedHub` over one
shared :class:`~common.market_data.adapter.MarketFeedAdapter`, one
:class:`~common.feed.hub.WorkerChannel` per enabled strategy
(:func:`~common.feed.hub.build_channel`, ``tick_channel=True`` always —
every positional worker is tick-driven, never candle-driven, so unlike the
intraday supervisor there is no fixture/non-engine path to make this
conditional on), one ``multiprocessing.get_context("spawn")`` child process
per worker (:func:`~runtimes.positional_options.worker.
run_positional_worker_process`), one control queue per worker draining
into ``hub.request_subscription`` on this supervisor's own thread — copied
from :meth:`~runtimes.intraday_options.supervisor.IntradayOptionsSupervisor.
_drain_control_queues` essentially verbatim, since it is already
strategy-agnostic. **N=1 — the single-strategy posture this runtime
originally shipped with — is simply this same machinery with one
registered channel; there is no separate single-worker code path.**

Production network construction (the one shared Dhan feed adapter) happens
once, in ``__main__.py``, never here — every network-facing object this
module needs is a parameter, so a test drives it with a fake adapter and
never touches the network. Each spawned child independently builds its own
scrip master/chain fetcher/margin fetcher — see ``worker.
run_positional_worker_process``'s own docstring for why (never a live
object pickled across the spawn boundary).

Migrations/retention/authentication all run exactly once, in
``__main__.main()``, strictly before this supervisor (or any worker)
exists — see that module's own docstring for the full startup order.

**Phase 6A: reconnect/stale-feed parity with ``IntradayOptionsSupervisor``,
reused rather than reimplemented.** The one shared adapter is wrapped in
:class:`~common.feed.reconnect.ReconnectingFeed` exactly as the intraday
supervisor already does (bounded backoff/jitter, automatic
segment-and-mode-aware resubscription via the real adapter's own
``resubscribe_all``, per-instrument staleness tracking); the
stuck-subscription/stale-instrument alarms and feed-health-event
persistence are ported near-verbatim from
``IntradayOptionsSupervisor``'s own methods of the same name. New here
(the intraday runtime has no equivalent, because its own engine's
per-candle/per-quote freshness gates were judged sufficient at the time):
every ``"disconnected"``/``"resubscribed"`` feed-health event is also
broadcast, in band on the existing tick channel, to *every* worker via
:meth:`~common.feed.hub.SharedFeedHub.broadcast_feed_gap` — each worker's
own engine latches a recoverable market-data-degraded gate on disconnect
and clears it only once it has observed fresh data, since the reconnect,
for everything it currently needs (see
``PositionalMultiLegEngine.on_feed_gap_notice``). ``ReconnectingFeed``/
``mark_feed_gap``/supervisor health alone protect candle-aggregation
integrity and operator visibility; they do not by themselves stop a
worker's own engine from acting on stale data, which is what the
propagated gate is for.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue as queue_module
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common.config import (
    ProjectPaths,
    Settings,
    discover_enabled_strategies,
    load_runtime_config,
    load_settings,
)
from common.config.models import ExecutionMode, RuntimeConfig
from common.execution import ExecutionRepository, check_mode_transition_safety
from common.feed import DEFAULT_TICK_MAX_DEPTH, FeedGapNotice, SharedFeedHub
from common.feed.hub import WorkerChannel, build_channel
from common.feed.reconnect import FeedHealthEvent, ReconnectingFeed
from common.health import DEFAULT_INTERVAL_SECONDS, HealthState, HeartbeatWriter
from common.logging import get_logger
from common.market_data.adapter import MarketFeedAdapter
from common.market_data.scrip_master import segment_code
from common.notifications import NotificationEvent, Notifier, NullNotifier, SafeNotifier
from common.persistence import Database, MigrationRunner
from common.process import DuplicateProcessError, shutdown_signals, supervisor_lock
from common.utils.timeutils import local_date_in, now_ist

from .config_adapter import WorkerConfig, build_worker_config
from .worker import EXIT_DUPLICATE, NOTIFIER_FROM_SETTINGS, run_positional_worker_process

log = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1

#: Queue depth per worker's candle channel — never actually consumed by a
#: positional worker (it is tick-driven), but ``build_channel`` always
#: creates one; a modest depth just bounds the harmless overflow-log
#: chatter rather than mattering functionally.
DEFAULT_QUEUE_DEPTH = 64

#: How long to wait for the feed thread to return after a stop is requested.
DEFAULT_SHUTDOWN_GRACE = 10.0

#: How often the idle main thread beats while the feed runs.
HEARTBEAT_POLL_SECONDS = 1.0

#: ``process_role`` for the group-level session — distinguishes it from the
#: ``worker`` sessions each spawned child opens for itself.
SUPERVISOR_ROLE = "supervisor"

#: Mirrors ``IntradayOptionsSupervisor``'s own threshold (limitation 15):
#: a runtime subscription request older than this, still unapplied, means
#: the feed is connected but delivering nothing to this group.
STUCK_SUBSCRIPTION_SECONDS = 30.0

#: Feed-health event kinds worth a notification, not just a `feed_events`
#: row — mirrors ``IntradayOptionsSupervisor``'s own
#: ``_CHAT_WORTHY_FEED_EVENTS`` exactly (the other event kinds already have
#: their own, more specific alarms: ``_check_stuck_subscription``/
#: ``_raise_silent_feed_alarm``).
_CHAT_WORTHY_FEED_EVENTS = frozenset({"disconnected", "recovered", "reconnect_exhausted"})


def _parse_subscription_request(request: object) -> tuple[str, int | None, int | None] | None:
    """Normalise one control-queue entry into ``(security_id, segment, mode)``.

    Identical shape and reasoning to ``runtimes.intraday_options.
    supervisor._parse_subscription_request`` (not imported: that module
    pulls in intraday-only dependencies this one must not depend on) — a
    bare id, or an ``(id, segment)``/``(id, segment, mode)`` tuple.
    """
    if isinstance(request, str):
        return (request, None, None) if request else None
    if isinstance(request, tuple) and len(request) in (2, 3):
        security_id = request[0]
        if not isinstance(security_id, str) or not security_id:
            return None
        segment = request[1]
        mode = request[2] if len(request) == 3 else None
        if _optional_code(segment) and _optional_code(mode):
            return security_id, segment, mode
    return None


def _optional_code(value: object) -> bool:
    """True when ``value`` is ``None`` or a plain int (``bool`` is not an int here)."""
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def _database_path(paths: ProjectPaths, runtime_cfg: RuntimeConfig, runtime_id: str) -> Path:
    if runtime_cfg.database:
        return paths.project_root / runtime_cfg.database
    return paths.database_path(runtime_id)


@dataclass
class PositionalSupervisorConfig:
    """Group-level configuration."""

    runtime_id: str
    database_path: Path
    lock_dir: Path
    pid_dir: Path
    log_dir: Path
    runtime_root: Path
    cache_dir: Path
    queue_depth: int = DEFAULT_QUEUE_DEPTH
    tick_queue_depth: int = DEFAULT_TICK_MAX_DEPTH
    heartbeat_interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    #: ``SharedFeedHub`` always builds a completed-candle aggregator per
    #: instrument, even though nothing here ever reads it — see the module
    #: docstring. The value is otherwise inert for this runtime.
    candle_interval_seconds: int = 60


@dataclass
class PositionalSupervisorResult:
    """What one supervised run produced."""

    workers_started: int = 0
    ticks_received: int = 0
    worker_exit_codes: dict[str, int] = field(default_factory=dict)
    #: Drops per channel — the candle channel (never consumed, see the
    #: module docstring) under ``strategy_id``, the tick channel under
    #: ``f"{strategy_id}:ticks"``.
    dropped_events: dict[str, int] = field(default_factory=dict)
    stopped_by_signal: bool = False
    #: False when the feed thread was still blocked after the grace period
    #: — see ``IntradayOptionsSupervisor``'s own field of the same name for
    #: the full reasoning this mirrors.
    clean_feed_shutdown: bool = True
    #: True once a runtime subscription has been waiting
    #: ``STUCK_SUBSCRIPTION_SECONDS`` unapplied — mirrors
    #: ``IntradayOptionsSupervisor``'s own field of the same name; see
    #: ``_check_stuck_subscription``.
    stopped_by_stuck_subscription: bool = False


class PositionalOptionsSupervisor:
    """Runs one shared feed and a set of positional paper workers."""

    def __init__(
        self,
        config: PositionalSupervisorConfig,
        adapter: MarketFeedAdapter,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] = now_ist,
    ) -> None:
        self._config = config
        #: Threaded to every spawned worker's own ``run_worker`` clock.
        #: Defaults to the real wall clock — production never overrides
        #: this. Exists so a test spawning a real worker process against a
        #: scripted, non-real-time tick stream can supply a picklable clock
        #: whose "now" tracks that same simulated timeline (Phase 6A: a
        #: worker's own ``on_poll`` evaluates independent of the last tick
        #: it saw) — see ``tests.integration._weekly_delta_neutral_fixtures.
        #: OffsetClock``.
        self._clock = clock
        # The adapter is wrapped rather than used bare — see the module
        # docstring for why this now matches IntradayOptionsSupervisor.
        # Safe for the recorded/fake adapter every test uses:
        # ReconnectingFeed._run treats a clean return as "the tape is
        # finished", never as a failure to retry.
        self._feed = ReconnectingFeed(adapter, on_feed_gap=lambda: self._hub.mark_feed_gap())
        self._hub = SharedFeedHub(self._feed, interval_seconds=config.candle_interval_seconds)
        self._workers: list[tuple[WorkerConfig, WorkerChannel]] = []
        self._processes: dict[str, mp.process.BaseProcess] = {}
        #: Upstream control queues, one per worker — the child puts a
        #: security_id (or ``(id, segment, mode)``) on its queue when its
        #: engine subscribes to a contract at runtime; this process drains
        #: them into the hub. See :meth:`_drain_control_queues`.
        self._control_queues: dict[str, Any] = {}
        self._notifier = SafeNotifier(notifier or NullNotifier())
        #: Latched once the stuck-subscription alarm has fired, so a
        #: condition that persists produces one notification, not one per
        #: poll. Mirrors ``IntradayOptionsSupervisor`` exactly.
        self._stuck_subscription_alarmed = False
        #: Security ids already reported stale — one ``feed_events`` row per
        #: stale spell, not one per poll. Mirrors ``IntradayOptionsSupervisor``.
        self._stale_instruments_alarmed: set[str] = set()
        #: The feed's own health events land here, not in a repository call
        #: directly — the feed runs on its own thread, and this
        #: repository's sqlite3 connection belongs to the thread that calls
        #: :meth:`run`. See :meth:`_drain_feed_health_events`.
        self._feed_health_events: queue_module.Queue[FeedHealthEvent] = queue_module.Queue()

    @property
    def hub(self) -> SharedFeedHub:
        return self._hub

    def control_queue(self, strategy_id: str) -> Any | None:
        return self._control_queues.get(strategy_id)

    def add_worker(self, worker_config: WorkerConfig) -> WorkerChannel:
        """Register one strategy under the shared hub.

        Always opts into a tick channel: every positional worker reads its
        ticks from :class:`~common.engine.hub_feed.HubTickFeed`, never from
        the candle channel (see the module docstring) — there is no
        fixture/non-engine path here to make this conditional on, unlike
        :meth:`~runtimes.intraday_options.supervisor.
        IntradayOptionsSupervisor.add_worker`.
        """
        underlying_segment = segment_code(worker_config.underlying_segment)
        channel = build_channel(
            worker_config.strategy_id,
            [worker_config.underlying_security_id],
            max_depth=self._config.queue_depth,
            tick_channel=True,
            tick_max_depth=self._config.tick_queue_depth,
            segment=underlying_segment,
            # The underlying stays on the adapter's own default (Ticker)
            # mode — putting an index on Full would gain nothing and cost
            # a wider frame per tick; see worker._request_subscription's
            # own docstring for the symmetric option-side reasoning.
            mode=None,
        )
        self._hub.register(channel)
        self._workers.append((worker_config, channel))
        self._control_queues[worker_config.strategy_id] = mp.get_context("spawn").Queue()
        return channel

    def run(
        self,
        *,
        join_timeout: float = 30.0,
        shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE,
    ) -> PositionalSupervisorResult:
        """Migrate, spawn workers, drive the feed, then shut down cleanly."""
        result = PositionalSupervisorResult()
        lock = supervisor_lock(
            runtime_id=self._config.runtime_id,
            lock_dir=self._config.lock_dir,
            pid_dir=self._config.pid_dir,
        )
        try:
            lock.acquire()
        except DuplicateProcessError:
            log.error("another supervisor already owns %s", self._config.runtime_id)
            raise

        # Migrate once, in the parent, before any worker opens the file —
        # a no-op replay when __main__.main() already migrated it; this
        # function is also called directly by tests.
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
            interval_seconds=self._config.heartbeat_interval_seconds,
        )
        heartbeat.beat(HealthState.STARTING, force=True)
        self._notifier.send(
            NotificationEvent(
                event_type="runtime_started",
                message=f"{self._config.runtime_id} supervisor starting",
                runtime_id=self._config.runtime_id,
                execution_mode=ExecutionMode.PAPER,
            )
        )
        self._notifier.set_on_failure(
            lambda event, reason: repository.record_notification(
                runtime_id=self._config.runtime_id,
                strategy_id=event.strategy_id,
                execution_mode=event.execution_mode,
                channel=self._notifier.channel,
                event_type=event.event_type,
                message=event.rendered(),
                delivered=False,
                failure_reason=reason,
            )
        )

        # Late-bound: the feed was constructed in __init__, before this
        # repository existed — mirrors IntradayOptionsSupervisor's own
        # comment and reasoning verbatim. The sink only enqueues for the DB
        # write below and broadcasts to every worker (both safe on the feed
        # thread) — it must never touch this thread's own sqlite3 connection
        # directly.
        self._feed.set_health_event_sink(self._on_feed_health_event)

        try:
            context = mp.get_context("spawn")
            for worker_config, channel in self._workers:
                process = context.Process(
                    target=run_positional_worker_process,
                    args=(
                        worker_config,
                        channel.tick_queue.raw if channel.tick_queue is not None else None,
                        self._control_queues.get(worker_config.strategy_id),
                        NOTIFIER_FROM_SETTINGS,
                        self._clock,
                    ),
                    name=f"{self._config.runtime_id}:{worker_config.strategy_id}",
                    daemon=False,
                )
                process.start()
                self._processes[worker_config.strategy_id] = process
                result.workers_started += 1
                log.info(
                    "spawned worker strategy_id=%s pid=%s",
                    worker_config.strategy_id,
                    process.pid,
                )

            self._run_feed(
                result, heartbeat=heartbeat, repository=repository, shutdown_grace=shutdown_grace
            )
            result.ticks_received = self._hub.tick_count

            # Sentinel per worker, on every channel it has — tells a
            # blocked consumer to stop waiting. Both channels, even though
            # a positional worker never drains the candle one: cheap, and
            # keeps this loop identical in shape to the intraday
            # supervisor's own.
            for _, channel in self._workers:
                channel.queue.publish(None)
                if channel.tick_queue is not None:
                    channel.tick_queue.publish(None)

            for strategy_id, worker_process in self._processes.items():
                worker_process.join(timeout=join_timeout)
                if worker_process.is_alive():
                    log.warning("worker %s did not exit; terminating", strategy_id)
                    worker_process.terminate()
                    worker_process.join(timeout=5.0)
                exit_code = worker_process.exitcode or 0
                result.worker_exit_codes[strategy_id] = exit_code
                if exit_code == EXIT_DUPLICATE:
                    self._report_duplicate_worker(strategy_id, repository)

            for _, channel in self._workers:
                result.dropped_events[channel.strategy_id] = channel.queue.dropped
                if channel.tick_queue is not None:
                    result.dropped_events[f"{channel.strategy_id}:ticks"] = (
                        channel.tick_queue.dropped
                    )

            self._publish_final_health(
                result, heartbeat=heartbeat, repository=repository, session_id=session.id
            )
            return result
        finally:
            self._release_queues()
            database.close()
            lock.release()

    def _release_queues(self) -> None:
        """Stop undelivered events from holding this process open at exit —
        see ``IntradayOptionsSupervisor._release_queues``'s own docstring
        for the full ``cancel_join_thread`` reasoning this mirrors
        verbatim."""
        for _, channel in self._workers:
            for queue in (channel.queue, channel.tick_queue):
                if queue is None:
                    continue
                cancel = getattr(queue.raw, "cancel_join_thread", None)
                if cancel is not None:
                    cancel()
        for control_queue in self._control_queues.values():
            cancel = getattr(control_queue, "cancel_join_thread", None)
            if cancel is not None:
                cancel()

    def _report_duplicate_worker(self, strategy_id: str, repository: ExecutionRepository) -> None:
        """A worker refused to start — another process already held its lock.

        Mirrors ``IntradayOptionsSupervisor._report_duplicate_worker``
        exactly: without this, a refused worker is a silent zero-length
        run — ``worker_lock`` already protects the database; this is what
        makes the *group* notice."""
        message = (
            f"{strategy_id} did not start: another process already holds its worker lock "
            "(exit code EXIT_DUPLICATE). No orders were placed for this strategy this run."
        )
        log.error("%s", message)
        repository.record_error(
            runtime_id=self._config.runtime_id,
            strategy_id=strategy_id,
            execution_mode=ExecutionMode.PAPER,
            severity="CRITICAL",
            component="supervisor.duplicate_worker",
            message=message,
        )
        self._notifier.send(
            NotificationEvent(
                event_type="duplicate_worker_refused",
                message=message,
                runtime_id=self._config.runtime_id,
                strategy_id=strategy_id,
                execution_mode=ExecutionMode.PAPER,
            )
        )

    def _publish_final_health(
        self,
        result: PositionalSupervisorResult,
        *,
        heartbeat: HeartbeatWriter,
        repository: ExecutionRepository,
        session_id: int,
    ) -> None:
        if result.clean_feed_shutdown:
            heartbeat.beat(HealthState.STOPPING, force=True)
            heartbeat.beat(HealthState.STOPPED, force=True)
            reason = "signal" if result.stopped_by_signal else "clean_shutdown"
            self._notifier.send(
                NotificationEvent(
                    event_type="runtime_stopped",
                    message=f"{self._config.runtime_id} supervisor stopped ({reason})",
                    runtime_id=self._config.runtime_id,
                    execution_mode=ExecutionMode.PAPER,
                )
            )
        else:
            heartbeat.beat(HealthState.DEGRADED, force=True)
            reason = "feed_did_not_stop"
        repository.close_session(session_id, reason=reason)

    # ------------------------------------------------------------- the feed
    def _run_feed(
        self,
        result: PositionalSupervisorResult,
        *,
        heartbeat: HeartbeatWriter,
        repository: ExecutionRepository,
        shutdown_grace: float,
    ) -> None:
        """Drive the feed on its own thread until it finishes or is signalled.

        Mirrors ``IntradayOptionsSupervisor._run_feed``'s own shape,
        including the stuck-subscription/stale-instrument alarms (Phase 6A
        parity — see the module docstring)."""
        signalled = threading.Event()
        wake = threading.Event()
        failure: list[BaseException] = []

        def _drive() -> None:
            try:
                self._hub.start()
            except BaseException as exc:  # re-raised on the caller's thread below
                failure.append(exc)
            finally:
                wake.set()

        def _on_signal() -> None:
            signalled.set()
            wake.set()

        feed_thread = threading.Thread(
            target=_drive, name=f"{self._config.runtime_id}:feed", daemon=True
        )

        with self._shutdown_signals(_on_signal):
            feed_thread.start()
            heartbeat.beat(HealthState.running_for(ExecutionMode.PAPER), force=True)
            while not wake.wait(timeout=HEARTBEAT_POLL_SECONDS):
                self._drain_control_queues()
                self._drain_feed_health_events(repository)
                if self._check_stuck_subscription(heartbeat, repository):
                    # Connected but delivering nothing long enough that no
                    # worker's own runtime subscription can ever be applied
                    # — end the run rather than sit RUNNING_PAPER forever
                    # with every engine unable to act. Mirrors
                    # IntradayOptionsSupervisor exactly.
                    result.stopped_by_stuck_subscription = True
                    log.info("stuck subscription threshold crossed; asking the feed to finish")
                    self._hub.request_stop()
                    break
                self._check_stale_instruments(repository)
                self._beat_running(heartbeat)
            # Once more after the wake: a request — or a health event —
            # that arrived in the same instant the feed finished is still
            # worth applying.
            self._drain_control_queues()
            self._drain_feed_health_events(repository)
            if signalled.is_set():
                result.stopped_by_signal = True
                log.info("shutdown requested; asking the feed to finish")
                self._hub.request_stop()
            feed_thread.join(timeout=shutdown_grace)

        if feed_thread.is_alive():
            result.clean_feed_shutdown = False
            self._raise_silent_feed_alarm(heartbeat, repository, shutdown_grace)
        else:
            self._hub.stop()

        if failure:
            raise failure[0]

    def _drain_control_queues(self) -> None:
        """Forward workers' runtime subscription requests to the hub.

        Verbatim copy of ``IntradayOptionsSupervisor._drain_control_
        queues``'s own logic (not imported: importing across the two
        supervisor modules would couple them for no reason — this loop is
        already fully generic, five lines, and the two runtimes must be
        free to diverge later without touching each other)."""
        for strategy_id, control_queue in self._control_queues.items():
            while True:
                try:
                    request = control_queue.get_nowait()
                except queue_module.Empty:
                    break
                parsed = _parse_subscription_request(request)
                if parsed is None:
                    log.warning(
                        "ignoring a malformed subscription request from %s: %r",
                        strategy_id,
                        request,
                    )
                    continue
                security_id, segment, mode = parsed
                self._hub.request_subscription(strategy_id, security_id, segment=segment, mode=mode)

    def _beat_running(self, heartbeat: HeartbeatWriter) -> None:
        stats = self._hub.queue_stats()
        heartbeat.beat(
            HealthState.running_for(ExecutionMode.PAPER),
            last_tick_at=self._hub.last_tick_at,
            queue_depth=max((s.depth for s in stats), default=0),
            dropped_events=sum(s.dropped for s in stats),
        )

    # -------------------------------------------------------- feed health
    def _on_feed_health_event(self, event: FeedHealthEvent) -> None:
        """Runs on the feed thread (``ReconnectingFeed``'s own callback) —
        never touches the repository directly (see :meth:`run`'s own
        comment). Two things, both safe here: enqueue for the durable
        ``feed_events`` write :meth:`_drain_feed_health_events` performs on
        the thread that owns the sqlite connection, and — Phase 6A, the one
        real addition beyond ``IntradayOptionsSupervisor`` parity — broadcast
        a :class:`~common.feed.queues.FeedGapNotice` to every worker so each
        one's own engine can latch/track its recoverable degraded gate. The
        broadcast is safe here specifically because it is a tick-channel
        publish, exactly like a real tick, and this *is* the thread that
        publishes real ticks.
        """
        self._feed_health_events.put(event)
        if event.event in ("disconnected", "resubscribed"):
            self._hub.broadcast_feed_gap(FeedGapNotice(kind=event.event, at=datetime.now(UTC)))

    def _drain_feed_health_events(self, repository: ExecutionRepository) -> None:
        """Write every queued ``feed_events`` row on this (the repository's)
        thread. Mirrors ``IntradayOptionsSupervisor._drain_feed_health_
        events`` exactly: a write that raises is logged and dropped, never
        allowed to break the poll loop — a ``feed_events`` row is
        diagnostic, not something the run depends on."""
        while True:
            try:
                event = self._feed_health_events.get_nowait()
            except queue_module.Empty:
                break
            try:
                repository.record_feed_event(runtime_id=self._config.runtime_id, **asdict(event))
            except Exception:
                log.exception("failed to persist a feed_events row (event=%s)", event.event)
            if event.event in _CHAT_WORTHY_FEED_EVENTS:
                self._notifier.send(
                    NotificationEvent(
                        event_type=f"feed_{event.event}",
                        message=event.reason or event.event,
                        runtime_id=self._config.runtime_id,
                        execution_mode=ExecutionMode.PAPER,
                    )
                )

    def _check_stuck_subscription(
        self, heartbeat: HeartbeatWriter, repository: ExecutionRepository
    ) -> bool:
        """Alarm when a runtime subscription has not been applied. Mirrors
        ``IntradayOptionsSupervisor._check_stuck_subscription`` exactly —
        see that method's own docstring for the full reasoning (the hub
        applies a pending subscription only on a real tick, so a feed
        delivering nothing never applies one, and an engine waiting on a
        contract it just chose will not fill against a cached price).
        Raised once per run; returns ``True`` exactly once, on the poll that
        first crosses the threshold."""
        age = self._hub.pending_subscription_age_seconds()
        if age < STUCK_SUBSCRIPTION_SECONDS:
            return False
        if self._stuck_subscription_alarmed:
            return False
        self._stuck_subscription_alarmed = True

        message = (
            f"a runtime subscription has been waiting {age:.1f}s to be applied "
            f"(threshold {STUCK_SUBSCRIPTION_SECONDS:.0f}s). The feed is delivering "
            "nothing for this group. Any engine waiting on a contract it just chose "
            "will not enter — it wants a fresh tick and will not use a cached price. "
            "Ending the run rather than continuing to present it as healthy; check "
            "the feed connection."
        )
        log.error("%s", message)
        heartbeat.beat(HealthState.DEGRADED, force=True)
        repository.record_error(
            runtime_id=self._config.runtime_id,
            strategy_id=None,
            execution_mode=ExecutionMode.PAPER,
            severity="CRITICAL",
            component="feed",
            message=message,
        )
        repository.record_feed_event(
            runtime_id=self._config.runtime_id, event="degraded", reason=message
        )
        self._notifier.send(
            NotificationEvent(
                event_type="subscription_not_applied",
                message=message,
                runtime_id=self._config.runtime_id,
                execution_mode=ExecutionMode.PAPER,
            )
        )
        return True

    def _check_stale_instruments(self, repository: ExecutionRepository) -> None:
        """Write a ``feed_events`` row for each newly-stale instrument.
        Mirrors ``IntradayOptionsSupervisor._check_stale_instruments``
        exactly — diagnostic only (a quiet far-OTM strike is not itself a
        broken feed), latched per instrument until a fresh tick clears it."""
        stale = set(self._feed.health.stale_instruments())
        newly_stale = stale - self._stale_instruments_alarmed
        for security_id in sorted(newly_stale):
            repository.record_feed_event(
                runtime_id=self._config.runtime_id,
                event="stale_instrument",
                security_id=security_id,
            )
        self._stale_instruments_alarmed = stale

    def _raise_silent_feed_alarm(
        self, heartbeat: HeartbeatWriter, repository: ExecutionRepository, shutdown_grace: float
    ) -> None:
        """Make an unclosable feed impossible to miss. Mirrors
        ``IntradayOptionsSupervisor._raise_silent_feed_alarm`` exactly —
        three channels (heartbeat, ``errors`` row, notification), because a
        log line is not an alarm."""
        message = (
            f"feed did not finish within {shutdown_grace:.1f}s of being asked to stop; "
            "the socket is connected but delivering nothing. The rest of the shutdown "
            "completed and the connection is left to process exit — it is NOT closed "
            "from this thread, which would hang. Check that the process actually exited."
        )
        log.error("%s", message)
        heartbeat.beat(HealthState.DEGRADED, force=True)
        repository.record_error(
            runtime_id=self._config.runtime_id,
            strategy_id=None,
            execution_mode=ExecutionMode.PAPER,
            severity="CRITICAL",
            component="feed",
            message=message,
        )
        repository.record_feed_event(
            runtime_id=self._config.runtime_id, event="degraded", reason=message
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
        with shutdown_signals(on_signal):
            yield


def build_positional_supervisor(
    *,
    runtime_id: str,
    config_root: Path,
    paths: ProjectPaths,
    adapter: MarketFeedAdapter,
    settings: Settings | None = None,
    trading_date: str | None = None,
    strategy_ids: frozenset[str] | None = None,
    notifier: Notifier | None = None,
    chain_fetcher_factory: str | None = None,
    scrip_master_factory: str | None = None,
    margin_fetcher_factory: str | None = None,
) -> PositionalOptionsSupervisor:
    """Discover this runtime's enabled strategies, admit them, build the group.

    Deliberately separable from ``__main__.main()``'s CLI/credential
    handling — a caller that already has an adapter (a test, or a future
    caller reusing this wiring) drives this directly. Assumes the caller
    has already checked that the runtime itself is enabled.

    ``strategy_ids``, when given, admits only strategies whose id is in the
    set — the rest are discovered and skipped, never refused as blocked.
    This is ``scripts/start_strategy.py``'s own mechanism (mirrors
    ``intraday_options.__main__.build_supervisor``'s identical parameter):
    a per-strategy start still goes through a supervisor, exactly as an
    unfiltered start does.

    ``chain_fetcher_factory``/``scrip_master_factory``/
    ``margin_fetcher_factory``, when given, apply to *every* admitted
    worker's own :class:`~runtimes.positional_options.config_adapter.
    WorkerConfig` — a test-only override (see that class's own docstring);
    production ``__main__.py`` never passes these, so every real strategy
    always builds the real Dhan sources.
    """
    settings = settings if settings is not None else load_settings()
    trading_date = trading_date or local_date_in(now_ist()).isoformat()
    runtime_cfg = load_runtime_config(config_root, runtime_id)
    database_path = _database_path(paths, runtime_cfg, runtime_id)

    supervisor = PositionalOptionsSupervisor(
        PositionalSupervisorConfig(
            runtime_id=runtime_id,
            database_path=database_path,
            lock_dir=paths.lock_root,
            pid_dir=paths.pid_root,
            log_dir=paths.log_root,
            runtime_root=paths.runtime_root,
            cache_dir=paths.cache_root,
            heartbeat_interval_seconds=runtime_cfg.health.heartbeat_interval_seconds,
        ),
        adapter,
        notifier=notifier,
    )

    # Defense in depth, exactly as the pre-Phase-5 single-worker
    # composition root ran this before admitting its one worker: live is
    # structurally unreachable for this runtime (build_worker_config
    # raises on mode: live before any worker exists, and no live broker
    # class is ever constructed here), so there is no broker to check
    # broker-confirmed exposure against — only a hand-edited database
    # could ever make this fail. A strategy that fails it is skipped, not
    # a reason to refuse the whole group — the rest of the group continues
    # (spec's own "one strategy's problem must not stop its siblings").
    transition_database = Database(database_path)
    try:
        MigrationRunner(transition_database).run_pending()
        transition_repository = ExecutionRepository(transition_database)

        for cfg in discover_enabled_strategies(config_root, runtime_id, settings=settings):
            if strategy_ids is not None and cfg.strategy.strategy_id not in strategy_ids:
                continue
            decision = check_mode_transition_safety(
                transition_repository,
                strategy_id=cfg.strategy.strategy_id,
                runtime_id=runtime_id,
                new_mode=cfg.strategy.mode,
                broker=None,
                reconciliation_runner=None,
            )
            if not decision.allowed:
                log.error(
                    "refusing to admit strategy_id=%s this session: mode transition unsafe: %s",
                    cfg.strategy.strategy_id,
                    decision.reason,
                )
                continue

            worker_config = build_worker_config(
                cfg,
                trading_date=trading_date,
                database_path=database_path,
                lock_dir=paths.lock_root,
                pid_dir=paths.pid_root,
                log_dir=paths.log_root,
                runtime_root=paths.runtime_root,
                cache_dir=paths.cache_root,
                chain_fetcher_factory=chain_fetcher_factory,
                scrip_master_factory=scrip_master_factory,
                margin_fetcher_factory=margin_fetcher_factory,
            )
            supervisor.add_worker(worker_config)
    finally:
        transition_database.close()

    return supervisor
