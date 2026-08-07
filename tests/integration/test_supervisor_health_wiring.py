"""Phase 7 Part 1: the supervisor's wiring into the health snapshot layer.

Four things this file proves that nothing else does:

* The feed's ``feed_events`` sink is actually connected — a
  :class:`~common.feed.reconnect.ReconnectingFeed` on its own (see
  ``tests/integration/test_feed_reconnect.py``) proves the *emission*; this
  proves the *supervisor wires it to the repository*.
* The stuck-subscription and silent-feed alarms, which already wrote to
  ``errors``/heartbeat/notification, now also write a ``degraded`` row to
  ``feed_events`` — the health snapshot's one channel for "what is the feed
  doing", so an operator does not have to know to also check ``errors``.
* A newly-stale instrument produces exactly one ``feed_events`` row, not one
  per poll — the same latching discipline the stuck-subscription alarm uses.
* ``set_startup_auth_outcome`` reaches ``auth_events`` once ``run()`` opens
  the repository, mapped through the correct vocabulary.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from common.config.models import ExecutionMode
from common.execution import ExecutionRepository
from common.feed.hub import WorkerChannel
from common.feed.queues import BoundedWorkerQueue
from common.health import HeartbeatWriter
from common.market_data.recorded import RecordedFeedAdapter
from common.notifications import RecordingNotifier
from common.persistence import Database, MigrationRunner
from runtimes.intraday_options.supervisor import IntradayOptionsSupervisor, SupervisorConfig

RUNTIME_ID = "intraday_options"


def _register_a_worker(supervisor: IntradayOptionsSupervisor) -> None:
    """Every run() below drives an empty recorded tape end-to-end and needs at
    least one registered channel — the hub refuses to start with none."""
    supervisor.hub.register(
        WorkerChannel(
            strategy_id="s1",
            security_ids=frozenset({"13"}),
            queue=BoundedWorkerQueue.in_process("s1"),
        )
    )


@pytest.fixture
def supervisor_bits(tmp_path, database_path):
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
    return supervisor, heartbeat, repository, notifier, database


def _feed_events(database):
    return database.connect().execute(
        "SELECT event, reason, security_id FROM feed_events ORDER BY id"
    ).fetchall()


# --------------------------------------------------------- the feed sink
def test_the_feeds_health_events_reach_the_repository_once_run_opens_it(tmp_path, database_path):
    """The feed is built in __init__, before any repository exists. This
    proves the late-binding in run() actually connects the two, not just that
    the sink mechanism itself works (test_feed_reconnect.py proves that)."""
    supervisor = IntradayOptionsSupervisor(
        SupervisorConfig(
            runtime_id=RUNTIME_ID,
            database_path=database_path,
            lock_dir=tmp_path,
            pid_dir=tmp_path,
            log_dir=tmp_path,
        ),
        adapter=RecordedFeedAdapter([]),
    )
    _register_a_worker(supervisor)
    # Before run(): no repository exists yet, so the sink must still be unset.
    feed_sink = supervisor._feed._on_health_event
    assert feed_sink is None

    result = supervisor.run()

    assert result.workers_started == 0
    database = Database(database_path)
    rows = _feed_events(database)
    # An empty recorded tape still connects and then finishes — at minimum a
    # "connected" row was written through the now-wired sink.
    assert any(row["event"] == "connected" for row in rows)


# -------------------------------------------------------- degraded alarms
def test_the_stuck_subscription_alarm_also_writes_a_degraded_feed_event(
    supervisor_bits, monkeypatch
):
    from runtimes.intraday_options import supervisor as module

    supervisor, heartbeat, repository, _notifier, database = supervisor_bits
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

    rows = _feed_events(database)
    assert len(rows) == 1
    assert rows[0]["event"] == "degraded"
    assert "will not enter" in rows[0]["reason"]


def test_the_silent_feed_alarm_also_writes_a_degraded_feed_event(supervisor_bits):
    supervisor, heartbeat, repository, _notifier, database = supervisor_bits

    supervisor._raise_silent_feed_alarm(heartbeat, repository, shutdown_grace=10.0)

    rows = _feed_events(database)
    assert len(rows) == 1
    assert rows[0]["event"] == "degraded"


# ---------------------------------------------------------- stale instruments
def test_a_newly_stale_instrument_produces_exactly_one_row(supervisor_bits):
    supervisor, _heartbeat, repository, _notifier, database = supervisor_bits
    supervisor._feed.health.last_tick_at["13"] = datetime(2020, 1, 1, tzinfo=UTC)  # ancient

    supervisor._check_stale_instruments(repository)
    supervisor._check_stale_instruments(repository)  # a second poll, same condition

    rows = _feed_events(database)
    assert [r["event"] for r in rows] == ["stale_instrument"]
    assert rows[0]["security_id"] == "13"


def test_a_recovered_instrument_can_be_reported_stale_again_later(supervisor_bits):
    supervisor, _heartbeat, repository, _notifier, database = supervisor_bits
    supervisor._feed.health.last_tick_at["13"] = datetime(2020, 1, 1, tzinfo=UTC)
    supervisor._check_stale_instruments(repository)

    supervisor._feed.health.last_tick_at["13"] = datetime.now(UTC)  # a fresh tick arrives
    supervisor._check_stale_instruments(repository)  # clears the latch

    # goes stale again
    supervisor._feed.health.last_tick_at["13"] = datetime(2020, 1, 1, tzinfo=UTC)
    supervisor._check_stale_instruments(repository)

    rows = _feed_events(database)
    assert [r["event"] for r in rows] == ["stale_instrument", "stale_instrument"]


# -------------------------------------------------------------- auth events
def test_the_startup_auth_outcome_is_recorded_once_the_repository_opens(tmp_path, database_path):
    supervisor = IntradayOptionsSupervisor(
        SupervisorConfig(
            runtime_id=RUNTIME_ID,
            database_path=database_path,
            lock_dir=tmp_path,
            pid_dir=tmp_path,
            log_dir=tmp_path,
        ),
        adapter=RecordedFeedAdapter([]),
    )
    _register_a_worker(supervisor)
    supervisor.set_startup_auth_outcome(
        source="cache", token_expiry="2026-08-08T09:00:00+05:30", requests_made=0
    )

    supervisor.run()

    database = Database(database_path)
    row = database.connect().execute("SELECT * FROM auth_events").fetchone()
    assert row["event"] == "token_reused_from_cache"
    assert row["token_source"] == "cache"
    assert row["token_expiry"] == "2026-08-08T09:00:00+05:30"


def test_no_auth_event_is_written_when_the_outcome_was_never_set(tmp_path, database_path):
    """Every existing test constructs a supervisor without calling the new
    setter; this pins that they keep working exactly as before."""
    supervisor = IntradayOptionsSupervisor(
        SupervisorConfig(
            runtime_id=RUNTIME_ID,
            database_path=database_path,
            lock_dir=tmp_path,
            pid_dir=tmp_path,
            log_dir=tmp_path,
        ),
        adapter=RecordedFeedAdapter([]),
    )
    _register_a_worker(supervisor)

    supervisor.run()

    database = Database(database_path)
    assert database.connect().execute("SELECT COUNT(*) FROM auth_events").fetchone()[0] == 0


# --------------------------------------------------- heartbeat interval config
def test_the_configured_heartbeat_interval_reaches_the_writer(tmp_path, database_path, monkeypatch):
    """SupervisorConfig.heartbeat_interval_seconds must actually reach the
    HeartbeatWriter run() constructs, not just be accepted and ignored."""
    from runtimes.intraday_options import supervisor as module

    captured: dict[str, object] = {}
    real_writer = module.HeartbeatWriter

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return real_writer(*args, **kwargs)

    monkeypatch.setattr(module, "HeartbeatWriter", _spy)

    supervisor = IntradayOptionsSupervisor(
        SupervisorConfig(
            runtime_id=RUNTIME_ID,
            database_path=database_path,
            lock_dir=tmp_path,
            pid_dir=tmp_path,
            log_dir=tmp_path,
            heartbeat_interval_seconds=42.0,
        ),
        adapter=RecordedFeedAdapter([]),
    )
    _register_a_worker(supervisor)

    supervisor.run()

    assert captured["interval_seconds"] == 42.0
