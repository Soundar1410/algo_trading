"""Master dashboard's reconciliation-status section: a real, read-only
reflection of reconciliation_runs — no longer the "Not implemented"
placeholder for the path that actually reads a database."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import dashboards.app as master_page
from common.persistence import Database, MigrationRunner


class _FakeStreamlit:
    def __init__(self) -> None:
        self.captions: list[str] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def caption(self, text: str) -> None:
        self.captions.append(text)

    def error(self, text: str) -> None:
        self.errors.append(text)

    def warning(self, text: str) -> None:
        self.warnings.append(text)


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "operational" / "intraday_options.db")
    MigrationRunner(database).run_pending()
    return database


def test_no_database_yet_reports_unavailable(tmp_path: Path):
    result = master_page.load_reconciliation_status(tmp_path / "operational" / "missing.db")
    assert isinstance(result, master_page.SnapshotUnavailable)


def test_no_runs_yet_returns_none(tmp_path: Path):
    database = _database(tmp_path)
    result = master_page.load_reconciliation_status(database.path)
    assert result is None


def test_a_completed_clean_run_is_read_back(tmp_path: Path):
    database = _database(tmp_path)
    now = datetime.now(UTC).isoformat()
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO reconciliation_runs (runtime_id, execution_mode, trigger_source, "
            "started_at, completed_at, status, critical_mismatch_count, entries_blocked) "
            "VALUES ('intraday_options', 'live', 'startup', ?, ?, 'completed', 0, 0)",
            (now, now),
        )
    result = master_page.load_reconciliation_status(database.path)
    assert isinstance(result, master_page.ReconciliationStatus)
    assert result.run_status == "completed"
    assert result.critical_mismatch_count == 0
    assert not result.entries_blocked


def test_the_latest_run_is_returned_not_an_older_one(tmp_path: Path):
    database = _database(tmp_path)
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO reconciliation_runs (runtime_id, execution_mode, trigger_source, "
            "started_at, status, critical_mismatch_count, entries_blocked) VALUES "
            "('intraday_options', 'live', 'startup', '2026-08-01T09:00:00Z', 'completed', 0, 0)"
        )
        conn.execute(
            "INSERT INTO reconciliation_runs (runtime_id, execution_mode, trigger_source, "
            "started_at, status, critical_mismatch_count, entries_blocked) VALUES "
            "('intraday_options', 'live', 'startup', '2026-08-13T09:00:00Z', 'failed', 0, 1)"
        )
    result = master_page.load_reconciliation_status(database.path)
    assert isinstance(result, master_page.ReconciliationStatus)
    assert result.started_at == "2026-08-13T09:00:00Z"
    assert result.run_status == "failed"


# --------------------------------------------------------------------- render
def test_render_shows_the_placeholder_when_status_is_none():
    """Every hand-built RuntimeCard from before this field existed keeps
    behaving exactly as it always did."""
    fake_st = _FakeStreamlit()
    master_page._render_reconciliation_status(fake_st, None)
    assert any(master_page.RECONCILIATION_STATUS in c for c in fake_st.captions)


def test_render_shows_a_clean_completed_run_as_a_caption():
    fake_st = _FakeStreamlit()
    status = master_page.ReconciliationStatus(
        run_status="completed",
        critical_mismatch_count=0,
        entries_blocked=False,
        started_at="2026-08-13T09:00:00Z",
        completed_at="2026-08-13T09:00:05Z",
    )
    master_page._render_reconciliation_status(fake_st, status)
    assert any("completed" in c for c in fake_st.captions)
    assert fake_st.warnings == []
    assert fake_st.errors == []


def test_render_warns_when_entries_are_blocked():
    fake_st = _FakeStreamlit()
    status = master_page.ReconciliationStatus(
        run_status="completed",
        critical_mismatch_count=2,
        entries_blocked=True,
        started_at="2026-08-13T09:00:00Z",
        completed_at="2026-08-13T09:00:05Z",
    )
    master_page._render_reconciliation_status(fake_st, status)
    assert any("blocked" in w for w in fake_st.warnings)


def test_render_errors_on_a_failed_run():
    fake_st = _FakeStreamlit()
    status = master_page.ReconciliationStatus(
        run_status="failed",
        critical_mismatch_count=0,
        entries_blocked=True,
        started_at="2026-08-13T09:00:00Z",
        completed_at=None,
    )
    master_page._render_reconciliation_status(fake_st, status)
    assert any("FAILED" in e for e in fake_st.errors)


def test_render_reports_a_read_failure_without_crashing():
    fake_st = _FakeStreamlit()
    master_page._render_reconciliation_status(
        fake_st, master_page.SnapshotUnavailable("locked")
    )
    assert any("unavailable" in c for c in fake_st.captions)


# ------------------------------------------------------------------ load_master
def test_load_master_populates_real_reconciliation_status(tmp_path: Path):
    database = _database(tmp_path)
    now = datetime.now(UTC).isoformat()
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO runtime_sessions (runtime_id, strategy_id, execution_mode, "
            "process_role, pid, started_at) VALUES "
            "('intraday_options', 'st01', 'paper', 'worker', 1, ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO reconciliation_runs (runtime_id, execution_mode, trigger_source, "
            "started_at, completed_at, status, critical_mismatch_count, entries_blocked) "
            "VALUES ('intraday_options', 'live', 'startup', ?, ?, 'completed', 1, 1)",
            (now, now),
        )

    card = master_page.load_master(database.path, "intraday_options", "2026-08-13")
    assert isinstance(card, master_page.RuntimeCard)
    assert isinstance(card.reconciliation_status, master_page.ReconciliationStatus)
    assert card.reconciliation_status.critical_mismatch_count == 1
    assert card.reconciliation_status.entries_blocked
