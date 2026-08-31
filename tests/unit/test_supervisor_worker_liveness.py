"""Unit coverage for the crash-awareness gap the 31 August 2026 incident
exposed: ``supertrend_buy_1_1p2``'s worker died mid-session, and nothing
noticed for six hours — the supervisor kept running with a dead child, its
tick queue draining nowhere (~90,000 dropped-tick warnings), and it never
exited, which would have silently cancelled the next trading day's
auto-start had it still been alive at 09:00.

Exercises ``IntradayOptionsSupervisor._check_worker_liveness`` and its
helpers directly, the same "call the private method, no full ``run()``"
convention ``test_stuck_subscription_alarm.py`` already uses for the other
poll-loop alarms. A fake process double stands in for a real spawned
child — deterministic and fast, in contrast to the real-``SIGKILL``
end-to-end proof in ``tests/end_to_end/test_supervisor.py``. Deliberately
never raises or checks for any specific exception type: the whole point of
this design is that it reacts only to ``process.is_alive()`` going
``False``, regardless of why.
"""

from __future__ import annotations

import queue as queue_module
import time as time_module
from pathlib import Path

import pytest

from common.config.models import ExecutionMode
from common.execution import ExecutionRepository
from common.health import HeartbeatWriter
from common.market_data.recorded import RecordedFeedAdapter
from common.notifications import RecordingNotifier
from common.persistence import Database, MigrationRunner
from runtimes.intraday_options.supervisor import (
    WORKER_RESTART_MAX_ATTEMPTS,
    IntradayOptionsSupervisor,
    SupervisorConfig,
    _WorkerState,
)
from runtimes.intraday_options.worker import WorkerConfig

RUNTIME_ID = "intraday_options"
TRADING_DATE = "2026-07-29"


class _FakeProcess:
    """A worker process double we fully control: dies and reports its exit
    code on cue, matching the small ``is_alive``/``exitcode``/``join``/``pid``
    surface ``_check_worker_liveness``/``_restart_worker`` actually use.
    Mirrors ``tests/unit/test_auto_start_runtime_launcher.py``'s
    ``_FakeProcess`` for that module's own, differently-shaped
    ``ProcessHandle`` needs.
    """

    _next_pid = 9000

    def __init__(self) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self._alive = True
        self.exitcode: int | None = None

    def is_alive(self) -> bool:
        return self._alive

    def join(self, timeout: float | None = None) -> None:
        return None

    def die(self, exitcode: int = 1) -> None:
        self._alive = False
        self.exitcode = exitcode


@pytest.fixture
def liveness_bits(tmp_path: Path, database_path: Path):
    """A supervisor with a real repository/heartbeat/notifier, and
    ``_spawn_worker`` replaced with a fake so no real OS process is ever
    started — every death and restart in this file is simulated, not real.
    """
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    repository = ExecutionRepository(database)
    notifier = RecordingNotifier()

    supervisor = IntradayOptionsSupervisor(
        SupervisorConfig(
            runtime_id=RUNTIME_ID,
            database_path=database_path,
            lock_dir=tmp_path,
            pid_dir=tmp_path,
            log_dir=tmp_path,
        ),
        adapter=RecordedFeedAdapter([]),
        notifier=notifier,
    )
    session = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id=None,
        execution_mode=ExecutionMode.PAPER,
        process_role="supervisor",
        pid=1234,
    )
    heartbeat = HeartbeatWriter(
        repository, session_id=session.id, runtime_id=RUNTIME_ID, strategy_id=None
    )

    fake_processes: dict[str, _FakeProcess] = {}

    def _fake_spawn(worker_config: WorkerConfig, channel):  # type: ignore[no-untyped-def]
        process = _FakeProcess()
        fake_processes[worker_config.strategy_id] = process
        supervisor._processes[worker_config.strategy_id] = process
        return process

    supervisor._spawn_worker = _fake_spawn  # type: ignore[method-assign]

    return supervisor, heartbeat, repository, notifier, database, fake_processes


def _admit(supervisor: IntradayOptionsSupervisor, strategy_id: str, *, tick_channel: bool = True):
    """Register and (fake-)spawn one worker — the same two steps ``run()``'s
    own spawn loop performs, without the rest of ``run()``."""
    worker_config = WorkerConfig(
        runtime_id=RUNTIME_ID,
        strategy_id=strategy_id,
        security_id="99926000",
        instrument="NIFTY",
        database_path=supervisor._config.database_path,
        lock_dir=supervisor._config.lock_dir,
        pid_dir=supervisor._config.pid_dir,
        log_dir=supervisor._config.log_dir,
        trading_date=TRADING_DATE,
    )
    channel = supervisor.add_worker(worker_config, tick_channel=tick_channel)
    assert channel is not None
    supervisor._spawn_worker(worker_config, channel)
    supervisor._worker_state[strategy_id] = _WorkerState(
        worker_config=worker_config, channel=channel
    )
    return worker_config, channel


def _bypass_backoff(supervisor: IntradayOptionsSupervisor, strategy_id: str) -> None:
    """Zero out the restart backoff, standing in for "enough real time has
    passed" without an actual ``time.sleep`` — keeps these tests fast and
    deterministic. Used only between iterations of a test that deliberately
    drives a strategy through several deaths in a row; a single death's own
    immediate-first-restart behaviour needs no help (``restart_not_before``
    starts at ``0.0``, already in the past)."""
    supervisor._worker_state[strategy_id].restart_not_before = 0.0


def _errors(database: Database):
    return database.connect().execute(
        "SELECT severity, component, message, strategy_id FROM errors"
    ).fetchall()


# --------------------------------------------------------------- detection
def test_a_live_worker_raises_nothing(liveness_bits):
    supervisor, heartbeat, repository, notifier, database, _procs = liveness_bits
    _admit(supervisor, "s1")

    supervisor._check_worker_liveness(heartbeat, repository)

    assert _errors(database) == []
    assert notifier.events == []
    assert supervisor._worker_state["s1"].restarts == 0


def test_a_dead_worker_is_alarmed_on_all_three_channels(liveness_bits):
    """A log line is not an alarm: the dashboard tile, its message, and a
    human who is looking at neither — same rule ``_check_stuck_subscription``
    already follows, applied to a dead worker instead of a stuck feed."""
    supervisor, heartbeat, repository, notifier, database, procs = liveness_bits
    _admit(supervisor, "s1")
    procs["s1"].die(exitcode=1)

    supervisor._check_worker_liveness(heartbeat, repository)

    rows = [r for r in _errors(database) if r["component"] == "supervisor.worker_died"]
    assert len(rows) == 1
    assert rows[0]["severity"] == "CRITICAL"
    assert rows[0]["strategy_id"] == "s1"
    assert "died" in rows[0]["message"]

    died_events = [e for e in notifier.events if e.event_type == "worker_died"]
    assert len(died_events) == 1
    assert died_events[0].strategy_id == "s1"
    assert died_events[0].required_action is not None

    state = (
        database.connect()
        .execute(
            "SELECT health_state FROM runtime_heartbeats WHERE strategy_id IS NULL "
            "ORDER BY id DESC LIMIT 1"
        )
        .fetchone()
    )
    assert state[0] == "DEGRADED"


def test_the_queue_is_suspended_the_instant_death_is_observed(liveness_bits):
    """Containment comes before any restart decision — this is the actual
    fix for the incident's ~90,000-warning overflow storm: a dead worker's
    queue must stop being fed immediately, not after the supervisor gets
    around to deciding what to do about the death."""
    supervisor, heartbeat, repository, _notifier, _database, procs = liveness_bits
    _, channel = _admit(supervisor, "s1")
    assert not supervisor.hub.is_suspended("s1")
    procs["s1"].die()

    # Push the restart backoff far into the future first, so the poll below
    # observes the death and alarms but does not yet restart — isolating
    # "suspended on death" from "resumed on restart".
    supervisor._worker_state["s1"].restart_not_before = time_module.monotonic() + 999.0

    supervisor._check_worker_liveness(heartbeat, repository)

    assert supervisor.hub.is_suspended("s1")
    assert channel.strategy_id == "s1"  # sanity: same channel the hub gates


def test_death_alarm_fires_once_however_long_the_backoff_holds(liveness_bits):
    """The condition (dead, waiting out backoff) persists across several
    polls by construction. One notification, not one per poll — the same
    property ``test_the_alarm_fires_once_however_long_it_persists`` proves
    for the stuck-subscription alarm."""
    supervisor, heartbeat, repository, notifier, database, procs = liveness_bits
    _admit(supervisor, "s1")
    procs["s1"].die()
    supervisor._worker_state["s1"].restart_not_before = time_module.monotonic() + 999.0

    for _ in range(5):
        supervisor._check_worker_liveness(heartbeat, repository)

    assert len([e for e in notifier.events if e.event_type == "worker_died"]) == 1
    assert len([r for r in _errors(database) if r["component"] == "supervisor.worker_died"]) == 1


# ------------------------------------------ clean completion, not a crash
def test_a_worker_that_exits_zero_is_not_alarmed_or_restarted(liveness_bits):
    """The critical distinction this design depends on: different strategies
    can carry different ``square_off_at`` times, so one finishing cleanly
    while its siblings keep trading is an everyday event, not an incident.
    Restarting it would re-run its trading day from a blank state right as
    the market closes — found by the real-process end-to-end test, where a
    fixture worker finishing its own scripted sequence early hit exactly
    this path before the distinction existed."""
    supervisor, heartbeat, repository, notifier, database, procs = liveness_bits
    _admit(supervisor, "s1")
    procs["s1"].die(exitcode=0)

    supervisor._check_worker_liveness(heartbeat, repository)

    state = supervisor._worker_state["s1"]
    assert state.finished is True
    assert state.contained is False
    assert state.restarts == 0
    assert notifier.events == []
    assert _errors(database) == []
    assert supervisor.hub.is_suspended("s1"), "nothing is left to read its queue either way"


def test_a_finished_worker_is_never_re_examined(liveness_bits):
    supervisor, heartbeat, repository, notifier, _database, procs = liveness_bits
    _admit(supervisor, "s1")
    procs["s1"].die(exitcode=0)
    supervisor._check_worker_liveness(heartbeat, repository)

    for _ in range(5):
        supervisor._check_worker_liveness(heartbeat, repository)

    assert supervisor._worker_state["s1"].restarts == 0
    assert notifier.events == []


def test_a_finished_worker_does_not_degrade_the_group_heartbeat(liveness_bits):
    """Only a genuinely contained (crashed-and-gave-up) worker should mark
    the group DEGRADED — a clean early finish is not a health problem."""
    supervisor, heartbeat, repository, _notifier, database, procs = liveness_bits
    _admit(supervisor, "s1")
    procs["s1"].die(exitcode=0)
    supervisor._check_worker_liveness(heartbeat, repository)

    supervisor._beat_running(heartbeat)

    state = (
        database.connect()
        .execute(
            "SELECT health_state FROM runtime_heartbeats WHERE strategy_id IS NULL "
            "ORDER BY id DESC LIMIT 1"
        )
        .fetchone()
    )
    assert state[0] != "DEGRADED"


def test_no_live_workers_remain_is_not_triggered_by_a_clean_finish(liveness_bits):
    """Deliberately narrower than "nothing left to feed": a group that
    finished early is ordinary (different strategies carry different
    square-off times) and is left to the session deadline rather than
    ending the feed the instant the last worker finishes — that eager stop
    was tried and reverted after it raced a control-queue request applied
    only by a live feed tick (see ``_no_live_workers_remain``'s own
    docstring)."""
    supervisor, heartbeat, repository, _notifier, _database, procs = liveness_bits
    _admit(supervisor, "s1")
    procs["s1"].die(exitcode=0)
    supervisor._check_worker_liveness(heartbeat, repository)

    assert supervisor._no_live_workers_remain() is False


# ----------------------------------------------------------------- restart
def test_a_dead_worker_is_respawned_reaped_drained_and_resumed(liveness_bits):
    supervisor, heartbeat, repository, _notifier, _database, procs = liveness_bits
    _, channel = _admit(supervisor, "s1")
    old_process = procs["s1"]

    # Something was queued for the dead worker before it died — it must not
    # survive into the respawned process.
    channel.queue.publish(object())
    if channel.tick_queue is not None:
        channel.tick_queue.publish(object())

    old_process.die()
    supervisor._check_worker_liveness(heartbeat, repository)

    state = supervisor._worker_state["s1"]
    assert state.restarts == 1
    assert state.contained is False
    assert not supervisor.hub.is_suspended("s1"), "resumed after a successful restart"

    new_process = supervisor._processes["s1"]
    assert new_process is not old_process, "a fresh process must replace the dead one"
    assert new_process.is_alive()

    # depth() reports -1 ("unknown") on macOS (mp.Queue.qsize() raises
    # NotImplementedError there), so a real emptiness proof reads instead:
    # a drained queue must raise queue.Empty rather than hand back the
    # stale item.
    with pytest.raises(queue_module.Empty):
        channel.queue.get(timeout=0.2)
    if channel.tick_queue is not None:
        with pytest.raises(queue_module.Empty):
            channel.tick_queue.get(timeout=0.2)


def test_a_second_death_gets_its_own_alarm(liveness_bits):
    """``death_alarmed`` resets on a successful restart, so a worker that
    dies again is treated as a fresh incident, not silently folded into the
    first one's already-fired alarm."""
    supervisor, heartbeat, repository, notifier, _database, procs = liveness_bits
    _admit(supervisor, "s1")

    procs["s1"].die()
    supervisor._check_worker_liveness(heartbeat, repository)
    _bypass_backoff(supervisor, "s1")
    procs["s1"].die()  # the respawned process, now also dead
    supervisor._check_worker_liveness(heartbeat, repository)

    assert len([e for e in notifier.events if e.event_type == "worker_died"]) == 2
    assert supervisor._worker_state["s1"].restarts == 2


# ------------------------------------------------------------- containment
def test_restart_budget_exhaustion_contains_the_worker(liveness_bits):
    supervisor, heartbeat, repository, notifier, database, procs = liveness_bits
    _admit(supervisor, "s1")

    for _ in range(WORKER_RESTART_MAX_ATTEMPTS + 1):
        _bypass_backoff(supervisor, "s1")
        procs["s1"].die()
        supervisor._check_worker_liveness(heartbeat, repository)

    state = supervisor._worker_state["s1"]
    assert state.contained is True
    assert state.restarts == WORKER_RESTART_MAX_ATTEMPTS
    assert supervisor.hub.is_suspended("s1")

    exhausted = [e for e in notifier.events if e.event_type == "worker_restart_exhausted"]
    assert len(exhausted) == 1
    assert exhausted[0].strategy_id == "s1"
    rows = [r for r in _errors(database) if r["component"] == "supervisor.worker_restart_exhausted"]
    assert len(rows) == 1
    assert rows[0]["severity"] == "CRITICAL"


def test_a_contained_worker_is_never_re_examined(liveness_bits):
    """Once contained, further polls must be pure no-ops for this strategy —
    no repeated alarms, no repeated restart attempts."""
    supervisor, heartbeat, repository, notifier, database, procs = liveness_bits
    _admit(supervisor, "s1")
    for _ in range(WORKER_RESTART_MAX_ATTEMPTS + 1):
        _bypass_backoff(supervisor, "s1")
        procs["s1"].die()
        supervisor._check_worker_liveness(heartbeat, repository)

    events_before = len(notifier.events)
    errors_before = len(_errors(database))
    restarts_before = supervisor._worker_state["s1"].restarts

    for _ in range(5):
        supervisor._check_worker_liveness(heartbeat, repository)

    assert len(notifier.events) == events_before
    assert len(_errors(database)) == errors_before
    assert supervisor._worker_state["s1"].restarts == restarts_before


def test_the_group_heartbeat_stays_degraded_while_any_worker_is_contained(liveness_bits):
    supervisor, heartbeat, repository, _notifier, database, procs = liveness_bits
    _admit(supervisor, "s1")
    for _ in range(WORKER_RESTART_MAX_ATTEMPTS + 1):
        _bypass_backoff(supervisor, "s1")
        procs["s1"].die()
        supervisor._check_worker_liveness(heartbeat, repository)

    supervisor._beat_running(heartbeat)

    state = (
        database.connect()
        .execute(
            "SELECT health_state FROM runtime_heartbeats WHERE strategy_id IS NULL "
            "ORDER BY id DESC LIMIT 1"
        )
        .fetchone()
    )
    assert state[0] == "DEGRADED"


# ------------------------------------------------------------- generality
@pytest.mark.parametrize("strategy_id", ["ema_cross_test", "straddle_test", "supertrend_test"])
def test_death_detection_is_identical_for_any_strategy_id(liveness_bits, strategy_id):
    """Nothing here is wired to any one strategy — or, by construction (no
    ``except sqlite3.OperationalError`` or any other except clause anywhere
    in this path), to any one cause of death."""
    supervisor, heartbeat, repository, notifier, _database, procs = liveness_bits
    _admit(supervisor, strategy_id)
    procs[strategy_id].die(exitcode=-9)  # SIGKILL-shaped exit code

    supervisor._check_worker_liveness(heartbeat, repository)

    # A first death restarts immediately (no backoff yet), which resumes the
    # channel again within this same call — see the dedicated suspend-on-
    # death test above for that instant in isolation. What persists is the
    # detection and the alarm.
    assert supervisor._worker_state[strategy_id].restarts == 1
    assert any(e.strategy_id == strategy_id for e in notifier.events)


def test_one_worker_dying_does_not_touch_a_healthy_sibling(liveness_bits):
    supervisor, heartbeat, repository, notifier, _database, procs = liveness_bits
    _admit(supervisor, "sick")
    _admit(supervisor, "healthy")
    procs["sick"].die()

    supervisor._check_worker_liveness(heartbeat, repository)

    # "sick" restarted immediately and was resumed within this same call —
    # see the dedicated suspend-on-death test for that instant in isolation.
    assert supervisor._worker_state["sick"].restarts == 1
    assert not supervisor.hub.is_suspended("healthy")
    assert supervisor._worker_state["healthy"].restarts == 0
    assert supervisor._worker_state["healthy"].contained is False
    assert not any(e.strategy_id == "healthy" for e in notifier.events)


# ------------------------------------------------------- no-live-workers
def test_no_live_workers_remain_is_false_with_nothing_admitted(liveness_bits):
    supervisor, *_rest = liveness_bits
    assert supervisor._no_live_workers_remain() is False


def test_no_live_workers_remain_is_false_while_anything_is_still_trying(liveness_bits):
    supervisor, heartbeat, repository, _notifier, _database, procs = liveness_bits
    _admit(supervisor, "s1")
    procs["s1"].die()
    supervisor._check_worker_liveness(heartbeat, repository)  # restarted, not contained

    assert supervisor._no_live_workers_remain() is False


def test_no_live_workers_remain_is_true_only_once_every_worker_is_contained(liveness_bits):
    supervisor, heartbeat, repository, _notifier, _database, procs = liveness_bits
    _admit(supervisor, "s1")
    _admit(supervisor, "s2")

    for _ in range(WORKER_RESTART_MAX_ATTEMPTS + 1):
        _bypass_backoff(supervisor, "s1")
        procs["s1"].die()
        supervisor._check_worker_liveness(heartbeat, repository)
    assert supervisor._no_live_workers_remain() is False, "s2 is still healthy"

    for _ in range(WORKER_RESTART_MAX_ATTEMPTS + 1):
        _bypass_backoff(supervisor, "s2")
        procs["s2"].die()
        supervisor._check_worker_liveness(heartbeat, repository)
    assert supervisor._no_live_workers_remain() is True


# ------------------------------------------------------- DB config untouched
def test_the_database_pragmas_this_task_promised_not_to_touch_are_unchanged():
    """This task is detection and containment, not the DB-lock frequency
    that triggered the incident's specific worker crash — that is a
    separate, later task. Pinned directly rather than trusted by inspection,
    since ``common/persistence/database.py`` was never even opened by any
    other test in this file."""
    from common.persistence.database import DEFAULT_BUSY_TIMEOUT_MS

    assert DEFAULT_BUSY_TIMEOUT_MS == 5_000
