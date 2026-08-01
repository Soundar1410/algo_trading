"""The limitation-15 alarm: a runtime subscription that is never applied.

The hub applies a pending subscription at the top of ``on_tick``, on the thread
that owns the connection (**D24**), so a feed delivering nothing never applies
one. Phase 3 recorded the consequence and left it silent: the engine waits for a
*fresh* tick on the contract it chose rather than using a cached price, so its
pending entry simply never fills.

Phase 4 Part 1 closes the silence. It matters more from here on — with synthetic
contracts an unapplied subscription was one symptom among many; with real ones it
is the single thing between a resolved contract and a fill.
"""

from __future__ import annotations

import time

import pytest

from common.feed.hub import SharedFeedHub, WorkerChannel
from common.feed.queues import BoundedWorkerQueue
from common.market_data.recorded import RecordedFeedAdapter


def _hub() -> SharedFeedHub:
    hub = SharedFeedHub(RecordedFeedAdapter([]))
    hub.register(
        WorkerChannel(
            strategy_id="s1",
            security_ids=frozenset({"13"}),
            queue=BoundedWorkerQueue.in_process("s1"),
        )
    )
    return hub


# ------------------------------------------------------------------ the clock
def test_nothing_pending_means_no_age():
    assert _hub().pending_subscription_age_seconds() == 0.0


def test_a_pending_subscription_starts_ageing_immediately():
    hub = _hub()
    hub.request_subscription("s1", "8103")
    time.sleep(0.02)
    assert hub.pending_subscription_age_seconds() > 0.0


def test_applying_the_subscription_stops_the_clock():
    hub = _hub()
    hub.request_subscription("s1", "8103")
    hub._apply_pending_subscriptions()
    assert hub.pending_subscription_age_seconds() == 0.0


def test_the_age_is_the_oldest_request_not_the_newest():
    """A steady trickle of new requests must not keep resetting the clock and
    hide the first one that has been stuck for a minute."""
    hub = _hub()
    hub.request_subscription("s1", "8103")
    time.sleep(0.05)
    hub.request_subscription("s1", "8104")
    assert hub.pending_subscription_age_seconds() >= 0.05


def test_a_later_request_times_from_itself_after_a_drain():
    hub = _hub()
    hub.request_subscription("s1", "8103")
    time.sleep(0.05)
    hub._apply_pending_subscriptions()
    hub.request_subscription("s1", "8104")
    age = hub.pending_subscription_age_seconds()
    assert age < 0.05, "the cleared request's age leaked into the new one"


def test_a_request_for_an_unregistered_worker_still_clears_the_clock():
    """It is dropped rather than applied, but it is no longer pending — leaving
    the clock running would alarm about a request nobody is waiting on."""
    hub = _hub()
    hub.request_subscription("nobody", "8103")
    hub._apply_pending_subscriptions()
    assert hub.pending_subscription_age_seconds() == 0.0


# ----------------------------------------------------------------- the alarm
@pytest.fixture
def supervisor_bits(tmp_path, database_path):
    from common.config.models import ExecutionMode
    from common.execution import ExecutionRepository
    from common.health import HeartbeatWriter
    from common.notifications import RecordingNotifier
    from common.persistence import Database, MigrationRunner
    from runtimes.intraday_options.supervisor import (
        IntradayOptionsSupervisor,
        SupervisorConfig,
    )

    database = Database(database_path)
    MigrationRunner(database).run_pending()
    repository = ExecutionRepository(database)
    notifier = RecordingNotifier()

    supervisor = IntradayOptionsSupervisor(
        SupervisorConfig(
            runtime_id="intraday_options",
            database_path=database_path,
            lock_dir=tmp_path,
            pid_dir=tmp_path,
            log_dir=tmp_path,
        ),
        adapter=RecordedFeedAdapter([]),
        notifier=notifier,
    )
    session = repository.open_session(
        runtime_id="intraday_options",
        strategy_id=None,
        execution_mode=ExecutionMode.PAPER,
        process_role="supervisor",
        pid=1234,
    )
    heartbeat = HeartbeatWriter(
        repository,
        session_id=session.id,
        runtime_id="intraday_options",
        strategy_id=None,
    )
    return supervisor, heartbeat, repository, notifier, database


def _errors(database):
    return database.connect().execute("SELECT severity, component, message FROM errors").fetchall()


def test_a_fresh_request_raises_nothing(supervisor_bits):
    supervisor, heartbeat, repository, notifier, database = supervisor_bits
    supervisor.hub.register(
        WorkerChannel(
            strategy_id="s1",
            security_ids=frozenset({"13"}),
            queue=BoundedWorkerQueue.in_process("s1"),
        )
    )
    supervisor.hub.request_subscription("s1", "8103")

    supervisor._check_stuck_subscription(heartbeat, repository)

    assert _errors(database) == []
    assert notifier.events == []


def test_a_stuck_request_fires_all_three_channels(supervisor_bits, monkeypatch):
    """A log line is not an alarm: the dashboard tile, its message, and a human
    who is looking at neither."""
    from runtimes.intraday_options import supervisor as module

    supervisor, heartbeat, repository, notifier, database = supervisor_bits
    monkeypatch.setattr(module, "STUCK_SUBSCRIPTION_SECONDS", 0.0)
    supervisor.hub.register(
        WorkerChannel(
            strategy_id="s1",
            security_ids=frozenset({"13"}),
            queue=BoundedWorkerQueue.in_process("s1"),
        )
    )
    supervisor.hub.request_subscription("s1", "8103")
    time.sleep(0.01)

    supervisor._check_stuck_subscription(heartbeat, repository)

    rows = _errors(database)
    assert len(rows) == 1
    severity, component, message = rows[0]
    assert severity == "CRITICAL"
    assert component == "feed"
    assert "will not enter" in message

    assert [event.event_type for event in notifier.events] == ["subscription_not_applied"]

    state = (
        repository.database.connect()
        .execute(
            "SELECT health_state FROM runtime_heartbeats WHERE strategy_id IS NULL "
            "ORDER BY id DESC LIMIT 1"
        )
        .fetchone()
    )
    assert state[0] == "DEGRADED"


def test_the_alarm_fires_once_however_long_it_persists(supervisor_bits, monkeypatch):
    """The condition persists by nature. One notification per poll would be
    noise, and noise is how a real alarm gets ignored."""
    from runtimes.intraday_options import supervisor as module

    supervisor, heartbeat, repository, notifier, database = supervisor_bits
    monkeypatch.setattr(module, "STUCK_SUBSCRIPTION_SECONDS", 0.0)
    supervisor.hub.register(
        WorkerChannel(
            strategy_id="s1",
            security_ids=frozenset({"13"}),
            queue=BoundedWorkerQueue.in_process("s1"),
        )
    )
    supervisor.hub.request_subscription("s1", "8103")
    time.sleep(0.01)

    for _ in range(5):
        supervisor._check_stuck_subscription(heartbeat, repository)

    assert len(_errors(database)) == 1
    assert len(notifier.events) == 1


def test_the_threshold_is_not_reached_by_an_ordinary_session(supervisor_bits):
    """The default must be generous against the mechanism it watches: the hub
    applies pending subscriptions on *any* tick."""
    from runtimes.intraday_options.supervisor import STUCK_SUBSCRIPTION_SECONDS

    assert STUCK_SUBSCRIPTION_SECONDS >= 30.0
