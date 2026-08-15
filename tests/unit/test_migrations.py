"""Migration runner: schema_migrations, idempotency, replay safety, integrity."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from common.persistence import (
    Database,
    DatabaseError,
    Migration,
    MigrationError,
    MigrationRunner,
    discover_migrations,
    migrate,
)

FIRST = """
CREATE TABLE IF NOT EXISTS runtime_sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id   TEXT NOT NULL,
    started_at   TEXT NOT NULL
);
"""

SECOND = """
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES runtime_sessions(id),
    correlation_id  TEXT NOT NULL UNIQUE,
    execution_mode  TEXT NOT NULL CHECK (execution_mode IN ('paper','live'))
);
CREATE INDEX IF NOT EXISTS idx_orders_correlation ON orders (correlation_id);
"""


@pytest.fixture
def versions(tmp_path: Path) -> Path:
    directory = tmp_path / "versions"
    directory.mkdir()
    (directory / "0001_runtime_sessions.sql").write_text(FIRST, encoding="utf-8")
    (directory / "0002_orders.sql").write_text(SECOND, encoding="utf-8")
    return directory


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "operational" / "intraday_options.db")


def _runner(db: Database, versions: Path) -> MigrationRunner:
    return MigrationRunner(db, versions_dir=versions)


# ------------------------------------------------------------- discovery
def test_filename_supplies_version_and_name(tmp_path: Path):
    # Phase 10: from_path also computes a checksum (see Migration.checksum),
    # so the file must actually exist — unrelated to what this test checks
    # (filename -> version/name parsing).
    path = tmp_path / "0007_add_positions.sql"
    path.write_text("CREATE TABLE IF NOT EXISTS positions (id INTEGER);\n", encoding="utf-8")

    migration = Migration.from_path(path)
    assert migration.version == "0007"
    assert migration.name == "add_positions"


def test_migrations_are_ordered_by_filename(versions: Path):
    assert [m.version for m in discover_migrations(versions)] == ["0001", "0002"]


def test_badly_named_migration_is_rejected(tmp_path: Path):
    directory = tmp_path / "versions"
    directory.mkdir()
    (directory / "add_stuff.sql").write_text(FIRST, encoding="utf-8")
    with pytest.raises(MigrationError, match=r"0001_description\.sql"):
        discover_migrations(directory)


def test_duplicate_version_is_rejected(tmp_path: Path):
    """Two 0002s would apply in an order that depends on the filesystem."""
    directory = tmp_path / "versions"
    directory.mkdir()
    (directory / "0002_a.sql").write_text(FIRST, encoding="utf-8")
    (directory / "0002_b.sql").write_text(SECOND, encoding="utf-8")
    with pytest.raises(MigrationError, match="Duplicate migration version"):
        discover_migrations(directory)


def test_missing_versions_directory_yields_nothing(tmp_path: Path):
    assert discover_migrations(tmp_path / "absent") == []


# --------------------------------------------------------------- applying
def test_fresh_database_gets_schema_migrations_table(db: Database, versions: Path):
    _runner(db, versions).run_pending()
    tables = {
        row["name"]
        for row in db.connect().execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "schema_migrations" in tables


def test_all_pending_migrations_are_applied(db: Database, versions: Path):
    applied = _runner(db, versions).run_pending()
    assert [m.version for m in applied] == ["0001", "0002"]

    tables = {
        row["name"]
        for row in db.connect().execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"runtime_sessions", "orders"} <= tables


def test_version_name_and_applied_at_are_recorded(db: Database, versions: Path):
    _runner(db, versions).run_pending()
    rows = (
        db.connect()
        .execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
        .fetchall()
    )

    assert [r["version"] for r in rows] == ["0001", "0002"]
    assert rows[0]["name"] == "runtime_sessions"
    assert rows[0]["applied_at"].startswith("20")  # ISO-8601 UTC timestamp


def test_second_run_applies_nothing(db: Database, versions: Path):
    _runner(db, versions).run_pending()
    assert _runner(db, versions).run_pending() == []


def test_repeated_runs_do_not_duplicate_rows(db: Database, versions: Path):
    for _ in range(3):
        _runner(db, versions).run_pending()
    count = db.connect().execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert count == 2


def test_only_the_new_migration_is_applied_on_upgrade(db: Database, versions: Path):
    (versions / "0002_orders.sql").unlink()
    _runner(db, versions).run_pending()

    (versions / "0002_orders.sql").write_text(SECOND, encoding="utf-8")
    newly = _runner(db, versions).run_pending()
    assert [m.version for m in newly] == ["0002"]


def test_pending_and_current_version_report_state(db: Database, versions: Path):
    runner = _runner(db, versions)
    assert runner.current_version() is None
    assert [m.version for m in runner.pending()] == ["0001", "0002"]

    runner.run_pending()
    assert runner.current_version() == "0002"
    assert runner.pending() == []


def test_migrate_helper_matches_the_runner(db: Database, versions: Path):
    assert [m.version for m in migrate(db, versions_dir=versions)] == ["0001", "0002"]


# ------------------------------------------------------- replay safety
def test_destructive_migration_is_rejected(db: Database, tmp_path: Path):
    directory = tmp_path / "versions"
    directory.mkdir()
    (directory / "0001_drop.sql").write_text("DROP TABLE orders;", encoding="utf-8")
    with pytest.raises(MigrationError, match="destructive statement"):
        _runner(db, directory).run_pending()


def test_non_idempotent_create_is_rejected(db: Database, tmp_path: Path):
    """The runner replays scripts after a crash, so a bare CREATE would fail."""
    directory = tmp_path / "versions"
    directory.mkdir()
    (directory / "0001_bare.sql").write_text("CREATE TABLE t (v INTEGER);", encoding="utf-8")
    with pytest.raises(MigrationError, match="IF NOT EXISTS"):
        _runner(db, directory).run_pending()


def test_replaying_an_applied_but_unrecorded_migration_is_a_no_op(db: Database, versions: Path):
    """Simulates a crash after the schema change but before the INSERT."""
    _runner(db, versions).run_pending()
    db.connect().execute("DELETE FROM schema_migrations WHERE version = '0002'")

    newly = _runner(db, versions).run_pending()
    assert [m.version for m in newly] == ["0002"]
    assert db.integrity_check() == []


def test_invalid_sql_raises_migration_error(db: Database, tmp_path: Path):
    directory = tmp_path / "versions"
    directory.mkdir()
    (directory / "0001_broken.sql").write_text("CREATE TABLE IF NOT EXISTS ((( ;", encoding="utf-8")
    with pytest.raises(MigrationError, match="failed"):
        _runner(db, directory).run_pending()


# ---------------------------------------------------------- integrity
def test_integrity_checks_run_after_a_batch(db: Database, versions: Path):
    _runner(db, versions).run_pending()
    assert db.foreign_key_check() == []
    assert db.integrity_check() == []


def test_migration_writing_a_dangling_reference_fails_immediately(db: Database, tmp_path: Path):
    """`foreign_keys=ON` rejects the orphan at insert time, before the batch ends."""
    directory = tmp_path / "versions"
    directory.mkdir()
    (directory / "0001_orphan.sql").write_text(
        """
        CREATE TABLE IF NOT EXISTS parent (id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS child (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER NOT NULL REFERENCES parent(id)
        );
        INSERT INTO child (id, parent_id) VALUES (1, 404);
        """,
        encoding="utf-8",
    )
    with pytest.raises(MigrationError, match="FOREIGN KEY constraint failed"):
        _runner(db, directory).run_pending()


def test_post_migration_check_catches_violations_enforcement_cannot(db: Database, versions: Path):
    """The batch check is the net for orphans that bypassed live enforcement.

    Rows written while `foreign_keys` was off — by an older build, an external
    tool, or a manual repair — are invisible to insert-time enforcement. The
    post-batch `foreign_key_check` is what stops that database being used.
    """
    conn = db.connect()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS parent (id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS child (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER NOT NULL REFERENCES parent(id)
        );
        INSERT INTO child (id, parent_id) VALUES (1, 404);
        """
    )
    conn.execute("PRAGMA foreign_keys = ON")
    assert db.foreign_key_check() != []

    with pytest.raises(DatabaseError, match="Foreign-key violations"):
        _runner(db, versions).run_pending()


# ------------------------------------------------------------------ lock
def test_migration_lock_file_is_used(db: Database, versions: Path, tmp_path: Path):
    lock_path = tmp_path / "locks" / "migrate.lock"
    MigrationRunner(db, versions_dir=versions, lock_path=lock_path).run_pending()
    assert lock_path.parent.is_dir()


def test_a_held_lock_times_out_rather_than_racing(db: Database, versions: Path, tmp_path: Path):
    """Two supervisors starting together must not both apply version 0001."""
    from filelock import FileLock

    lock_path = tmp_path / "locks" / "migrate.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    holder = FileLock(str(lock_path))
    holder.acquire()
    try:
        runner = MigrationRunner(
            db, versions_dir=versions, lock_path=lock_path, lock_timeout_seconds=0.1
        )
        with pytest.raises(MigrationError, match="migration lock"):
            runner.run_pending()
    finally:
        holder.release()


# ------------------------------------------------------- shipped versions
def test_shipped_migrations_start_at_the_walking_skeleton():
    """0001 is the walking skeleton and nothing may be inserted before it.

    Migrations are forward-only, so an earlier number appearing later would mean
    a database that skipped it never gets it.
    """
    from common.persistence.migrations import VERSIONS_DIR

    shipped = discover_migrations(VERSIONS_DIR)
    assert [m.version for m in shipped] == [
        "0001",
        "0002",
        "0003",
        "0004",
        "0005",
        "0006",
        "0007",
        "0008",
        "0009",
    ]
    assert shipped[0].name == "walking_skeleton"
    assert shipped[1].name == "feed_and_auth_health"
    assert shipped[2].name == "paper_fill_realism"
    assert shipped[3].name == "operator_audit"
    assert shipped[4].name == "retention_indexes"
    # Phase 10.
    assert shipped[5].name == "widen_order_status_for_expired"
    assert shipped[6].name == "reconciliation_tables"
    # Dashboard corrective pass.
    assert shipped[7].name == "trade_ledger"
    # strategy-straddle-920: generic multi-leg basket/leg support.
    assert shipped[8].name == "multi_leg_baskets"


def test_shipped_migrations_apply_to_a_fresh_database(tmp_path: Path):
    """The real migrations must survive the runner's own replay-safety rules.

    0006 is fine to apply unconfirmed here: a fresh database's `orders` table
    has zero rows, so MigrationRunner._check_destructive_preconditions never
    requires require_fresh_backup_for_destructive for this case (nothing to
    lose) — see the dedicated 0006-specific tests below for the non-empty
    case, which does require it.
    """
    from common.persistence.migrations import VERSIONS_DIR

    database = Database(tmp_path / "operational" / "intraday_options.db")
    applied = MigrationRunner(database, versions_dir=VERSIONS_DIR).run_pending()

    assert [m.version for m in applied] == [
        "0001",
        "0002",
        "0003",
        "0004",
        "0005",
        "0006",
        "0007",
        "0008",
        "0009",
    ]
    assert database.integrity_check() == []
    assert database.foreign_key_check() == []
    with database.connect() as conn:
        status_check_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='orders'"
        ).fetchone()["sql"]
    assert "EXPIRED" in status_check_sql
    with database.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"strategy_baskets", "strategy_legs"} <= tables


def test_later_migrations_upgrade_a_database_created_by_0001_alone(tmp_path: Path):
    """The real upgrade path: an existing paper database must not need rebuilding.

    Applying 0001 by itself, then the full set, proves the later migrations are
    purely additive rather than only working on a database built from scratch.
    """
    from common.persistence.migrations import VERSIONS_DIR, discover_migrations

    only_0001 = tmp_path / "just_first"
    only_0001.mkdir()
    first = discover_migrations(VERSIONS_DIR)[0]
    (only_0001 / first.path.name).write_text(
        first.path.read_text(encoding="utf-8"), encoding="utf-8"
    )

    database = Database(tmp_path / "operational" / "intraday_options.db")
    MigrationRunner(database, versions_dir=only_0001).run_pending()

    # Put a row in a 0001 table, so a destructive later migration is detectable.
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO runtime_sessions (runtime_id, execution_mode, process_role, "
            "pid, started_at) VALUES (?, 'paper', 'worker', 1, '2026-07-30T09:15:00Z')",
            ("intraday_options",),
        )

    applied = MigrationRunner(database, versions_dir=VERSIONS_DIR).run_pending()

    # 0006 applies without require_fresh_backup_for_destructive here: this
    # database's `orders` table (created by 0001, never populated by this
    # test) has zero rows, so MigrationRunner._check_destructive_preconditions
    # has nothing to protect — see the dedicated 0006 tests for the
    # non-empty-table case, which does require it.
    assert [m.version for m in applied] == [
        "0002",
        "0003",
        "0004",
        "0005",
        "0006",
        "0007",
        "0008",
        "0009",
    ]
    with database.connect() as conn:
        survivors = conn.execute("SELECT COUNT(*) FROM runtime_sessions").fetchone()[0]
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert survivors == 1, "a later migration must not disturb existing rows"
    assert {"auth_events", "feed_events", "option_chain_snapshots"} <= tables
    assert "paper_fill_quotes" in tables
    assert "audit_events" in tables
    assert {"strategy_baskets", "strategy_legs"} <= tables
    assert database.integrity_check() == []


def test_0003_records_the_quote_in_a_side_table_not_new_fills_columns(tmp_path: Path):
    """Phase 4 Part 5, and the reason is the runner's own rule.

    SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``, and every
    migration here must be a no-op on replay because ``executescript()`` commits
    implicitly and cannot be undone (deviation D6). Widening ``fills`` would
    therefore fail on the second run — which is the whole safety mechanism.
    """
    from common.persistence.migrations import VERSIONS_DIR

    database = Database(tmp_path / "operational" / "intraday_options.db")
    MigrationRunner(database, versions_dir=VERSIONS_DIR).run_pending()

    with database.connect() as conn:
        fills = {row["name"] for row in conn.execute("PRAGMA table_info(fills)")}
        quotes = {row["name"] for row in conn.execute("PRAGMA table_info(paper_fill_quotes)")}

    assert "quote_bid" not in fills, "fills must not have been widened in place"
    assert {"quote_bid", "quote_ask", "quote_age_ms", "latency_applied"} <= quotes


def test_no_phase_two_table_can_store_a_secret(tmp_path: Path):
    """Structural check on the schema, not on call sites.

    A column named for a token or credential is an invitation to write one, and
    the spec forbids persisting secrets to SQLite outright.
    """
    from common.persistence.migrations import VERSIONS_DIR

    database = Database(tmp_path / "operational" / "intraday_options.db")
    MigrationRunner(database, versions_dir=VERSIONS_DIR).run_pending()

    forbidden = ("token", "secret", "pin", "password", "totp", "client_id", "access")
    with database.connect() as conn:
        for table in ("auth_events", "feed_events", "option_chain_snapshots", "audit_events"):
            columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
            for column in columns:
                lowered = column.lower()
                for word in forbidden:
                    # token_expiry and token_source name metadata, not the token.
                    if lowered in {"token_expiry", "token_source"}:
                        continue
                    assert word not in lowered, f"{table}.{column} looks like a secret store"


def test_shipped_migrations_are_idempotent(tmp_path: Path):
    from common.persistence.migrations import VERSIONS_DIR

    database = Database(tmp_path / "operational" / "intraday_options.db")
    MigrationRunner(database, versions_dir=VERSIONS_DIR).run_pending()
    assert MigrationRunner(database, versions_dir=VERSIONS_DIR).run_pending() == []


# ================================================================= Phase 10
# ------------------------------------------------- checksum bootstrap/verify
def test_checksum_baseline_is_established_for_a_legacy_database(db: Database, versions: Path):
    """A database migrated before Phase 10 has no ``checksum`` column at all.

    The next run must add it and backfill every already-applied row from
    the file currently on disk — a one-time trust point — rather than
    require a rebuild or silently skip verification forever.
    """
    MigrationRunner(db, versions_dir=versions).run_pending()
    with db.transaction() as conn:
        conn.execute("ALTER TABLE schema_migrations DROP COLUMN checksum")

    MigrationRunner(db, versions_dir=versions).run_pending()

    with db.connect() as conn:
        rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    assert {r["version"] for r in rows} == {"0001", "0002"}
    assert all(r["checksum"] for r in rows), "every pre-existing row must get a real checksum"


def test_checksum_verification_passes_on_an_unmodified_database(db: Database, versions: Path):
    MigrationRunner(db, versions_dir=versions).run_pending()
    # Must not raise.
    MigrationRunner(db, versions_dir=versions).run_pending()


def test_a_hand_edited_applied_migration_is_refused_at_startup(db: Database, versions: Path):
    MigrationRunner(db, versions_dir=versions).run_pending()

    (versions / "0001_runtime_sessions.sql").write_text(
        FIRST + "\n-- an edit made after this migration was already applied\n",
        encoding="utf-8",
    )

    with pytest.raises(MigrationError, match="has changed since it was applied"):
        MigrationRunner(db, versions_dir=versions).run_pending()


def test_a_deleted_checksummed_migration_is_refused_at_startup(
    db: Database, versions: Path
):
    MigrationRunner(db, versions_dir=versions).run_pending()
    (versions / "0001_runtime_sessions.sql").unlink()

    with pytest.raises(MigrationError, match="has no corresponding file"):
        MigrationRunner(db, versions_dir=versions).run_pending()


def test_checksum_bootstrap_refuses_when_the_original_file_is_gone(
    db: Database, versions: Path, tmp_path: Path
):
    """Cannot establish a trust baseline for a version with no file on disk."""
    MigrationRunner(db, versions_dir=versions).run_pending()
    with db.transaction() as conn:
        conn.execute("ALTER TABLE schema_migrations DROP COLUMN checksum")

    empty_versions = tmp_path / "empty_versions"
    empty_versions.mkdir()

    with pytest.raises(MigrationError, match="no corresponding file"):
        MigrationRunner(db, versions_dir=empty_versions).run_pending()


# ---------------------------------------- failed-migration transaction safety
def test_a_migration_that_fails_partway_leaves_no_recorded_version_and_is_retried_next_startup(
    db: Database, tmp_path: Path
):
    """The runner's core safety argument, proven end to end rather than
    merely asserted in a docstring: a crash (or here, a genuine SQL error)
    partway through a multi-statement migration leaves whatever ran before
    the bad statement in place (``executescript`` auto-commits per
    statement) but records **no** version — and the next attempt safely
    replays the already-applied part (``IF NOT EXISTS`` no-ops) before
    hitting the same failure, never silently continuing and never
    falsely marking the version as applied."""
    broken_versions = tmp_path / "broken"
    broken_versions.mkdir()
    (broken_versions / "0001_partly_broken.sql").write_text(
        "CREATE TABLE IF NOT EXISTS ok_table (id INTEGER);\n"
        "THIS IS NOT VALID SQL AT ALL;\n",
        encoding="utf-8",
    )

    runner = MigrationRunner(db, versions_dir=broken_versions)
    with pytest.raises(MigrationError, match="failed"):
        runner.run_pending()

    with db.connect() as conn:
        tables = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        recorded = conn.execute(
            "SELECT COUNT(*) AS n FROM schema_migrations WHERE version = '0001'"
        ).fetchone()["n"]
    assert "ok_table" in tables, "the statement before the bad one still ran"
    assert recorded == 0, "a failed migration must never be recorded as applied"

    # Fix the file in place — safe, because it was never recorded as applied,
    # so this is not "editing an already-applied migration".
    (broken_versions / "0001_partly_broken.sql").write_text(
        "CREATE TABLE IF NOT EXISTS ok_table (id INTEGER);\n"
        "CREATE TABLE IF NOT EXISTS also_ok (id INTEGER);\n",
        encoding="utf-8",
    )

    applied = MigrationRunner(db, versions_dir=broken_versions).run_pending()

    assert [m.version for m in applied] == ["0001"]
    with db.connect() as conn:
        tables = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        recorded = conn.execute(
            "SELECT COUNT(*) AS n FROM schema_migrations WHERE version = '0001'"
        ).fetchone()["n"]
    assert {"ok_table", "also_ok"} <= tables
    assert recorded == 1


# ------------------------------------------------- 0006 (reviewed-destructive)
def test_migration_0006_upgrades_an_existing_orders_table_with_data(tmp_path: Path):
    """The real upgrade path, not just a fresh database: seed a database
    through 0001-0005 only, put one row per pre-existing status in
    ``orders`` plus a referencing ``fills`` row, then apply 0006 and prove
    every prior row survives untouched, referential integrity holds, a new
    ``EXPIRED`` row is now accepted, and an unrecognised status is still
    rejected by the (now wider) CHECK."""
    from common.persistence.migrations import VERSIONS_DIR

    up_to_0005 = tmp_path / "up_to_0005"
    up_to_0005.mkdir()
    for migration in discover_migrations(VERSIONS_DIR):
        if migration.version in {"0006", "0007"}:
            continue
        (up_to_0005 / migration.path.name).write_text(
            migration.path.read_text(encoding="utf-8"), encoding="utf-8"
        )

    database = Database(tmp_path / "operational" / "intraday_options.db")
    MigrationRunner(database, versions_dir=up_to_0005).run_pending()

    statuses = [
        "PENDING",
        "SUBMITTED",
        "ACKNOWLEDGED",
        "PARTIALLY_FILLED",
        "FILLED",
        "REJECTED",
        "CANCELLED",
        "UNKNOWN",
    ]
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO runtime_sessions (id, runtime_id, execution_mode, process_role, pid, "
            "started_at) VALUES (1, 'intraday_options', 'live', 'worker', 1, "
            "'2026-07-30T09:15:00Z')"
        )
        for i, status in enumerate(statuses, start=1):
            conn.execute(
                "INSERT INTO order_intents (id, correlation_id, correlation_namespace, "
                "session_id, runtime_id, strategy_id, execution_mode, trading_date, "
                "sequence_number, instrument, security_id, side, quantity, order_type, "
                "product_type, risk_decision, created_at) VALUES "
                "(?, ?, 'live', 1, 'intraday_options', 'st01', 'live', '2026-07-30', ?, "
                "'NIFTY', '1', 'BUY', 50, 'MARKET', 'INTRADAY', 'ALLOWED', "
                "'2026-07-30T09:15:00Z')",
                (i, f"corr_{i:03d}", i),
            )
            conn.execute(
                "INSERT INTO orders (id, intent_id, correlation_id, runtime_id, strategy_id, "
                "execution_mode, status, filled_quantity, updated_at) VALUES "
                "(?, ?, ?, 'intraday_options', 'st01', 'live', ?, 0, '2026-07-30T09:16:00Z')",
                (i, i, f"corr_{i:03d}", status),
            )
        conn.execute(
            "INSERT INTO fills (order_id, correlation_id, runtime_id, strategy_id, "
            "execution_mode, broker_fill_id, quantity, price, filled_at) VALUES "
            "(5, 'corr_005', 'intraday_options', 'st01', 'live', 'bf_001', 50, 100.0, "
            "'2026-07-30T09:16:00Z')"
        )

    before = {
        row["id"]: dict(row)
        for row in database.connect().execute("SELECT * FROM orders").fetchall()
    }

    MigrationRunner(database, versions_dir=VERSIONS_DIR).run_pending(
        require_fresh_backup_for_destructive=True
    )

    assert database.integrity_check() == []
    assert database.foreign_key_check() == []

    with database.connect() as conn:
        after = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM orders").fetchall()}
        assert after == before, "every prior row must survive 0006 unchanged"

        fill_survives = conn.execute(
            "SELECT COUNT(*) AS n FROM fills WHERE order_id = 5"
        ).fetchone()["n"]
        assert fill_survives == 1, "the fills FK to orders(id) must still resolve"

        conn.execute(
            "INSERT INTO order_intents (id, correlation_id, correlation_namespace, "
            "session_id, runtime_id, strategy_id, execution_mode, trading_date, "
            "sequence_number, instrument, security_id, side, quantity, order_type, "
            "product_type, risk_decision, created_at) VALUES "
            "(9, 'corr_009', 'live', 1, 'intraday_options', 'st01', 'live', '2026-07-30', 9, "
            "'NIFTY', '1', 'BUY', 50, 'MARKET', 'INTRADAY', 'ALLOWED', "
            "'2026-07-30T09:15:00Z')"
        )
        conn.execute(
            "INSERT INTO orders (intent_id, correlation_id, runtime_id, strategy_id, "
            "execution_mode, status, filled_quantity, updated_at) VALUES "
            "(9, 'corr_009', 'intraday_options', 'st01', 'live', 'EXPIRED', 0, "
            "'2026-07-30T09:17:00Z')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO orders (intent_id, correlation_id, runtime_id, strategy_id, "
                "execution_mode, status, filled_quantity, updated_at) VALUES "
                "(9, 'corr_bad', 'intraday_options', 'st01', 'live', 'NOT_A_REAL_STATUS', 0, "
                "'2026-07-30T09:18:00Z')"
            )


def test_migration_0006_refuses_without_a_confirmed_fresh_backup_when_orders_has_rows(
    tmp_path: Path,
):
    from common.persistence.migrations import VERSIONS_DIR

    up_to_0005 = tmp_path / "up_to_0005"
    up_to_0005.mkdir()
    for migration in discover_migrations(VERSIONS_DIR):
        if migration.version in {"0006", "0007"}:
            continue
        (up_to_0005 / migration.path.name).write_text(
            migration.path.read_text(encoding="utf-8"), encoding="utf-8"
        )

    database = Database(tmp_path / "operational" / "intraday_options.db")
    MigrationRunner(database, versions_dir=up_to_0005).run_pending()
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO runtime_sessions (id, runtime_id, execution_mode, process_role, "
            "pid, started_at) VALUES (1, 'intraday_options', 'live', 'worker', 1, "
            "'2026-07-30T09:15:00Z')"
        )
        conn.execute(
            "INSERT INTO order_intents (id, correlation_id, correlation_namespace, "
            "session_id, runtime_id, strategy_id, execution_mode, trading_date, "
            "sequence_number, instrument, security_id, side, quantity, order_type, "
            "product_type, risk_decision, created_at) VALUES "
            "(1, 'corr_001', 'live', 1, 'intraday_options', 'st01', 'live', '2026-07-30', 1, "
            "'NIFTY', '1', 'BUY', 50, 'MARKET', 'INTRADAY', 'ALLOWED', "
            "'2026-07-30T09:15:00Z')"
        )
        conn.execute(
            "INSERT INTO orders (intent_id, correlation_id, runtime_id, strategy_id, "
            "execution_mode, status, filled_quantity, updated_at) VALUES "
            "(1, 'corr_001', 'intraday_options', 'st01', 'live', 'PENDING', 0, "
            "'2026-07-30T09:15:00Z')"
        )

    with pytest.raises(MigrationError, match="fresh backup"):
        MigrationRunner(database, versions_dir=VERSIONS_DIR).run_pending()

    # Explicit confirmation lets it proceed.
    applied = MigrationRunner(database, versions_dir=VERSIONS_DIR).run_pending(
        require_fresh_backup_for_destructive=True
    )
    assert "0006" in [m.version for m in applied]


def test_migration_0006_detects_an_interrupted_previous_attempt_and_refuses_to_guess(
    tmp_path: Path,
):
    """Simulates a crash between 0006's DROP and RENAME steps: ``orders_new``
    exists, ``orders`` does not. The runner must not guess whether the copy
    finished — it must stop and ask for manual recovery from backup."""
    from common.persistence.migrations import VERSIONS_DIR

    up_to_0005 = tmp_path / "up_to_0005"
    up_to_0005.mkdir()
    for migration in discover_migrations(VERSIONS_DIR):
        if migration.version in {"0006", "0007"}:
            continue
        (up_to_0005 / migration.path.name).write_text(
            migration.path.read_text(encoding="utf-8"), encoding="utf-8"
        )

    database = Database(tmp_path / "operational" / "intraday_options.db")
    MigrationRunner(database, versions_dir=up_to_0005).run_pending()

    with database.transaction() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("CREATE TABLE orders_new (id INTEGER PRIMARY KEY)")
        conn.execute("DROP TABLE orders")
        conn.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(MigrationError, match="Refusing to guess"):
        MigrationRunner(database, versions_dir=VERSIONS_DIR).run_pending(
            require_fresh_backup_for_destructive=True
        )


# --------------------------------------------------- 0009 (strategy-straddle-920)
def test_migration_0009_upgrades_a_database_created_by_0008_with_real_rows(tmp_path: Path):
    """The real upgrade path for 0009: seed a database through 0001-0008 only
    (the actual prior head at the time this migration was authored — see
    ``0009_multi_leg_baskets.sql``'s own docstring), insert a real
    ``trade_ledger`` row exactly as 0008 already shipped it, then apply 0009
    and prove that row survives completely untouched (0009 adds no columns to
    ``trade_ledger`` at all — see that migration's docstring for why), the two
    new tables exist with a working unique constraint, and a second run does
    not re-apply anything."""
    from common.persistence.migrations import VERSIONS_DIR

    up_to_0008 = tmp_path / "up_to_0008"
    up_to_0008.mkdir()
    for migration in discover_migrations(VERSIONS_DIR):
        if migration.version == "0009":
            continue
        (up_to_0008 / migration.path.name).write_text(
            migration.path.read_text(encoding="utf-8"), encoding="utf-8"
        )

    database = Database(tmp_path / "operational" / "intraday_options.db")
    MigrationRunner(database, versions_dir=up_to_0008).run_pending()

    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO trade_ledger (runtime_id, strategy_id, execution_mode, trading_date, "
            "instrument, security_id, entry_side, quantity, entry_price, exit_price, "
            "gross_pnl, entry_correlation_id, exit_correlation_id, exit_broker_fill_id, "
            "opened_at, closed_at, created_at) VALUES "
            "('intraday_options', 'st01', 'paper', '2026-08-17', 'NIFTY', '1', 'SELL', 750, "
            "100.0, 10.0, 67500.0, 'corr_entry', 'corr_exit', 'bf_001', "
            "'2026-08-17T09:21:00Z', '2026-08-17T10:00:00Z', '2026-08-17T10:00:00Z')"
        )
    before = dict(
        database.connect().execute("SELECT * FROM trade_ledger").fetchone()
    )

    applied = MigrationRunner(database, versions_dir=VERSIONS_DIR).run_pending()
    assert [m.version for m in applied] == ["0009"]

    assert database.integrity_check() == []
    assert database.foreign_key_check() == []

    with database.connect() as conn:
        after = dict(conn.execute("SELECT * FROM trade_ledger").fetchone())
        assert after == before, "0009 must not touch any existing trade_ledger row"

        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"strategy_baskets", "strategy_legs"} <= tables

        conn.execute(
            "INSERT INTO strategy_baskets (runtime_id, strategy_id, execution_mode, "
            "trading_date, basket_id, entries_consumed, adjustment_count, "
            "square_off_state, created_at, updated_at) VALUES "
            "('intraday_options', 'straddle_920', 'paper', '2026-08-17', "
            "'straddle_920:2026-08-17', 1, 0, 'PENDING', 'now', 'now')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO strategy_baskets (runtime_id, strategy_id, execution_mode, "
                "trading_date, basket_id, entries_consumed, adjustment_count, "
                "square_off_state, created_at, updated_at) VALUES "
                "('intraday_options', 'straddle_920', 'paper', '2026-08-17', "
                "'straddle_920:2026-08-17', 1, 0, 'PENDING', 'now', 'now')"
            )

    # A second startup must not attempt to reapply 0009.
    second_run = MigrationRunner(database, versions_dir=VERSIONS_DIR).run_pending()
    assert second_run == []
