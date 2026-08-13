"""The account-scoped shared database: schema, migration wiring, path."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from common.config.paths import ProjectPaths
from common.persistence import (
    ACCOUNT_VERSIONS_DIR,
    migrate_account_shared_database,
    open_account_shared_database,
)


def test_account_shared_database_path_is_not_a_runtime_group_file():
    paths = ProjectPaths(project_root=Path("/tmp/algo_trading_test_root"))
    account_path = paths.account_shared_database_path
    assert account_path == paths.operational_root / "dhan_account_shared.db"
    assert account_path != paths.database_path("intraday_options")
    assert account_path != paths.database_path("positional_options")


def test_migrations_apply_and_create_every_expected_table(tmp_path: Path):
    database = open_account_shared_database(tmp_path / "dhan_account_shared.db")
    applied = migrate_account_shared_database(database)

    assert [m.version for m in applied] == ["0001"]
    with database.connect() as conn:
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {
        "schema_migrations",
        "live_account_state_provenance",
        "live_order_rate_windows",
        "live_risk_reservations",
        "live_realised_pnl_events",
        "live_open_positions",
        "live_position_mtm",
        "live_confirmations",
        "live_preflight_results",
    } <= tables
    assert database.integrity_check() == []
    assert database.foreign_key_check() == []


def test_migrations_are_idempotent(tmp_path: Path):
    database = open_account_shared_database(tmp_path / "dhan_account_shared.db")
    migrate_account_shared_database(database)
    assert migrate_account_shared_database(database) == []


def test_reservation_state_check_accepts_every_documented_state(tmp_path: Path):
    database = open_account_shared_database(tmp_path / "dhan_account_shared.db")
    migrate_account_shared_database(database)

    states = [
        "RESERVED",
        "SUBMITTED",
        "ACKNOWLEDGED",
        "PARTIALLY_FILLED",
        "FILLED",
        "REJECTED",
        "CANCELLED",
        "EXPIRED",
        "UNKNOWN",
        "RECONCILED",
        "RELEASED",
    ]
    with database.transaction() as conn:
        for i, state in enumerate(states, start=1):
            conn.execute(
                "INSERT INTO live_risk_reservations (account_key, runtime_id, strategy_id, "
                "correlation_id, trading_date, state, projected_capital, residual_quantity, "
                "created_at, updated_at) VALUES "
                "('acct1', 'intraday_options', 'st01', ?, '2026-08-13', ?, 1000.0, 1, "
                "'2026-08-13T09:15:00Z', '2026-08-13T09:15:00Z')",
                (f"corr_{i:03d}", state),
            )
    with database.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM live_risk_reservations").fetchone()["n"]
    assert count == len(states)


def test_reservation_correlation_id_is_unique_per_account(tmp_path: Path):
    database = open_account_shared_database(tmp_path / "dhan_account_shared.db")
    migrate_account_shared_database(database)

    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO live_risk_reservations (account_key, runtime_id, strategy_id, "
            "correlation_id, trading_date, state, projected_capital, residual_quantity, "
            "created_at, updated_at) VALUES "
            "('acct1', 'intraday_options', 'st01', 'l_io_st01_20260813_0001', '2026-08-13', "
            "'RESERVED', 1000.0, 1, '2026-08-13T09:15:00Z', '2026-08-13T09:15:00Z')"
        )
    with pytest.raises(sqlite3.IntegrityError), database.transaction() as conn:
        conn.execute(
            "INSERT INTO live_risk_reservations (account_key, runtime_id, strategy_id, "
            "correlation_id, trading_date, state, projected_capital, residual_quantity, "
            "created_at, updated_at) VALUES "
            "('acct1', 'intraday_options', 'st01', 'l_io_st01_20260813_0001', "
            "'2026-08-13', 'RESERVED', 500.0, 1, '2026-08-13T09:16:00Z', "
            "'2026-08-13T09:16:00Z')"
        )


def test_realised_pnl_events_replay_is_a_safe_no_op(tmp_path: Path):
    """The exact property that makes SUM() over this table safe: replaying
    the same idempotency_key twice must not double-count."""
    database = open_account_shared_database(tmp_path / "dhan_account_shared.db")
    migrate_account_shared_database(database)

    for _ in range(2):
        with database.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO live_realised_pnl_events (account_key, runtime_id, "
                "strategy_id, trading_date, idempotency_key, realised_pnl_delta, recorded_at) "
                "VALUES ('acct1', 'intraday_options', 'st01', '2026-08-13', 'bf_001', 500.0, "
                "'2026-08-13T09:20:00Z')"
            )

    with database.connect() as conn:
        total = conn.execute(
            "SELECT SUM(realised_pnl_delta) AS total FROM live_realised_pnl_events "
            "WHERE account_key = 'acct1'"
        ).fetchone()["total"]
    assert total == 500.0


def test_position_mtm_overwrite_never_double_counts(tmp_path: Path):
    """The double-counting fix, proven directly: repeated marks for the same
    position must replace, not accumulate."""
    database = open_account_shared_database(tmp_path / "dhan_account_shared.db")
    migrate_account_shared_database(database)

    for pnl, as_of in [(100.0, "T1"), (150.0, "T2"), (90.0, "T3")]:
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO live_position_mtm (account_key, runtime_id, strategy_id, "
                "security_id, unrealised_pnl, as_of) VALUES "
                "('acct1', 'intraday_options', 'st01', 'sec1', ?, ?) "
                "ON CONFLICT (account_key, runtime_id, strategy_id, security_id) "
                "DO UPDATE SET unrealised_pnl = excluded.unrealised_pnl, as_of = excluded.as_of",
                (pnl, as_of),
            )

    with database.connect() as conn:
        rows = conn.execute("SELECT unrealised_pnl, as_of FROM live_position_mtm").fetchall()
    assert len(rows) == 1
    assert rows[0]["unrealised_pnl"] == 90.0
    assert rows[0]["as_of"] == "T3"


def test_versions_dir_points_at_a_separate_directory_from_runtime_group_migrations():
    from common.persistence.migrations import VERSIONS_DIR

    assert ACCOUNT_VERSIONS_DIR != VERSIONS_DIR
    assert ACCOUNT_VERSIONS_DIR.name == "account_versions"
