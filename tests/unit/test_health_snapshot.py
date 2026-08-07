"""The health snapshot: one read model over a seeded, migrated database.

Phase 7 Part 1's audit finding: the dashboard built its own ad-hoc SELECTs,
and migration 0002's `auth_events`/`feed_events` had no reader at all. Every
query here runs against ``connect_readonly`` — the same connection the
dashboard uses — so a query that only works on a write connection would fail
this test the same way it would fail in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from common.config.models import ExecutionMode
from common.execution import ExecutionRepository
from common.health.snapshot import _non_negative_age, read_snapshot
from common.persistence import Database, MigrationRunner, connect_readonly

RUNTIME_ID = "intraday_options"
TRADING_DATE = "2026-08-07"


@pytest.fixture
def repository(database_path: Path) -> ExecutionRepository:
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    return ExecutionRepository(database)


def _snapshot(database_path: Path, **kwargs: object):
    """``read_snapshot`` against a fresh read-only connection, defaulted to
    this module's runtime/trading date. Every test opens its own connection
    rather than sharing one, matching how a real reader (the dashboard,
    ``scripts/status``) would."""
    kwargs.setdefault("runtime_id", RUNTIME_ID)
    kwargs.setdefault("trading_date", TRADING_DATE)
    return read_snapshot(connect_readonly(database_path), **kwargs)  # type: ignore[arg-type]


def test_an_empty_database_returns_a_snapshot_with_no_group_and_zeroed_totals(
    repository: ExecutionRepository, database_path: Path
):
    snapshot = _snapshot(database_path)

    assert snapshot.group is None
    assert snapshot.strategies == ()
    assert snapshot.auth.event is None
    assert snapshot.market_data.last_event is None
    assert snapshot.broker.healthy is True
    assert snapshot.open_positions == 0
    assert snapshot.realised_pnl_paper == 0.0
    assert snapshot.orders_today == 0
    assert snapshot.recent_errors == ()
    assert snapshot.database.integrity_ok is True
    assert snapshot.database.migration_version is not None


def test_a_negative_age_from_clock_skew_between_sqlite_and_python_clamps_to_zero():
    """A real, observed flake, not a hypothetical: julianday('now') (SQLite's
    clock) and the Python datetime.now(UTC) that produced beat_at are
    sampled a fraction of a millisecond apart, and a read moments after a
    write occasionally computes a tiny negative age (~-0.001s) — clock skew
    between two individually-correct clocks, not a beat from the future."""
    assert _non_negative_age(-0.0010058283805847168) == 0.0
    assert _non_negative_age(3.5) == 3.5
    assert _non_negative_age(0.0) == 0.0


def test_the_group_heartbeat_is_the_strategy_id_is_null_row(
    repository: ExecutionRepository, database_path: Path
):
    session = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id=None,
        execution_mode=ExecutionMode.PAPER,
        process_role="supervisor",
        pid=4242,
    )
    repository.record_heartbeat(
        session_id=session.id,
        runtime_id=RUNTIME_ID,
        strategy_id=None,
        health_state="RUNNING_PAPER",
    )

    snapshot = _snapshot(database_path)

    assert snapshot.group is not None
    assert snapshot.group.strategy_id is None
    assert snapshot.group.health_state == "RUNNING_PAPER"
    assert snapshot.group.pid == 4242
    assert snapshot.group.heartbeat_age_seconds is not None
    assert snapshot.group.heartbeat_age_seconds >= 0.0


def test_a_strategy_heartbeat_does_not_leak_into_the_group_reading(
    repository: ExecutionRepository, database_path: Path
):
    session = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=1234,
    )
    repository.record_heartbeat(
        session_id=session.id,
        runtime_id=RUNTIME_ID,
        strategy_id="st01",
        health_state="RUNNING_PAPER",
    )

    snapshot = _snapshot(database_path)

    assert snapshot.group is None  # no strategy_id IS NULL row was ever written
    assert len(snapshot.strategies) == 1
    assert snapshot.strategies[0].strategy_id == "st01"
    assert snapshot.strategies[0].health_state == "RUNNING_PAPER"
    assert snapshot.strategies[0].execution_mode == "paper"
    assert snapshot.strategies[0].pid == 1234


def test_a_strategys_square_off_state_and_entries_blocked_come_from_strategy_state(
    repository: ExecutionRepository, database_path: Path
):
    repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=1,
    )
    session = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=42,
    )
    repository.record_heartbeat(
        session_id=session.id,
        runtime_id=RUNTIME_ID,
        strategy_id="st01",
        health_state="BLOCK_NEW_ENTRIES",
    )
    repository.save_strategy_state(
        runtime_id=RUNTIME_ID,
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        square_off_state="IN_PROGRESS",
        entries_blocked=True,
    )

    snapshot = _snapshot(database_path)

    strategy = snapshot.strategies[0]
    assert strategy.pid == 42  # the latest session's, not the first
    assert strategy.square_off_state == "IN_PROGRESS"
    assert strategy.entries_blocked is True


def test_a_strategy_with_no_persisted_state_reports_none_not_a_default(
    repository: ExecutionRepository, database_path: Path
):
    session = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=1,
    )
    repository.record_heartbeat(
        session_id=session.id, runtime_id=RUNTIME_ID, strategy_id="st01", health_state="STARTING"
    )

    snapshot = _snapshot(database_path)

    strategy = snapshot.strategies[0]
    assert strategy.square_off_state is None
    assert strategy.entries_blocked is None
    assert strategy.open_positions == 0


def test_a_strategys_open_positions_are_counted_for_its_own_trading_date_only(
    repository: ExecutionRepository, database_path: Path
):
    session = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=1,
    )
    repository.record_heartbeat(
        session_id=session.id,
        runtime_id=RUNTIME_ID,
        strategy_id="st01",
        health_state="RUNNING_PAPER",
    )
    conn = repository.database.connect()
    conn.execute(
        """
        INSERT INTO positions
            (runtime_id, strategy_id, execution_mode, trading_date, instrument,
             security_id, quantity, average_price, status, opened_at, updated_at)
        VALUES (?, 'st01', 'paper', ?, 'NIFTY', '1', 50, 100.0, 'OPEN', 'x', 'x')
        """,
        (RUNTIME_ID, TRADING_DATE),
    )
    # A closed position, and one on a different trading date: neither counts.
    conn.execute(
        """
        INSERT INTO positions
            (runtime_id, strategy_id, execution_mode, trading_date, instrument,
             security_id, quantity, average_price, status, opened_at, updated_at)
        VALUES (?, 'st01', 'paper', ?, 'NIFTY', '2', 0, 100.0, 'CLOSED', 'x', 'x')
        """,
        (RUNTIME_ID, TRADING_DATE),
    )
    conn.execute(
        """
        INSERT INTO positions
            (runtime_id, strategy_id, execution_mode, trading_date, instrument,
             security_id, quantity, average_price, status, opened_at, updated_at)
        VALUES (?, 'st01', 'paper', '2020-01-01', 'NIFTY', '3', 50, 100.0, 'OPEN', 'x', 'x')
        """,
        (RUNTIME_ID,),
    )

    snapshot = _snapshot(database_path)

    assert snapshot.strategies[0].open_positions == 1


def test_a_strategys_latest_error_is_attached_to_its_own_row_only(
    repository: ExecutionRepository, database_path: Path
):
    for strategy_id in ("st01", "st02"):
        session = repository.open_session(
            runtime_id=RUNTIME_ID,
            strategy_id=strategy_id,
            execution_mode=ExecutionMode.PAPER,
            process_role="worker",
            pid=1,
        )
        repository.record_heartbeat(
            session_id=session.id,
            runtime_id=RUNTIME_ID,
            strategy_id=strategy_id,
            health_state="RUNNING_PAPER",
        )
    repository.record_error(
        runtime_id=RUNTIME_ID,
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        severity="ERROR",
        component="worker",
        message="st01 blew up",
    )

    snapshot = _snapshot(database_path)

    by_id = {s.strategy_id: s for s in snapshot.strategies}
    assert by_id["st01"].last_error == "st01 blew up"
    assert by_id["st02"].last_error is None


def test_auth_health_reports_the_latest_event(repository: ExecutionRepository, database_path: Path):
    repository.record_auth_event(runtime_id=RUNTIME_ID, event="token_reused_from_env")
    repository.record_auth_event(
        runtime_id=RUNTIME_ID,
        event="token_generated",
        token_source="generated",
        token_expiry="2026-08-08T09:00:00+05:30",
        requests_made=2,
    )

    snapshot = _snapshot(database_path)

    assert snapshot.auth.event == "token_generated"
    assert snapshot.auth.token_source == "generated"
    assert snapshot.auth.token_expiry == "2026-08-08T09:00:00+05:30"


def test_market_data_health_aggregates_across_feed_events(
    repository: ExecutionRepository, database_path: Path
):
    repository.record_feed_event(runtime_id=RUNTIME_ID, event="connected")
    repository.record_feed_event(
        runtime_id=RUNTIME_ID, event="disconnected", reason="socket died", reason_code=None
    )
    repository.record_feed_event(runtime_id=RUNTIME_ID, event="reconnect_attempted", attempt=1)
    repository.record_feed_event(runtime_id=RUNTIME_ID, event="reconnect_attempted", attempt=2)
    repository.record_feed_event(
        runtime_id=RUNTIME_ID,
        event="connected",
        downtime_seconds=12.5,
    )
    repository.record_feed_event(
        runtime_id=RUNTIME_ID,
        event="resubscribed",
        expected_subscriptions=3,
        active_subscriptions=3,
    )
    repository.record_feed_event(
        runtime_id=RUNTIME_ID, event="stale_instrument", security_id="99926000"
    )

    snapshot = _snapshot(database_path)
    market = snapshot.market_data

    assert market.last_event == "stale_instrument"
    assert market.reconnect_count == 2
    assert market.stale_instrument_count == 1
    assert market.total_downtime_seconds == pytest.approx(12.5)
    assert market.expected_subscriptions == 3
    assert market.active_subscriptions == 3
    assert market.subscriptions_match is True


def test_broker_health_reflects_the_latest_broker_component_error(
    repository: ExecutionRepository, database_path: Path
):
    repository.record_error(
        runtime_id=RUNTIME_ID,
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        severity="ERROR",
        component="broker",
        message="broker unhealthy at startup",
    )
    # A non-broker error must not be mistaken for a broker problem.
    repository.record_error(
        runtime_id=RUNTIME_ID,
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        severity="ERROR",
        component="feed",
        message="unrelated feed error",
    )

    snapshot = _snapshot(database_path)

    assert snapshot.broker.healthy is False
    assert snapshot.broker.last_error == "broker unhealthy at startup"


def test_database_health_runs_clean_on_a_freshly_migrated_database(
    repository: ExecutionRepository, database_path: Path
):
    snapshot = _snapshot(database_path)

    assert snapshot.database.integrity_ok is True
    assert snapshot.database.integrity_problems == ()
    assert snapshot.database.foreign_key_violation_count == 0
    assert snapshot.database.journal_mode == "wal"


def test_recent_errors_respects_the_limit_and_most_recent_first(
    repository: ExecutionRepository, database_path: Path
):
    for i in range(3):
        repository.record_error(
            runtime_id=RUNTIME_ID,
            strategy_id=None,
            execution_mode=None,
            severity="ERROR",
            component="worker",
            message=f"error {i}",
        )

    snapshot = _snapshot(database_path, recent_error_limit=2)

    assert snapshot.recent_errors == ("error 2", "error 1")


def test_a_snapshot_never_writes_through_a_readonly_connection(
    repository: ExecutionRepository, database_path: Path
):
    """The health snapshot must be exercisable through exactly the connection
    the dashboard actually uses — a query that only works on a write
    connection would silently pass any test built against one."""
    conn = connect_readonly(database_path)
    read_snapshot(conn, runtime_id=RUNTIME_ID, trading_date=TRADING_DATE)
    import sqlite3

    with pytest.raises(sqlite3.OperationalError, match="readonly database"):
        conn.execute("INSERT INTO errors (runtime_id, severity, component, message, occurred_at) "
                      "VALUES ('x', 'ERROR', 'x', 'x', 'x')")


def test_a_different_runtime_id_is_never_mixed_in(
    repository: ExecutionRepository, database_path: Path
):
    session = repository.open_session(
        runtime_id="positional_options",
        strategy_id=None,
        execution_mode=ExecutionMode.PAPER,
        process_role="supervisor",
        pid=1,
    )
    repository.record_heartbeat(
        session_id=session.id,
        runtime_id="positional_options",
        strategy_id=None,
        health_state="RUNNING_PAPER",
    )

    snapshot = _snapshot(database_path)

    assert snapshot.group is None
