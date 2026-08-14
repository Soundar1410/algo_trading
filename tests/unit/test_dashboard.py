"""Dashboard safety suite: import boundaries and read-only enforcement.

Every page-specific behaviour test moved out to its own file
(``test_dashboard_home.py``, ``test_dashboard_intraday_options_*.py``,
``test_dashboard_system_health.py``, ``test_dashboard_positional_and_stocks.py``,
``test_dashboard_read_models.py``, ``test_dashboard_account_wide.py``,
``test_dashboard_reconciliation.py``) as the page set grew from three thin
pages to a full tabbed information architecture. What stays here is the
cross-cutting guarantee every one of those pages depends on: no dashboard
module — anywhere under ``dashboards/``, not just the five page entry
points — imports a broker, a feed, a write-capable ``Database``, or
``streamlit`` at module level, and every read genuinely goes through a
read-only connection.

``grep -rni streamlit tests/`` returned zero hits before this file (Phase 7
audit finding); this is the file that closes it, and stays the file that
does for every page added since.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from common.persistence import Database, MigrationRunner, connect_readonly
from dashboards._shared import SnapshotUnavailable, load_snapshot

RUNTIME_ID = "intraday_options"
TRADING_DATE = "2026-08-07"

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS_DIR = REPO_ROOT / "dashboards"
#: Recursive — the read-model layer under ``dashboards/data/`` is just as
#: subject to these rules as the five page entry points are.
PAGE_MODULE_FILES = sorted(
    p
    for p in DASHBOARDS_DIR.rglob("*.py")
    if p.name != "__init__.py" and "__pycache__" not in p.parts
)


# =============================================== regression: safe imports
def _imported_module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize(
    "path", PAGE_MODULE_FILES, ids=lambda p: str(p.relative_to(DASHBOARDS_DIR))
)
def test_no_dashboard_module_imports_a_broker_or_a_feed(path: Path):
    """A broker or feed import here is exactly the side-effecting import
    the spec forbids — a second broker/feed connection from the dashboard
    would compete with the supervisor's own."""
    imported = _imported_module_names(path)
    lowered = {name: name.lower() for name in imported}
    offenders = {
        name
        for name, lower in lowered.items()
        if "broker" in lower or ".feed" in lower or "market_data" in lower
    }
    assert offenders == set(), f"{path.name} imports {offenders}"


@pytest.mark.parametrize(
    "path", PAGE_MODULE_FILES, ids=lambda p: str(p.relative_to(DASHBOARDS_DIR))
)
def test_no_dashboard_module_imports_streamlit_at_module_level(path: Path):
    """Streamlit must be importable lazily only, inside main() — this is
    what lets every test in this package run without streamlit at
    collection time. ``pages/*.py`` shims are the one exception: Streamlit
    itself only ever executes them at app-run time, never at test-collection
    time, so a top-level ``main()`` call there is the documented shape, not
    a violation.
    """
    if path.parent.name == "pages":
        pytest.skip("pages/*.py shims call main() unconditionally by design")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert not any(alias.name == "streamlit" for alias in node.names), path.name
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "streamlit", path.name


def _imported_names_from(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize(
    "path",
    [p for p in PAGE_MODULE_FILES if p.parent.name != "pages"],
    ids=lambda p: str(p.relative_to(DASHBOARDS_DIR)),
)
def test_no_dashboard_module_imports_the_write_capable_database_class(path: Path):
    """``common.persistence.Database`` opens a write connection —
    ``connect_readonly``/``run_bounded`` is the only entry point every page
    may use. ``_shared.py`` itself is exempt: it imports ``DatabaseError``
    (a plain exception class, not a connection) from the same module, which
    this check must not confuse with importing ``Database`` itself."""
    imported = _imported_names_from(path, "common.persistence")
    assert "Database" not in imported, f"{path.name} imports the write-capable Database class"


@pytest.mark.parametrize(
    "path",
    [p for p in PAGE_MODULE_FILES if p.parent.name != "pages"],
    ids=lambda p: str(p.relative_to(DASHBOARDS_DIR)),
)
def test_no_dashboard_module_imports_subprocess_or_os_system(path: Path):
    """No dashboard module may shell out — there is no legitimate reason
    for a read-only page to start a process, and it rules out an
    accidental runtime-control path (start/stop/square-off)."""
    imported = _imported_module_names(path)
    assert "subprocess" not in imported, f"{path.name} imports subprocess"


def test_the_dashboards_directory_is_what_we_think_it_is():
    """Guards the parametrisation above: an empty/short glob would pass
    everything trivially, the same guard test_scripts_are_read_only.py uses
    for scripts/."""
    names = {str(p.relative_to(DASHBOARDS_DIR)) for p in PAGE_MODULE_FILES}
    assert names == {
        "_shared.py",
        "app.py",
        "formatting.py",
        "intraday_options.py",
        "intraday_stocks.py",
        "positional_options.py",
        "system_health.py",
        "data/account.py",
        "data/calendar_stats.py",
        "data/incidents.py",
        "data/intraday_options.py",
        "data/positional.py",
        "data/stocks.py",
        "pages/1_Intraday_Options.py",
        "pages/2_Positional_Options.py",
        "pages/3_Intraday_Stocks.py",
        "pages/4_System_Health.py",
    }


# ======================================================== robustness (_shared)
def test_a_missing_database_returns_a_clear_reason_not_an_exception(database_path: Path):
    result = load_snapshot(database_path, RUNTIME_ID, TRADING_DATE)
    assert isinstance(result, SnapshotUnavailable)
    assert "No database yet" in result.reason


def test_a_pre_migration_database_returns_a_clear_reason_not_an_exception(database_path: Path):
    """The file exists, opens fine, but no migration has ever run — every
    table read_snapshot queries is missing."""
    Database(database_path).connect()  # creates an empty, valid, table-less file
    assert database_path.is_file()

    result = load_snapshot(database_path, RUNTIME_ID, TRADING_DATE)

    assert isinstance(result, SnapshotUnavailable)
    assert "Database not ready" in result.reason


def test_a_locked_database_returns_a_clear_reason_not_an_exception(
    database_path: Path, monkeypatch
):
    """Simulated rather than genuinely raced: a real lock needs a second
    connection to hold a write transaction past the busy timeout, which
    would make this test slow and timing-dependent for no extra safety
    proven. The behaviour under test is entirely in the except branch,
    which does not care *why* sqlite raised — only that it does not
    propagate."""
    Database(database_path).connect()

    def _raise(*_args: object, **_kwargs: object):
        raise sqlite3.OperationalError("database is locked")

    import dashboards._shared as shared_module

    monkeypatch.setattr(shared_module, "read_snapshot", _raise)

    result = load_snapshot(database_path, RUNTIME_ID, TRADING_DATE)

    assert isinstance(result, SnapshotUnavailable)
    assert "locked" in result.reason


def test_load_snapshot_never_opens_a_write_connection(database_path: Path):
    """Belt and braces on top of the AST-import check above: even a
    successful read must go through connect_readonly, not Database."""
    database = Database(database_path)
    MigrationRunner(database).run_pending()

    result = load_snapshot(database_path, RUNTIME_ID, TRADING_DATE)

    assert not isinstance(result, SnapshotUnavailable)
    # The write connection above is still the only one that created
    # anything; prove the read path used a genuinely read-only one by
    # confirming a second read-only open still refuses a write.
    conn = connect_readonly(database_path)
    with pytest.raises(sqlite3.OperationalError, match="readonly database"):
        conn.execute("DELETE FROM runtime_sessions")
    conn.close()


def test_run_bounded_never_opens_a_write_connection(database_path: Path):
    """Same guarantee for the newer, generalised helper every read-model
    query in ``dashboards/data/`` goes through."""
    from dashboards._shared import run_bounded

    database = Database(database_path)
    MigrationRunner(database).run_pending()

    def _select_one(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COUNT(*) AS c FROM runtime_sessions").fetchone()
        return int(row["c"])

    result = run_bounded(database_path, _select_one)
    assert result == 0


def test_run_bounded_reports_a_missing_database_without_raising(tmp_path: Path):
    from dashboards._shared import run_bounded

    result = run_bounded(tmp_path / "missing.db", lambda conn: conn.execute("SELECT 1"))
    assert isinstance(result, SnapshotUnavailable)
    assert "No database yet" in result.reason
