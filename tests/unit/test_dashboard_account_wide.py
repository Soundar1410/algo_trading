"""Master dashboard's account-wide risk section: a real, read-only
reflection of ``dhan_account_shared.db`` — spans every runtime group
sharing one Dhan account, not any single runtime group's own database."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import dashboards.app as master_page
from common.persistence import migrate_account_shared_database, open_account_shared_database


class _FakeStreamlit:
    def __init__(self) -> None:
        self.captions: list[str] = []
        self.warnings: list[str] = []
        self.markdowns: list[str] = []
        self.column_calls: list[list[_FakeColumn]] = []

    def markdown(self, text: str) -> None:
        self.markdowns.append(text)

    def caption(self, text: str) -> None:
        self.captions.append(text)

    def warning(self, text: str) -> None:
        self.warnings.append(text)

    def columns(self, n: int) -> list[_FakeColumn]:
        cols = [_FakeColumn(self) for _ in range(n)]
        self.column_calls.append(cols)
        return cols


class _FakeColumn:
    def __init__(self, sink: _FakeStreamlit) -> None:
        self._sink = sink
        self.metrics: list[tuple[str, object]] = []

    def metric(self, label: str, value: object) -> None:
        self.metrics.append((label, value))


def _account_db(tmp_path: Path):
    database = open_account_shared_database(tmp_path / "operational" / "dhan_account_shared.db")
    migrate_account_shared_database(database)
    return database


def test_no_database_yet_reports_unavailable(tmp_path: Path):
    result = master_page.load_account_status(
        tmp_path / "operational" / "missing.db", trading_date="2026-08-13"
    )
    assert isinstance(result, master_page.SnapshotUnavailable)


def test_an_empty_shared_database_reports_zero_accounts(tmp_path: Path):
    database = _account_db(tmp_path)
    result = master_page.load_account_status(database.path, trading_date="2026-08-13")
    assert isinstance(result, master_page.AccountWideStatus)
    assert result.accounts == ()


def test_a_reservation_without_a_provenance_row_still_shows_up_as_never_reconciled(
    tmp_path: Path,
):
    """Spec: a missing/empty shared database (or a missing provenance row
    for one account_key) is never zero exposure — it must be visible and
    flagged, not silently omitted."""
    database = _account_db(tmp_path)
    now = datetime.now(UTC).isoformat()
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO live_risk_reservations (account_key, runtime_id, strategy_id, "
            "correlation_id, trading_date, state, projected_capital, projected_legs, "
            "residual_quantity, created_at, updated_at) VALUES "
            "('acct1', 'intraday_options', 'st01', 'l_st01_20260813_0001', '2026-08-13', "
            "'RESERVED', 20000.0, 1, 75, ?, ?)",
            (now, now),
        )

    result = master_page.load_account_status(database.path, trading_date="2026-08-13")
    assert isinstance(result, master_page.AccountWideStatus)
    assert len(result.accounts) == 1
    account = result.accounts[0]
    assert account.account_key == "acct1"
    assert account.reconciliation_status == "never_reconciled"
    assert account.reserved_capital == 20000.0


def test_a_reconciled_account_with_positions_and_pnl_is_read_back(tmp_path: Path):
    database = _account_db(tmp_path)
    now = datetime.now(UTC).isoformat()
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO live_account_state_provenance (account_key, reconciliation_status, "
            "last_reconciled_at, established_at) VALUES ('acct1', 'reconciled', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO live_open_positions (account_key, runtime_id, strategy_id, "
            "security_id, quantity, average_price, deployed_capital, opened_at, updated_at) "
            "VALUES ('acct1', 'intraday_options', 'st01', '49081', 75, 100.0, 7500.0, ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO live_position_mtm (account_key, runtime_id, strategy_id, "
            "security_id, unrealised_pnl, as_of) VALUES "
            "('acct1', 'intraday_options', 'st01', '49081', 250.0, ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO live_realised_pnl_events (account_key, runtime_id, strategy_id, "
            "trading_date, idempotency_key, realised_pnl_delta, recorded_at) VALUES "
            "('acct1', 'intraday_options', 'st01', '2026-08-13', 'fill_1', 500.0, ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO live_order_rate_windows (account_key, call_class, window_start, "
            "window_seconds, count) VALUES ('acct1', 'new_order', ?, 60, 3)",
            (now,),
        )

    result = master_page.load_account_status(database.path, trading_date="2026-08-13")
    assert isinstance(result, master_page.AccountWideStatus)
    assert len(result.accounts) == 1
    account = result.accounts[0]
    assert account.reconciliation_status == "reconciled"
    assert account.realised_pnl_today == 500.0
    assert account.unrealised_pnl == 250.0
    assert account.open_position_count == 1
    assert account.open_positions_capital == 7500.0
    assert account.new_order_count_current_window == 3
    assert account.has_unmarked_position is False


def test_an_open_position_with_no_mark_is_flagged_unmarked(tmp_path: Path):
    database = _account_db(tmp_path)
    now = datetime.now(UTC).isoformat()
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO live_open_positions (account_key, runtime_id, strategy_id, "
            "security_id, quantity, average_price, deployed_capital, opened_at, updated_at) "
            "VALUES ('acct1', 'intraday_options', 'st01', '49081', 75, 100.0, 7500.0, ?, ?)",
            (now, now),
        )

    result = master_page.load_account_status(database.path, trading_date="2026-08-13")
    assert isinstance(result, master_page.AccountWideStatus)
    account = result.accounts[0]
    assert account.has_unmarked_position is True
    assert account.unrealised_pnl == 0.0


# --------------------------------------------------------------------- render
def test_render_reports_unavailable_without_crashing():
    fake_st = _FakeStreamlit()
    master_page._render_account_status(fake_st, master_page.SnapshotUnavailable("locked"))
    assert any("unavailable" in c for c in fake_st.captions)


def test_render_reports_no_accounts_yet():
    fake_st = _FakeStreamlit()
    status = master_page.AccountWideStatus(trading_date="2026-08-13", accounts=())
    master_page._render_account_status(fake_st, status)
    assert any("No live worker" in c for c in fake_st.captions)


def test_render_warns_when_an_account_is_not_reconciled():
    fake_st = _FakeStreamlit()
    account = master_page.AccountRow(
        account_key="acct1234567890",
        reconciliation_status="never_reconciled",
        realised_pnl_today=0.0,
        unrealised_pnl=0.0,
        has_unmarked_position=False,
        open_position_count=0,
        open_positions_capital=0.0,
        reserved_capital=0.0,
        new_order_count_current_window=0,
    )
    status = master_page.AccountWideStatus(trading_date="2026-08-13", accounts=(account,))
    master_page._render_account_status(fake_st, status)
    assert any("never_reconciled" in w and "blocked" in w for w in fake_st.warnings)


def test_render_shows_metrics_for_a_healthy_reconciled_account():
    fake_st = _FakeStreamlit()
    account = master_page.AccountRow(
        account_key="acct1234567890",
        reconciliation_status="reconciled",
        realised_pnl_today=500.0,
        unrealised_pnl=250.0,
        has_unmarked_position=False,
        open_position_count=1,
        open_positions_capital=7500.0,
        reserved_capital=0.0,
        new_order_count_current_window=3,
    )
    status = master_page.AccountWideStatus(trading_date="2026-08-13", accounts=(account,))
    master_page._render_account_status(fake_st, status)
    assert fake_st.warnings == []
    assert len(fake_st.column_calls) == 1
    metrics = {label: value for col in fake_st.column_calls[0] for label, value in col.metrics}
    assert metrics["Open positions"] == 1
    assert metrics["Daily P&L"] == "750.00"
    assert metrics["New-order calls (current window)"] == 3
