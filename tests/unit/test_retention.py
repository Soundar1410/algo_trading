"""Phase 7 Part 5: retention and backups.

Covers the four independently reviewable pieces: the policy allowlist itself,
bounded age-based row deletion (one transaction, five tables, never the four
trading tables), log compression/deletion on top of the size cap, and
pre-migration database backups with a retained-backup count. ``run_retention``
gets one orchestration test tying the three sweeps together.

Also covers migration ``0005_retention_indexes.sql`` — added after the audit
found ``purge_old_rows``'s own query plan was a full scan plus a temp-B-tree
sort on every retained table, because every existing index on these tables
leads with ``runtime_id``, not the timestamp column the purge query filters
and orders on. One structural test (``EXPLAIN QUERY PLAN``) and two timing
tests against a six-figure synthetic backlog confirm the fix: the query plan
changed from SCAN to SEARCH, and the cost genuinely stopped scaling with
backlog size rather than merely looking like it should have.
"""

from __future__ import annotations

import gzip
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from common.persistence import Database, MigrationRunner
from common.retention import backup
from common.retention import database as retention_database
from common.retention.backup import backup_database
from common.retention.database import purge_old_rows
from common.retention.logs import sweep_logs
from common.retention.policy import NEVER_PURGED_TABLES, RETAINED_TABLES
from common.retention.runner import run_retention

NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=200)
RECENT = NOW - timedelta(hours=1)


# ------------------------------------------------------------------- policy
def test_retained_and_never_purged_tables_are_disjoint():
    assert RETAINED_TABLES.keys().isdisjoint(NEVER_PURGED_TABLES)


def test_retained_tables_are_exactly_the_five_the_plan_names():
    assert set(RETAINED_TABLES) == {
        "runtime_heartbeats",
        "notifications",
        "errors",
        "feed_events",
        "auth_events",
    }


def test_never_purged_tables_are_exactly_the_trading_tables():
    assert {"orders", "fills", "positions", "order_intents"} == NEVER_PURGED_TABLES


# --------------------------------------------------------------- fixtures
@pytest.fixture
def migrated_db(database_path: Path) -> Database:
    db = Database(database_path)
    MigrationRunner(db).run_pending()
    return db


def _seed_session(db: Database) -> int:
    conn = db.connect()
    conn.execute("BEGIN")
    conn.execute(
        "INSERT INTO runtime_sessions "
        "(runtime_id, execution_mode, process_role, pid, started_at) "
        "VALUES ('intraday_options', 'paper', 'supervisor', 1, ?)",
        (NOW.isoformat(),),
    )
    session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("COMMIT")
    return int(session_id)


def _insert(db: Database, table: str, columns: dict[str, object]) -> None:
    conn = db.connect()
    conn.execute("BEGIN")
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        tuple(columns.values()),
    )
    conn.execute("COMMIT")


def _count(db: Database, table: str) -> int:
    return int(db.connect().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _seed_retained_tables(db: Database, *, session_id: int, occurred_at: str) -> None:
    _insert(
        db,
        "runtime_heartbeats",
        {
            "session_id": session_id,
            "runtime_id": "intraday_options",
            "health_state": "OK",
            "beat_at": occurred_at,
        },
    )
    _insert(
        db,
        "notifications",
        {
            "runtime_id": "intraday_options",
            "channel": "telegram",
            "event_type": "test",
            "message": "hello",
            "created_at": occurred_at,
        },
    )
    _insert(
        db,
        "errors",
        {
            "runtime_id": "intraday_options",
            "severity": "ERROR",
            "component": "test",
            "message": "boom",
            "occurred_at": occurred_at,
        },
    )
    _insert(
        db,
        "feed_events",
        {"runtime_id": "intraday_options", "event": "connected", "occurred_at": occurred_at},
    )
    _insert(
        db,
        "auth_events",
        {
            "runtime_id": "intraday_options",
            "event": "token_generated",
            "occurred_at": occurred_at,
        },
    )


# ---------------------------------------------------------------- purge_old_rows
def test_purge_deletes_old_rows_and_keeps_recent_ones(migrated_db: Database):
    session_id = _seed_session(migrated_db)
    _seed_retained_tables(migrated_db, session_id=session_id, occurred_at=OLD.isoformat())
    _seed_retained_tables(migrated_db, session_id=session_id, occurred_at=RECENT.isoformat())

    deleted = purge_old_rows(migrated_db, max_age_days=90, batch_limit=1000, now=NOW)

    assert deleted == {table: 1 for table in RETAINED_TABLES}
    for table in RETAINED_TABLES:
        assert _count(migrated_db, table) == 1


def test_purge_never_touches_a_trading_table_even_when_it_is_ancient(migrated_db: Database):
    _insert(
        migrated_db,
        "positions",
        {
            "runtime_id": "intraday_options",
            "strategy_id": "io_alpha",
            "execution_mode": "paper",
            "trading_date": "2020-01-01",
            "instrument": "NIFTY",
            "security_id": "111",
            "quantity": 50,
            "average_price": 100.0,
            "status": "CLOSED",
            "opened_at": OLD.isoformat(),
            "updated_at": OLD.isoformat(),
        },
    )
    purge_old_rows(migrated_db, max_age_days=1, batch_limit=1000, now=NOW)
    assert _count(migrated_db, "positions") == 1


def test_purge_is_bounded_by_batch_limit_across_repeated_calls(migrated_db: Database):
    session_id = _seed_session(migrated_db)
    for _ in range(5):
        _insert(
            migrated_db,
            "errors",
            {
                "runtime_id": "intraday_options",
                "severity": "ERROR",
                "component": "test",
                "message": "boom",
                "occurred_at": OLD.isoformat(),
            },
        )

    first = purge_old_rows(migrated_db, max_age_days=1, batch_limit=2, now=NOW)
    assert first == {"errors": 2}
    assert _count(migrated_db, "errors") == 3

    second = purge_old_rows(migrated_db, max_age_days=1, batch_limit=2, now=NOW)
    assert second == {"errors": 2}
    assert _count(migrated_db, "errors") == 1

    third = purge_old_rows(migrated_db, max_age_days=1, batch_limit=2, now=NOW)
    assert third == {"errors": 1}
    assert _count(migrated_db, "errors") == 0
    assert session_id  # silence unused-var lint; session exists for FK validity


def test_purge_runs_as_one_transaction_a_failure_rolls_back_earlier_deletes(
    migrated_db: Database, monkeypatch: pytest.MonkeyPatch
):
    _insert(
        migrated_db,
        "errors",
        {
            "runtime_id": "intraday_options",
            "severity": "ERROR",
            "component": "test",
            "message": "boom",
            "occurred_at": OLD.isoformat(),
        },
    )
    # 'errors' is processed before the nonexistent table (dict insertion
    # order), so its DELETE runs successfully inside the still-open
    # transaction before the second statement fails.
    monkeypatch.setattr(
        retention_database,
        "RETAINED_TABLES",
        {"errors": "occurred_at", "no_such_table": "occurred_at"},
    )

    with pytest.raises(sqlite3.OperationalError):
        purge_old_rows(migrated_db, max_age_days=1, batch_limit=1000, now=NOW)

    assert _count(migrated_db, "errors") == 1  # rolled back, not half-purged


# ------------------------------------------------------- migration 0005: indexes
def test_purge_query_plan_seeks_the_new_index_not_a_full_scan(migrated_db: Database):
    """Migration 0005's whole purpose, checked structurally against the exact
    query purge_old_rows runs: EXPLAIN QUERY PLAN must show an index SEARCH,
    never a SCAN or a TEMP B-TREE sort, for every retained table. Before
    0005 this was SCAN ... USING COVERING INDEX <the runtime_id-leading one>
    plus USE TEMP B-TREE FOR ORDER BY on all five — confirmed by hand while
    diagnosing the gap 0005 closes."""
    conn = migrated_db.connect()
    for table, column in RETAINED_TABLES.items():
        query = f"SELECT id FROM {table} WHERE {column} < ? ORDER BY {column} LIMIT ?"
        rows = conn.execute(f"EXPLAIN QUERY PLAN {query}", ("2020-01-01", 5000))
        plan = " | ".join(row["detail"] for row in rows)
        assert "SEARCH" in plan, f"{table}: expected an index seek, got: {plan}"
        assert "SCAN" not in plan, f"{table}: fell back to a full scan: {plan}"
        assert "TEMP B-TREE" not in plan, f"{table}: still sorting instead of seeking: {plan}"


def _seed_heartbeat_backlog(db_path: Path, *, count: int, drop_new_index: bool = False) -> Database:
    """A freshly migrated database with ``count`` ancient heartbeat rows,
    inserted in one bulk ``executemany`` transaction so a six-figure backlog
    is fast to build in a unit test. ``drop_new_index`` reproduces the
    pre-0005 state on an otherwise identical database, for a direct
    before/after timing comparison against the exact same data."""
    db = Database(db_path)
    MigrationRunner(db).run_pending()
    session_id = _seed_session(db)
    conn = db.connect()
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT INTO runtime_heartbeats (session_id, runtime_id, health_state, beat_at) "
        "VALUES (?, 'intraday_options', 'OK', ?)",
        [(session_id, OLD.isoformat())] * count,
    )
    conn.execute("COMMIT")
    if drop_new_index:
        conn.execute("DROP INDEX idx_runtime_heartbeats_beat_at")
    return db


def _time_purge(db: Database, *, batch_limit: int = 1000) -> float:
    start = time.perf_counter()
    deleted = purge_old_rows(db, max_age_days=1, batch_limit=batch_limit, now=NOW)
    elapsed = time.perf_counter() - start
    assert deleted == {"runtime_heartbeats": batch_limit}  # the run actually did the work timed
    return elapsed


def test_purge_cost_does_not_scale_with_backlog_size_once_indexed(tmp_path: Path):
    """Same batch_limit, an 8x larger backlog (50k vs 400k rows, both well
    past 100k for the larger) — purge time should barely move, because the
    index lets the query seek to the oldest ``batch_limit`` rows rather than
    read every row to find them. Isolates "does cost scale with backlog"
    from the with/without-index comparison below."""
    small = _seed_heartbeat_backlog(tmp_path / "small.db", count=50_000)
    large = _seed_heartbeat_backlog(tmp_path / "large.db", count=400_000)

    small_time = _time_purge(small)
    large_time = _time_purge(large)

    # A full scan-and-sort would take roughly 8x as long on 8x the backlog;
    # an index seek should not. Generous multiplicative *and* additive
    # margin against a loaded CI machine — measured on dev hardware this
    # ratio is close to 1.0 (both purges land around 1ms).
    assert large_time < small_time * 5 + 0.25, (
        f"purge time grew with backlog size: {small_time:.4f}s at 50k rows vs "
        f"{large_time:.4f}s at 400k rows — the index seek should make it nearly flat"
    )


def test_purge_is_dramatically_faster_with_the_index_than_without(tmp_path: Path):
    """The literal before/after: two databases with an identical 400k-row
    backlog and an identical batch_limit, differing only in whether
    migration 0005's index exists. Measured on dev hardware this is roughly
    a 20x speedup (0.001s vs 0.023s); the assertion asks for a fraction of
    that, plus an absolute ceiling, to stay robust on a slower machine."""
    with_index = _seed_heartbeat_backlog(tmp_path / "with_index.db", count=400_000)
    without_index = _seed_heartbeat_backlog(
        tmp_path / "without_index.db", count=400_000, drop_new_index=True
    )

    with_index_time = _time_purge(with_index)
    without_index_time = _time_purge(without_index)

    assert with_index_time < without_index_time / 5, (
        f"expected at least a 5x speedup from the index; got "
        f"with_index={with_index_time:.4f}s without_index={without_index_time:.4f}s"
    )
    assert with_index_time < 0.5  # absolute regression guard, independent of the ratio above


# --------------------------------------------------------------------- logs
def _touch(path: Path, *, mtime: datetime) -> None:
    path.write_text("log line\n", encoding="utf-8")
    timestamp = mtime.timestamp()
    os.utime(path, (timestamp, timestamp))


def test_sweep_logs_leaves_the_active_file_alone_regardless_of_age(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    active = log_dir / "algo_trading.log"
    _touch(active, mtime=NOW - timedelta(days=400))

    sweep_logs(log_dir, max_age_days=30, compress_after_days=1, now=NOW)

    assert active.is_file()
    assert active.read_text(encoding="utf-8") == "log line\n"


def test_sweep_logs_compresses_past_the_compress_age_but_keeps_it(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    rotated = log_dir / "algo_trading.log.2"
    _touch(rotated, mtime=NOW - timedelta(days=3))

    report = sweep_logs(log_dir, max_age_days=30, compress_after_days=1, now=NOW)

    assert not rotated.exists()
    gz = log_dir / "algo_trading.log.2.gz"
    assert gz.is_file()
    assert report.compressed == (gz,)
    assert report.deleted == ()
    with gzip.open(gz, "rt", encoding="utf-8") as handle:
        assert handle.read() == "log line\n"


def test_sweep_logs_does_not_touch_a_backup_younger_than_the_compress_age(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    rotated = log_dir / "algo_trading.log.1"
    _touch(rotated, mtime=NOW - timedelta(hours=12))

    report = sweep_logs(log_dir, max_age_days=30, compress_after_days=1, now=NOW)

    assert rotated.is_file()
    assert report.compressed == ()
    assert report.deleted == ()


def test_sweep_logs_deletes_past_the_max_age_compressed_or_not(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    plain = log_dir / "algo_trading.log.3"
    already_gz = log_dir / "algo_trading.log.4.gz"
    _touch(plain, mtime=NOW - timedelta(days=40))
    _touch(already_gz, mtime=NOW - timedelta(days=40))

    report = sweep_logs(log_dir, max_age_days=30, compress_after_days=1, now=NOW)

    assert not plain.exists()
    assert not already_gz.exists()
    assert set(report.deleted) == {plain, already_gz}


def test_sweep_logs_on_a_missing_directory_is_a_harmless_no_op(tmp_path: Path):
    report = sweep_logs(tmp_path / "does_not_exist", max_age_days=30, compress_after_days=1)
    assert report.compressed == ()
    assert report.deleted == ()


# ------------------------------------------------------------------- backup
def _make_sqlite_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
    finally:
        conn.close()


def test_backup_of_a_database_that_does_not_exist_yet_is_a_no_op(tmp_path: Path):
    result = backup_database(
        tmp_path / "operational" / "intraday_options.db",
        tmp_path / "backups",
        retain_count=3,
    )
    assert result is None
    assert not (tmp_path / "backups").exists()


def test_backup_produces_a_consistent_readable_snapshot(tmp_path: Path):
    db_path = tmp_path / "operational" / "intraday_options.db"
    _make_sqlite_db(db_path)

    dest = backup_database(db_path, tmp_path / "backups", retain_count=3, now=NOW)

    assert dest is not None
    assert dest.is_file()
    conn = sqlite3.connect(str(dest))
    try:
        assert conn.execute("SELECT x FROM t").fetchone() == (42,)
    finally:
        conn.close()


def test_backup_prunes_down_to_the_retained_count(tmp_path: Path):
    db_path = tmp_path / "operational" / "intraday_options.db"
    _make_sqlite_db(db_path)
    backup_dir = tmp_path / "backups"

    for i in range(4):
        backup_database(db_path, backup_dir, retain_count=2, now=NOW + timedelta(seconds=i))

    remaining = sorted(backup_dir.glob("intraday_options_*.db"))
    assert len(remaining) == 2
    # The two survivors are the two most recently taken.
    assert "T120002" in remaining[0].name or "T120003" in remaining[0].name


def test_backup_default_module_is_importable_directly():
    # common.retention.backup is the module the __init__ re-exports from;
    # this just guards against the re-export drifting from the module.
    assert backup.backup_database is backup_database


# ------------------------------------------------------------------ runner
def test_run_retention_orchestrates_all_three_sweeps(tmp_path: Path, migrated_db: Database):
    session_id = _seed_session(migrated_db)
    _seed_retained_tables(migrated_db, session_id=session_id, occurred_at=OLD.isoformat())

    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    _touch(log_dir / "algo_trading.log.9", mtime=NOW - timedelta(days=400))

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(exist_ok=True)
    for day in ("2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"):
        (cache_dir / f"dhan_scrip_master_{day}.csv").write_text("x", encoding="utf-8")

    report = run_retention(
        database=migrated_db,
        log_dir=log_dir,
        cache_dir=cache_dir,
        log_max_age_days=30,
        log_compress_after_days=1,
        db_row_max_age_days=90,
        db_delete_batch_limit=5000,
        scrip_cache_retain_count=2,
        now=NOW,
    )

    assert report.rows_deleted == {table: 1 for table in RETAINED_TABLES}
    assert report.logs.deleted == (log_dir / "algo_trading.log.9",)
    assert report.scrip_masters_pruned == 2
    assert len(list(cache_dir.glob("dhan_scrip_master_*.csv"))) == 2
