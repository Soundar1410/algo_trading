"""``dashboards/system_health.py``: active vs. resolved incidents, PIDs,
notification failures, and the live-gate matrix — the page most directly
built to fix "old feed errors after recovery without timestamps."
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from _dashboard_fakes import FakeStreamlit

import dashboards.system_health as health_page
from common.execution import ExecutionRepository
from common.persistence import Database, MigrationRunner
from dashboards.data.incidents import IncidentRow

RUNTIME_ID = "intraday_options"
TRADING_DATE = "2026-08-14"


def _repository(database_path: Path) -> ExecutionRepository:
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    return ExecutionRepository(database)


def test_a_recovered_feed_error_is_shown_as_resolved_not_active(database_path: Path):
    """The exact scenario the module docstring records: a feed outage
    already fixed by a clean restart must not read as currently broken."""
    repository = _repository(database_path)
    now = datetime.now(UTC).isoformat()
    with repository.database.transaction() as conn:
        conn.execute(
            "INSERT INTO feed_events (runtime_id, event, occurred_at) VALUES "
            "('intraday_options', 'disconnected', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO feed_events (runtime_id, event, occurred_at) VALUES "
            "('intraday_options', 'connected', ?)",
            (now,),
        )
    repository.record_error(
        runtime_id=RUNTIME_ID, strategy_id=None, execution_mode=None, severity="ERROR",
        component="feed", message="feed disconnected",
    )

    view = health_page.load_system_health(database_path, RUNTIME_ID, TRADING_DATE)
    assert isinstance(view, health_page.SystemHealthView)
    assert view.active_incidents == ()
    assert len(view.resolved_incidents) == 1
    assert view.resolved_incidents[0].message == "feed disconnected"


def test_an_unrecovered_feed_error_stays_active(database_path: Path):
    repository = _repository(database_path)
    now = datetime.now(UTC).isoformat()
    with repository.database.transaction() as conn:
        conn.execute(
            "INSERT INTO feed_events (runtime_id, event, occurred_at) VALUES "
            "('intraday_options', 'disconnected', ?)",
            (now,),
        )
    repository.record_error(
        runtime_id=RUNTIME_ID, strategy_id=None, execution_mode=None, severity="ERROR",
        component="feed", message="feed disconnected",
    )

    view = health_page.load_system_health(database_path, RUNTIME_ID, TRADING_DATE)
    assert isinstance(view, health_page.SystemHealthView)
    assert len(view.active_incidents) == 1
    assert view.resolved_incidents == ()


def test_notification_delivery_failures_are_surfaced(database_path: Path):
    repository = _repository(database_path)
    repository.record_notification(
        runtime_id=RUNTIME_ID, strategy_id=None, execution_mode=None, channel="telegram",
        event_type="daily_summary", message="summary", delivered=False, failure_reason="timeout",
    )
    repository.record_notification(
        runtime_id=RUNTIME_ID, strategy_id=None, execution_mode=None, channel="telegram",
        event_type="order_filled", message="filled", delivered=True, failure_reason=None,
    )

    view = health_page.load_system_health(database_path, RUNTIME_ID, TRADING_DATE)
    assert isinstance(view, health_page.SystemHealthView)
    assert len(view.notification_failures) == 1
    assert view.notification_failures[0].event_type == "daily_summary"


def test_render_separates_active_from_resolved_sections():
    view = health_page.SystemHealthView(
        runtime_id=RUNTIME_ID, token_expiry=None, auth_event=None, feed_last_event="connected",
        feed_last_reason=None, reconnect_count=1, reconnect_exhausted_count=0,
        stale_instrument_count=0, subscriptions_match=True, database_healthy=True,
        integrity_problems=(), foreign_key_violation_count=0, migration_version="0007",
        group_pid=999, strategy_pids=(),
        active_incidents=(),
        resolved_incidents=(
            IncidentRow(
                strategy_id=None, execution_mode=None, severity="ERROR", component="feed",
                message="old outage", occurred_at="2026-08-14T04:00:00+00:00", active=False,
            ),
        ),
        notification_failures=(),
    )
    st = FakeStreamlit()
    health_page.render(st, view)
    assert any("No active incidents" in s for s in st.successes)
    assert "Resolved / historical incidents (1)" in st.expander_labels
    assert any("old outage" in c for c in st.captions)


def test_render_shows_an_active_incident_as_an_error_not_a_caption():
    view = health_page.SystemHealthView(
        runtime_id=RUNTIME_ID, token_expiry=None, auth_event=None, feed_last_event="disconnected",
        feed_last_reason=None, reconnect_count=1, reconnect_exhausted_count=0,
        stale_instrument_count=0, subscriptions_match=True, database_healthy=True,
        integrity_problems=(), foreign_key_violation_count=0, migration_version="0007",
        group_pid=999, strategy_pids=(),
        active_incidents=(
            IncidentRow(
                strategy_id=None, execution_mode=None, severity="ERROR", component="feed",
                message="feed still down", occurred_at="2026-08-14T08:00:00+00:00", active=True,
            ),
        ),
        resolved_incidents=(),
        notification_failures=(),
    )
    st = FakeStreamlit()
    health_page.render(st, view)
    assert any("feed still down" in e for e in st.errors)
    assert st.successes == []
