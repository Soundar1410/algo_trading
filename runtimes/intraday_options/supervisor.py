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
import queue as queue_module
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from common.config.models import ExecutionMode, LiveGateDecision
from common.execution import ExecutionRepository, strategy_token
from common.execution.health_events import auth_event_for_source
from common.feed import DEFAULT_TICK_MAX_DEPTH, SharedFeedHub
from common.feed.hub import WorkerChannel, build_channel
from common.feed.reconnect import FeedHealthEvent, ReconnectingFeed
from common.health import DEFAULT_INTERVAL_SECONDS, HealthState, HeartbeatWriter
from common.logging import get_logger
from common.market_data.adapter import MarketFeedAdapter
from common.notifications import NotificationEvent, Notifier, NullNotifier, SafeNotifier
from common.persistence import Database, MigrationRunner
from common.process import DuplicateProcessError, shutdown_signals, supervisor_lock

from .worker import EXIT_DUPLICATE, NOTIFIER_FROM_SETTINGS, WorkerConfig, run_worker_process

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

#: How long a runtime subscription may sit unapplied before it is an incident
#: (runbook limitation 15). Generous against the mechanism it watches: the hub
#: applies pending subscriptions on any tick, and during market hours frames
#: arrive continuously, so reaching this means the group has received **no**
#: tick at all for that long — which is already an incident by itself.
STUCK_SUBSCRIPTION_SECONDS = 30.0

#: feed_events whose occurrence is itself worth telling a human about, as
#: opposed to the other five (connected, reconnect_attempted, resubscribed,
#: stale_instrument, degraded) which stay diagnostic-only — every reconnect
#: *attempt* during a flapping connection, or the routine "connected" a normal
#: reconnect ends with, would be exactly the noise spec 2539 ("do not send
#: every tick, candle or heartbeat") and 2554 ("aggregate repeated errors")
#: warn against. ``degraded`` is deliberately excluded here too: the two
#: alarms that already write it (_check_stuck_subscription,
#: _raise_silent_feed_alarm) already send their own, more specific
#: notification alongside it.
_CHAT_WORTHY_FEED_EVENTS = frozenset({"disconnected", "recovered", "reconnect_exhausted"})


def _parse_subscription_request(request: object) -> tuple[str, int | None, int | None] | None:
    """Normalise one control-queue entry into ``(security_id, segment, mode)``.

    Three shapes are accepted, and all three are real:

    * ``"49081"`` — a bare id, meaning "the hub's defaults". This is what every
      pre-Phase-4 sender put on the queue, and the recorded-tape paths still do.
    * ``("49081", 2)`` — an id and the numeric exchange segment it lives in.
      Phase 4 Part 1 added this so an option contract chosen mid-session is
      subscribed on ``NSE_FNO`` rather than on the underlying's ``IDX_I``, where
      it would be accepted and then deliver nothing.
    * ``("49081", 2, 21)`` — the same plus the subscription mode. Part 5 added
      this so the contract arrives in Full mode, the only one carrying a bid/ask
      book; on Ticker it streams prices the fill model can price only off LTP.

    The two-element shape is still accepted rather than migrated, because the
    queue can hold entries written by a worker that started before an upgrade.

    Returns ``None`` for anything else, so the caller can log and drop it. The
    child that sent a malformed entry is the one with the problem; taking the
    whole group down with it would be the worse outcome.
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
    #: Depth of a worker's opt-in tick queue. Sized from a measured tick rate
    #: rather than the candle queue's assumptions — see
    #: :data:`common.feed.queues.DEFAULT_TICK_MAX_DEPTH`.
    tick_queue_depth: int = DEFAULT_TICK_MAX_DEPTH
    #: From ``RuntimeConfig.health.heartbeat_interval_seconds`` — see
    #: :mod:`common.config.models`. Defaulted independently rather than
    #: importing the config layer here, so a direct construction (every
    #: existing test) keeps working unchanged.
    heartbeat_interval_seconds: float = DEFAULT_INTERVAL_SECONDS


@dataclass
class SupervisorResult:
    """What one supervised run produced."""

    workers_started: int = 0
    candles_published: int = 0
    ticks_received: int = 0
    worker_exit_codes: dict[str, int] = field(default_factory=dict)
    #: Drops per channel. ``strategy_id`` is the **candle** channel;
    #: ``f"{strategy_id}:ticks"`` is the tick channel, present only for a worker that
    #: opted into one. Kept apart rather than summed because the two mean different
    #: things: a dropped tick latches that worker's entries off for the day (runbook
    #: limitation 14), while a dropped candle does not, and one number for both would
    #: hide which happened.
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
        # The adapter is wrapped rather than used bare, and Phase 4 Part 3 is when
        # that started being true. `ReconnectingFeed` had **no constructor call
        # anywhere outside tests**, so everything Phase 2 built — bounded backoff
        # with jitter, resubscription after a new socket, 807 token refresh — was
        # unreachable in the deployed runtime, and so was `on_feed_gap`. That last
        # one is why it matters here: `mark_feed_gap` is the whole of limitation
        # 4's mitigation, and nothing was calling it.
        #
        # The lambda defers the `self._hub` lookup to call time, which is what
        # lets the two refer to each other without an ordering problem.
        #
        # Safe for the recorded adapter every test uses: `ReconnectingFeed._run`
        # treats a clean return as "the tape is finished", not as a failure to
        # retry — see its comment, which names this case explicitly.
        self._feed = ReconnectingFeed(adapter, on_feed_gap=lambda: self._hub.mark_feed_gap())
        self._hub = SharedFeedHub(self._feed, interval_seconds=config.candle_interval_seconds)
        self._workers: list[tuple[WorkerConfig, WorkerChannel]] = []
        self._processes: dict[str, mp.process.BaseProcess] = {}
        #: Live-mode strategies :meth:`add_worker` refused rather than spawned,
        #: with the human-readable reason. Recorded to persistence once ``run()``
        #: opens the repository — see the mixed-mode admission gate below.
        self._blocked_workers: list[tuple[WorkerConfig, str]] = []
        #: strategy_token(strategy_id) -> the first strategy_id that claimed
        #: it, so :meth:`add_worker` can refuse a second strategy whose
        #: correlation-ID token would collide with an already-admitted one —
        #: see :func:`~common.execution.correlation.strategy_token`'s own
        #: docstring (D78) for the crash this replaces.
        self._admitted_strategy_tokens: dict[str, str] = {}
        #: Strategies refused for exactly that reason. Reported the same way
        #: as ``_blocked_workers``, under its own event type — this is not a
        #: live-gate decision and must not be reported as one.
        self._token_collision_refusals: list[tuple[WorkerConfig, str]] = []
        #: Upstream control queues, one per tick-channel worker. The child puts a
        #: security_id on its queue when its engine subscribes to a contract at
        #: runtime; this process drains them and asks the hub, which applies the
        #: request on the feed thread. See :meth:`_drain_control_queues`.
        self._control_queues: dict[str, Any] = {}
        # Wrapped once, here: a notification channel must never be able to abort a
        # shutdown, and the alarm this class raises is sent while the runtime is
        # already in trouble — the worst moment to discover an unwrapped timeout.
        self._notifier = SafeNotifier(notifier or NullNotifier())
        #: Latched once the limitation-15 alarm has fired, so a condition that
        #: persists produces one notification rather than one per poll.
        self._stuck_subscription_alarmed = False
        #: Security IDs already reported stale, so a `feed_events` row is written
        #: once per instrument per stale spell rather than once per poll. An
        #: instrument that later gets a fresh tick drops out on its own — see
        #: :meth:`_check_stale_instruments`.
        self._stale_instruments_alarmed: set[str] = set()
        #: Set via :meth:`set_startup_auth_outcome` before :meth:`run`, since the
        #: token is obtained before this object's repository exists. ``None``
        #: means no outcome was recorded — a caller that never calls the setter
        #: (every existing test) gets the previous, unaudited behaviour.
        self._startup_auth_outcome: tuple[str, str | None, int] | None = None
        #: The feed's health events land here, not in a repository call directly:
        #: the feed runs on its own thread (module docstring), and the repository's
        #: sqlite3 connection was opened on *this* one — sqlite3 refuses a
        #: cross-thread call outright. This queue is the same handoff
        #: :attr:`_control_queues` already uses for the opposite direction; see
        #: :meth:`_drain_feed_health_events`. Unbounded deliberately: these are
        #: state-transition events, not tick-rate ones (migration 0002's own
        #: rule), so there is no load pattern here to bound against.
        self._feed_health_events: queue_module.Queue[FeedHealthEvent] = queue_module.Queue()

    @property
    def hub(self) -> SharedFeedHub:
        return self._hub

    def set_startup_auth_outcome(
        self, *, source: str, token_expiry: str | None, requests_made: int
    ) -> None:
        """Record the token outcome from :meth:`~common.authentication.bootstrap.
        AuthBootstrap.get_token`, called before this repository existed.

        ``source`` is ``TokenOutcome.source`` verbatim ("environment"/"cache"/
        "generated") — stored as ``auth_events.token_source`` and separately
        mapped to the ``auth_events.event`` vocabulary by
        :func:`~common.execution.health_events.auth_event_for_source` when
        :meth:`run` writes it, matching the migration's own column comment.
        """
        self._startup_auth_outcome = (source, token_expiry, requests_made)

    def add_worker(
        self,
        worker_config: WorkerConfig,
        *,
        tick_channel: bool = False,
        live_gate: LiveGateDecision | None = None,
    ) -> WorkerChannel | None:
        """Register a strategy, or refuse it individually. Never abort the group.

        ``tick_channel=True`` additionally gives the worker the opt-in raw-tick
        queue the ported engine reads, and an upstream control queue for the
        runtime subscriptions it makes. Both are handed to the child at spawn
        (Part 2b-ii-B-2); they were created and never delivered until then.

        **A worker configured with an engine needs this flag.** It is not inferred
        from ``WorkerConfig.engine``, because the opt-in also governs what the *hub*
        publishes and that decision belongs to the group, not to one strategy's
        configuration. An engine worker spawned without it refuses to start rather
        than silently running the fixture path — see
        :func:`runtimes.intraday_options.engine_worker.run_engine`.

        Args:
            live_gate: the caller's :func:`~common.config.models.effective_live_gate`
                result for this strategy. Only consulted for a live-mode worker;
                irrelevant, and never evaluated, for paper. Omitting it for a
                live-mode worker is treated as a block, not an oversight that
                happens to allow — the same fail-closed default
                ``effective_live_gate`` itself uses for a missing preflight.

        Returns:
            The registered channel, or ``None`` if this worker was refused. A
            live-designated strategy that fails the gate is refused *by
            itself* — this method never raises for that reason and never stops
            a paper strategy elsewhere in the same group from starting. Spec
            line 2973-2974: the live strategy is blocked, not rerouted to
            paper, and the paper strategy continues safely.
        """
        if worker_config.execution_mode is not ExecutionMode.PAPER:
            self._blocked_workers.append(
                (worker_config, self._live_admission_refusal(worker_config, live_gate))
            )
            _log.error("%s", self._blocked_workers[-1][1])
            return None

        # Checked before this strategy is ever admitted, not after its first
        # order fails: two strategy_ids that agree on their first four
        # alphanumeric characters build the identical correlation_id string
        # on their first order of the day (strategy_token's own docstring —
        # D78, found via a real worker crash: order_intents.correlation_id's
        # UNIQUE constraint rejecting the second one).
        token = strategy_token(worker_config.strategy_id)
        already_admitted = self._admitted_strategy_tokens.get(token)
        if already_admitted is not None and already_admitted != worker_config.strategy_id:
            reason = (
                f"Strategy {worker_config.strategy_id!r} refused: its correlation-ID "
                f"token {token!r} collides with already-admitted strategy "
                f"{already_admitted!r}. Both would build identical correlation IDs on "
                "their first order of the day, which the database refuses as a "
                "duplicate. The rest of the group continues; rename one strategy_id "
                "to resolve."
            )
            self._token_collision_refusals.append((worker_config, reason))
            _log.error("%s", reason)
            return None
        self._admitted_strategy_tokens[token] = worker_config.strategy_id

        channel = build_channel(
            worker_config.strategy_id,
            [worker_config.security_id],
            max_depth=self._config.queue_depth,
            tick_channel=tick_channel,
            tick_max_depth=self._config.tick_queue_depth,
        )
        self._hub.register(channel)
        self._workers.append((worker_config, channel))
        if tick_channel:
            self._control_queues[worker_config.strategy_id] = mp.get_context("spawn").Queue()
        return channel

    @staticmethod
    def _live_admission_refusal(
        worker_config: WorkerConfig, live_gate: LiveGateDecision | None
    ) -> str:
        """Why a live-mode worker was refused. Always something — never silent."""
        if live_gate is None:
            reasons = "no live gate decision was evaluated for this worker"
        elif not live_gate.allowed:
            reasons = "; ".join(live_gate.blocked_reasons)
        else:
            # Unreachable until Phase 10 delivers DhanLiveBroker order placement.
            # Written as a hard stop for the same reason common.broker.factory
            # does (see LiveExecutionBlocked there): if a future change ever lets
            # the gate open, this must still refuse rather than spawn a process
            # guaranteed to fail once it reaches build_broker.
            return (
                f"Strategy {worker_config.strategy_id!r} passed the live gate, but live "
                "order placement is not implemented. DhanLiveBroker order methods "
                "arrive in Phase 10. The rest of the group continues."
            )
        return (
            f"Strategy {worker_config.strategy_id!r} is configured for live execution "
            f"but the live gate blocks it: {reasons}. This strategy will not start. It "
            "is deliberately NOT rerouted to paper — a live strategy running as paper "
            "would misrepresent real exposure. The rest of the group continues."
        )

    def control_queue(self, strategy_id: str) -> Any | None:
        """The upstream subscription-request queue for one worker, if it has one."""
        return self._control_queues.get(strategy_id)

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

        # Late-bound: this SafeNotifier was built in __init__, before this
        # repository existed — mirrors set_health_event_sink just below for
        # exactly the same reason. Safe to call record_notification from this
        # callback: it only ever runs on this thread (this SafeNotifier is
        # not deferred), the same thread that owns the repository.
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
        # repository existed. The sink only enqueues — it must never touch the
        # repository directly, because it runs on the feed thread and the
        # repository's connection belongs to this one. See
        # _drain_feed_health_events for the thread that actually writes.
        self._feed.set_health_event_sink(self._feed_health_events.put)

        if self._startup_auth_outcome is not None:
            source, token_expiry, requests_made = self._startup_auth_outcome
            repository.record_auth_event(
                runtime_id=self._config.runtime_id,
                event=auth_event_for_source(source),
                token_source=source,
                token_expiry=token_expiry,
                requests_made=requests_made,
            )
            self._notifier.send(
                NotificationEvent(
                    event_type="authenticated",
                    message=f"token source={source}",
                    runtime_id=self._config.runtime_id,
                    execution_mode=ExecutionMode.PAPER,
                )
            )

        # Record every live-mode refusal add_worker() collected, before spawning
        # anything admitted. Recorded, not just logged: an operator reading only
        # the database must be able to see that a strategy was deliberately
        # blocked, not silently missing. See test coverage for the mixed-mode
        # gate's requirement that this never shows up as a paper-mode row.
        for worker_config, reason in self._blocked_workers:
            repository.record_error(
                runtime_id=self._config.runtime_id,
                strategy_id=worker_config.strategy_id,
                execution_mode=worker_config.execution_mode,
                severity="ERROR",
                component="supervisor.live_gate",
                message=reason,
            )
            self._notifier.send(
                NotificationEvent(
                    event_type="live_strategy_blocked",
                    message=reason,
                    runtime_id=self._config.runtime_id,
                    strategy_id=worker_config.strategy_id,
                    execution_mode=worker_config.execution_mode,
                )
            )

        # Same pattern, distinct event type: a token collision is not a
        # live-gate decision and must not be reported as one.
        for worker_config, reason in self._token_collision_refusals:
            repository.record_error(
                runtime_id=self._config.runtime_id,
                strategy_id=worker_config.strategy_id,
                execution_mode=worker_config.execution_mode,
                severity="ERROR",
                component="supervisor.correlation_token_collision",
                message=reason,
            )
            self._notifier.send(
                NotificationEvent(
                    event_type="correlation_token_collision",
                    message=reason,
                    runtime_id=self._config.runtime_id,
                    strategy_id=worker_config.strategy_id,
                    execution_mode=worker_config.execution_mode,
                )
            )

        try:
            context = mp.get_context("spawn")
            for worker_config, channel in self._workers:
                process = context.Process(
                    # run_worker_process, not run_worker: multiprocessing
                    # discards a target's return value, so only this
                    # sys.exit()-translating wrapper makes worker_exitcode
                    # (and worker_exit_codes below) reflect what actually
                    # happened — see run_worker_process's own docstring (D77).
                    target=run_worker_process,
                    # Both extra channels travel to the child from here. Until Part
                    # 2b-ii-B-2 they were created and never handed over, which left
                    # the ported engine with no way to receive a tick and no way to
                    # ask for a subscription. A worker that did not opt in gets
                    # ``None`` for both and is completely unaffected.
                    args=(
                        worker_config,
                        channel.queue.raw,
                        # Not this supervisor's own notifier object — spawn
                        # starts a fresh interpreter, so there is no object to
                        # hand across. This sentinel tells the child to build
                        # its own from its own environment. See
                        # NOTIFIER_FROM_SETTINGS's docstring in worker.py.
                        NOTIFIER_FROM_SETTINGS,
                        channel.tick_queue.raw if channel.tick_queue is not None else None,
                        self._control_queues.get(worker_config.strategy_id),
                    ),
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

            # Sentinel per worker, on **every** channel it has: tells a blocked
            # consumer to stop waiting rather than relying on its idle timeout.
            #
            # The tick channel matters as much as the candle one. `HubTickFeed` turns
            # this sentinel into `engine.request_square_off()` — which is how a
            # SIGTERM delivered only to this process reaches each child's engine and
            # closes its positions. That path was built in Part 2b-ii-A and was
            # unreachable as deployed until this line, because only the candle queue
            # was ever sentinelled.
            for _, channel in self._workers:
                channel.queue.publish(None)
                if channel.tick_queue is not None:
                    channel.tick_queue.publish(None)

            for strategy_id, worker_process in self._processes.items():
                worker_process.join(timeout=join_timeout)
                if worker_process.is_alive():
                    _log.warning("worker %s did not exit; terminating", strategy_id)
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
                result,
                heartbeat=heartbeat,
                repository=repository,
                session_id=session.id,
            )
            return result
        finally:
            self._release_queues()
            database.close()
            lock.release()

    def _release_queues(self) -> None:
        """Stop undelivered events from holding this process open at exit.

        A ``multiprocessing.Queue`` flushes through a feeder thread, and that
        thread is **joined at interpreter exit**. If nothing drained the queue and
        the pipe buffer is full, the join never completes and the process hangs
        with no error — measured at ~65 KB of undelivered ticks, which the tick
        channel reaches in a few hundred events.

        That is the shape of failure Phase 3 Part 1 exists to prevent: a shutdown
        path that can itself hang. So at shutdown — after the sentinel, after the
        workers have been joined — undelivered market data is abandoned rather
        than flushed. This is the same judgement the queues already make under
        load: a lagging consumer gets the freshest data or none, never a stalled
        producer. Nothing here is a trading record; those went to SQLite before
        the broker was ever called.
        """
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

        ``worker.py``'s ``EXIT_DUPLICATE`` has been recorded into ``result.
        worker_exit_codes`` since Phase 3 Part 2b-ii-B-2, but nothing ever
        inspected it: the string ``EXIT_DUPLICATE`` appeared nowhere in this
        module. Without this, a refused worker was a silent zero-length run
        — the group's own duplicate-worker guard (``worker_lock``) already
        protects the database; this is what makes the *group* notice, so an
        operator sees "this strategy never started" instead of just seeing
        fewer orders than expected with no explanation.

        Every worker this method can ever be called for was admitted as a
        paper-mode worker (:meth:`add_worker` refuses anything else before
        it ever reaches ``self._processes``) — so ``execution_mode`` is
        always PAPER here, not a lookup.
        """
        message = (
            f"{strategy_id} did not start: another process already holds its worker lock "
            "(exit code EXIT_DUPLICATE). No orders were placed for this strategy this run."
        )
        _log.error("%s", message)
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
            # The group-level counterpart to "runtime_started" above. The
            # unclean case already has its own notification
            # (feed_shutdown_unclean, _raise_silent_feed_alarm) — this is not
            # a duplicate of that, it is the event for the ordinary case that
            # alarm exists to be the exception to.
            self._notifier.send(
                NotificationEvent(
                    event_type="runtime_stopped",
                    message=f"{self._config.runtime_id} supervisor stopped ({reason})",
                    runtime_id=self._config.runtime_id,
                    execution_mode=ExecutionMode.PAPER,
                )
            )
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
            # spends the wait keeping the group's heartbeat fresh and forwarding
            # workers' runtime subscription requests. Without the beat the
            # dashboard would show one at startup and then a heartbeat age that
            # climbs all session — indistinguishable from a dead supervisor.
            while not wake.wait(timeout=HEARTBEAT_POLL_SECONDS):
                self._drain_control_queues()
                self._drain_feed_health_events(repository)
                self._check_stuck_subscription(heartbeat, repository)
                self._check_stale_instruments(repository)
                self._beat_running(heartbeat)
            # Once more after the wake: a request — or a health event — that
            # arrived in the same instant the feed finished is still worth
            # applying, and costs nothing if the queues are empty.
            self._drain_control_queues()
            self._drain_feed_health_events(repository)
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

    def _drain_control_queues(self) -> None:
        """Forward workers' runtime subscription requests to the hub.

        Non-blocking, and it never calls the adapter itself: ``request_subscription``
        only enqueues, and the hub applies it on the feed thread — the one thread
        permitted to touch the connection. This method therefore stays safe to call
        from the main thread while the feed thread is inside ``recv()``.

        A malformed entry is logged and dropped rather than allowed to kill the
        supervisor's loop: the child that sent it is the one with the problem, and
        taking the whole group down with it would be a worse outcome.
        """
        for strategy_id, control_queue in self._control_queues.items():
            while True:
                try:
                    request = control_queue.get_nowait()
                except queue_module.Empty:
                    break
                parsed = _parse_subscription_request(request)
                if parsed is None:
                    _log.warning(
                        "ignoring a malformed subscription request from %s: %r",
                        strategy_id,
                        request,
                    )
                    continue
                security_id, segment, mode = parsed
                self._hub.request_subscription(strategy_id, security_id, segment=segment, mode=mode)

    def _drain_feed_health_events(self, repository: ExecutionRepository) -> None:
        """Write every queued ``feed_events`` row on this (the repository's) thread.

        The feed thread only ever enqueues (:meth:`run`'s ``set_health_event_sink``
        call) — it must not call the repository directly, since its sqlite3
        connection belongs to this thread. A write that raises is logged and
        dropped rather than allowed to break the poll loop: a `feed_events` row is
        diagnostic, not something the run depends on, and the same isolation
        discipline ``SafeNotifier`` applies to Telegram belongs here too.
        """
        while True:
            try:
                event = self._feed_health_events.get_nowait()
            except queue_module.Empty:
                break
            try:
                repository.record_feed_event(runtime_id=self._config.runtime_id, **asdict(event))
            except Exception:
                _log.exception("failed to persist a feed_events row (event=%s)", event.event)
            if event.event in _CHAT_WORTHY_FEED_EVENTS:
                self._notifier.send(
                    NotificationEvent(
                        event_type=f"feed_{event.event}",
                        message=event.reason or event.event,
                        runtime_id=self._config.runtime_id,
                        execution_mode=ExecutionMode.PAPER,
                    )
                )

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

    def _check_stuck_subscription(
        self,
        heartbeat: HeartbeatWriter,
        repository: ExecutionRepository,
    ) -> None:
        """Alarm when a runtime subscription has not been applied (limitation 15).

        The hub applies a pending subscription at the top of ``on_tick``, on the
        thread that owns the connection (**D24**), so a feed delivering nothing
        never applies one. The consequence is specific and silent: the engine
        deliberately waits for a *fresh* tick on the contract it chose rather
        than using a cached price, so its pending entry simply never fills. That
        is the safe direction — no entry beats an entry at a stale price — but
        until now nothing said so.

        It matters more from Phase 4 Part 1 on. With synthetic contracts an
        unapplied subscription was one symptom among many; with **real** ones it
        is the single thing standing between a resolved contract and a fill, so
        it is exactly the failure a live rehearsal is most likely to meet.

        Raised **once per run**, not once per poll: this is a condition that
        persists, and a notification every second would be noise rather than an
        alarm. The heartbeat is left `DEGRADED` for as long as it holds.
        """
        age = self._hub.pending_subscription_age_seconds()
        if age < STUCK_SUBSCRIPTION_SECONDS:
            return
        if self._stuck_subscription_alarmed:
            return
        self._stuck_subscription_alarmed = True

        message = (
            f"a runtime subscription has been waiting {age:.1f}s to be applied "
            f"(threshold {STUCK_SUBSCRIPTION_SECONDS:.0f}s). The hub applies one only "
            "when a tick arrives, so the feed is delivering nothing for this group. "
            "Any engine waiting on a contract it just chose will not enter — it wants "
            "a fresh tick and will not use a cached price. Check the feed connection."
        )
        _log.error("%s", message)
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

    def _check_stale_instruments(self, repository: ExecutionRepository) -> None:
        """Write a ``feed_events`` row for each newly-stale instrument.

        Deliberately lighter-weight than :meth:`_check_stuck_subscription`: a
        single quiet far-OTM strike is explicitly *not* a broken feed (see
        :data:`~common.feed.reconnect.DEFAULT_STALENESS_SECONDS`'s own
        comment), so this writes a diagnostic row for the dashboard/CLI to
        show, not an ``errors`` row or a notification. Latched per instrument:
        an instrument stays "already reported" until a fresh tick clears it
        from :meth:`~common.feed.reconnect.FeedHealth.stale_instruments`, so a
        quiet strike produces one row, not one every poll.
        """
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
