"""Migration runner: schema_migrations, idempotency, replay safety, integrity."""

from __future__ import annotations

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
    migration = Migration.from_path(tmp_path / "0007_add_positions.sql")
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
    assert [m.version for m in shipped] == ["0001", "0002", "0003"]
    assert shipped[0].name == "walking_skeleton"
    assert shipped[1].name == "feed_and_auth_health"
    assert shipped[2].name == "paper_fill_realism"


def test_shipped_migrations_apply_to_a_fresh_database(tmp_path: Path):
    """The real migrations must survive the runner's own replay-safety rules."""
    from common.persistence.migrations import VERSIONS_DIR

    database = Database(tmp_path / "operational" / "intraday_options.db")
    applied = MigrationRunner(database, versions_dir=VERSIONS_DIR).run_pending()

    assert [m.version for m in applied] == ["0001", "0002", "0003"]
    assert database.integrity_check() == []
    assert database.foreign_key_check() == []


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

    assert [m.version for m in applied] == ["0002", "0003"]
    with database.connect() as conn:
        survivors = conn.execute("SELECT COUNT(*) FROM runtime_sessions").fetchone()[0]
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert survivors == 1, "a later migration must not disturb existing rows"
    assert {"auth_events", "feed_events", "option_chain_snapshots"} <= tables
    assert "paper_fill_quotes" in tables
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
        for table in ("auth_events", "feed_events", "option_chain_snapshots"):
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
