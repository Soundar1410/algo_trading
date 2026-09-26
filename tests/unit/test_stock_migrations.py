"""Phase 4a: ``positional_stocks.db`` has its own migration set (spec 9, v1.2i).

The two paper runtimes run from this working tree and apply — and checksum —
``common/persistence/migrations/versions/`` at every start. A stock migration
there would reach their live databases the next morning, and editing or
removing it afterwards would stop both runtimes. So these tests prove the
separation from both sides:

* the shared versions directory is byte-identical to ``feature-paper-auto-start``;
* the shared runner applies no stock migration to a database;
* the stock runner applies only the stock set, and nothing else.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from common.persistence import Database, MigrationRunner
from common.persistence.migrations import VERSIONS_DIR, discover_migrations
from runtimes.positional_stocks.database import STOCK_VERSIONS_DIR, open_stock_database

REPO = Path(__file__).resolve().parents[2]
BASE_BRANCH = "feature-paper-auto-start"
SHARED_RELATIVE = VERSIONS_DIR.relative_to(REPO).as_posix()

STOCK_TABLES = {
    "stock_weekly_runs",
    "stock_positions",
    "stock_pending_orders",
    "stock_fills",
    "stock_equity",
    "stock_cooling_off",
    "stock_universe_seen",
    "stock_signals",
    "stock_position_reviews",
    "stock_corporate_actions",
}


def _tables(database: Database) -> set[str]:
    rows = database.connect().execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row["name"] for row in rows}


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, check=False)


def test_the_shared_versions_directory_is_byte_identical_to_the_base_branch() -> None:
    if _git("rev-parse", "--verify", "--quiet", BASE_BRANCH).returncode != 0:
        pytest.skip(f"{BASE_BRANCH} is not available in this checkout")
    listed = _git("ls-tree", "--name-only", BASE_BRANCH, f"{SHARED_RELATIVE}/")
    assert listed.returncode == 0
    base_files = {Path(line).name for line in listed.stdout.decode().splitlines()}
    here_files = {p.name for p in VERSIONS_DIR.iterdir() if p.is_file()}
    assert here_files == base_files
    for name in sorted(base_files):
        shown = _git("show", f"{BASE_BRANCH}:{SHARED_RELATIVE}/{name}")
        assert shown.returncode == 0
        assert (VERSIONS_DIR / name).read_bytes() == shown.stdout, name


def test_no_stock_migration_is_in_the_shared_directory() -> None:
    for migration in discover_migrations(VERSIONS_DIR):
        assert "stock" not in migration.name
        assert "stock_" not in migration.path.read_text(encoding="utf-8")


def test_the_shared_runner_applies_no_stock_migration(tmp_path: Path) -> None:
    database = Database(tmp_path / "intraday_options.db")
    applied = MigrationRunner(database).run_pending()
    assert [m.path.parent for m in applied] == [VERSIONS_DIR] * len(applied)
    assert not any(name.startswith("stock_") for name in _tables(database))


def test_the_stock_runner_applies_only_the_stock_set(tmp_path: Path) -> None:
    database = open_stock_database(tmp_path / "positional_stocks.db")
    applied = MigrationRunner(database, versions_dir=STOCK_VERSIONS_DIR).applied_versions()
    assert applied == {m.version for m in discover_migrations(STOCK_VERSIONS_DIR)}
    tables = _tables(database) - {"schema_migrations", "sqlite_sequence"}
    assert tables == STOCK_TABLES
    # None of the options-shaped tables the shared set would have created.
    assert not {"positions", "fills", "signals", "orders"} & _tables(database)


def test_the_stock_migrations_are_replay_safe(tmp_path: Path) -> None:
    path = tmp_path / "positional_stocks.db"
    open_stock_database(path).close()
    database = open_stock_database(path)
    assert MigrationRunner(database, versions_dir=STOCK_VERSIONS_DIR).pending() == []
    assert database.foreign_key_check() == []
    # The runner's own rules (IF NOT EXISTS, nothing destructive) were applied
    # to these files on open; a replay of the raw script is also a no-op.
    for migration in discover_migrations(STOCK_VERSIONS_DIR):
        database.connect().executescript(migration.path.read_text(encoding="utf-8"))
    assert _tables(database) - {"schema_migrations", "sqlite_sequence"} == STOCK_TABLES


def test_every_stock_table_is_prefixed_and_carries_strategy_id(tmp_path: Path) -> None:
    database = open_stock_database(tmp_path / "positional_stocks.db")
    for table in STOCK_TABLES:
        assert table.startswith("stock_")
        columns = {row["name"] for row in database.connect().execute(f"PRAGMA table_info({table})")}
        assert "strategy_id" in columns, table
