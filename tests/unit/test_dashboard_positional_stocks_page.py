"""Phase 6 (spec v1.3.2 11.1): the Positional Stocks page, end to end.

* the real Streamlit runtime (``AppTest``) on a book the real decide run wrote,
  on no book at all ("not started yet"), on a busy book and a corrupt journal;
* **read-only**: every file under the project is byte-identical after a page
  load, with writes, sockets and subprocesses made to raise while it renders;
* **never loads network code**, in a fresh interpreter;
* **never disturbs a run**: a decide run with a dashboard read open completes
  exactly as one without, and every read taken meanwhile is the old state, the
  new state or "book busy" — never an exception.
"""

from __future__ import annotations

import builtins
import functools
import hashlib
import os
import socket
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from _stock_run_fixtures import REPO, Root
from streamlit.testing.v1 import AppTest
from test_dashboard_positional_stocks_data import build_book, view_of

from common.persistence import connect_readonly
from dashboards import _shared
from dashboards.data import positional_stocks as ps
from dashboards.positional_stocks import TABS

PAGE = REPO / "dashboards" / "positional_stocks.py"


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``~/Library/LaunchAgents`` of a throwaway home, never the real one."""
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def _app(root: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    monkeypatch.setenv("PROJECT_ROOT", str(root))
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.run()
    return at


def _charts(block: object) -> list[object]:
    """Line charts: AppTest exposes them only as ``vega_lite_chart`` elements."""
    return [
        c
        for c in block.children.values()  # type: ignore[attr-defined]
        if getattr(c, "type", None) == "vega_lite_chart"
    ]


def _texts(block: object) -> list[str]:
    out: list[str] = []
    for kind in ("markdown", "info", "warning", "error", "caption"):
        out += [str(e.value) for e in getattr(block, kind)]
    return out


@pytest.fixture
def book(tmp_path: Path) -> Root:
    return build_book(tmp_path / "project")


# ================================================================ AppTest
def test_the_page_renders_the_book(
    book: Root, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = _app(book.path, monkeypatch)
    assert list(at.exception) == []
    assert [t.label for t in at.tabs] == list(TABS)
    overview, positions, trades, equity, report, health = at.tabs
    assert {m.label for m in overview.metric} >= {
        "Regime",
        "Equity",
        "Cash",
        "Peak",
        "Drawdown",
        "Brake 1",
        "Brake 2",
        "Open positions",
        "Committed (held positions)",
        "Runtime enabled",
        "Strategy enabled",
    }
    assert any(text.startswith("#### Operator actions: ") for text in _texts(overview))
    assert len(positions.dataframe) == 2  # open positions, pending orders
    assert {m.label for m in trades.metric} >= {"Trades", "Win rate", "Net P&L"}
    assert len(_charts(equity)) == 2
    assert any(f"{book.last_session(4)}.md" in t for t in _texts(report))
    assert len(health.dataframe) == 1
    assert not any("Not started yet" in t for tab in at.tabs for t in _texts(tab))


def test_before_go_live_every_tab_says_not_started(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Root.create(tmp_path / "project")
    at = _app(root.path, monkeypatch)
    assert list(at.exception) == []
    assert [t.label for t in at.tabs] == list(TABS)
    for tab in at.tabs:
        infos = [str(e.value) for e in tab.info]
        assert any("Not started yet" in t and "go-live checklist" in t for t in infos), tab.label
        # Never a table that could read as "no trades": only Overview's
        # LaunchAgent table, which is about scheduling, not the book.
        expected = 1 if tab.label == "Overview" else 0
        assert len(tab.dataframe) == expected, tab.label
        assert not _charts(tab)


def test_a_busy_book_says_so(
    book: Root, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    conn = sqlite3.connect(book.db, isolation_level=None)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("BEGIN EXCLUSIVE")
    monkeypatch.setattr(
        _shared, "connect_readonly", functools.partial(connect_readonly, busy_timeout_ms=100)
    )
    try:
        at = _app(book.path, monkeypatch)
    finally:
        conn.execute("ROLLBACK")
        conn.close()
    assert list(at.exception) == []
    for label in ("Overview", "Positions & orders", "Equity", "Health"):
        tab = next(t for t in at.tabs if t.label == label)
        assert any(str(w.value) == ps.BUSY for w in tab.warning), label
    # journal.csv is a file: it still renders on its own.
    trades = next(t for t in at.tabs if t.label == "Trades & performance")
    assert {m.label for m in trades.metric} >= {"Trades"}


def test_a_corrupt_journal_is_a_message(
    book: Root, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (book.reports / "journal.csv").write_bytes(b"\xff\xfe garbage")
    at = _app(book.path, monkeypatch)
    assert list(at.exception) == []
    trades = next(t for t in at.tabs if t.label == "Trades & performance")
    assert any(str(e.value).startswith("journal.csv could not be read") for e in trades.error)


def test_an_unreadable_report_section_is_never_a_zero_on_the_page(
    book: Root, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = book.reports / f"{book.last_session(4)}.md"
    report.write_text(report.read_text().replace("## 8. Corporate actions", "## 8. Corp. acts"))
    at = _app(book.path, monkeypatch)
    assert list(at.exception) == []
    overview = at.tabs[0]
    assert "#### Operator actions: could not be counted — see below" in _texts(overview)
    assert any(
        f"could not read silent freeze lifts from {report.name}" in str(w.value)
        for w in overview.warning
    )


# ============================================================== read-only
def _snapshot(root: Path) -> dict[str, tuple[str, int]]:
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mtime_ns,
            )
    return out


def test_the_page_writes_nothing(
    book: Root, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every file under the project is byte- and mtime-identical after a page
    load. While the page renders, any write-mode open, socket connection,
    subprocess or os.system raises — and the page still renders cleanly.
    SQLite's own sidecars are the one exception, and only as an empty
    ``-wal`` plus the ``-shm`` index (see below)."""
    before = _snapshot(book.path)
    real_open = builtins.open
    forbidden: list[str] = []

    def guarded_open(file: object, mode: str = "r", *args: object, **kwargs: object) -> object:
        if any(flag in mode for flag in "wax+") and str(book.path) in str(file):
            forbidden.append(f"open({file!r}, {mode!r})")
            raise PermissionError("the dashboard must not write")
        return real_open(file, mode, *args, **kwargs)  # type: ignore[call-overload]

    def refuse(*args: object, **kwargs: object) -> None:
        forbidden.append(f"refused call {args[:1]!r}")
        raise PermissionError("the dashboard must not do this")

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    at = _app(book.path, monkeypatch)
    monkeypatch.undo()

    assert list(at.exception) == []
    assert forbidden == []
    after = _snapshot(book.path)
    # SQLite's own sidecars: a read-only connection to a WAL database whose
    # sidecars were removed at the writer's clean close re-creates them — an
    # EMPTY -wal (no row, nothing to replay) and the -shm index. Measured, not
    # assumed; the existing pages' connect_readonly does the same. Anything
    # else, and the database file itself, must be identical.
    empty = hashlib.sha256(b"").hexdigest()
    for suffix in ("-wal", "-shm"):
        name = f"data/operational/{book.db.name}{suffix}"
        if name in after and name not in before:
            if suffix == "-wal":
                assert after[name][0] == empty, "a read must never put a row in the WAL"
            after.pop(name)
    assert after == before
    assert f"data/operational/{book.db.name}" in after


# ================================================== never loads network code
def test_the_page_never_loads_network_code(book: Root, tmp_path: Path) -> None:
    """Fresh interpreter: import the page, build its view on a real book. None
    of fetch.py, the Dhan market-data clients, the scrip master, AuthBootstrap
    (the whole ``common.authentication`` package), dhanhq, ``common.retention``
    or a paper runtime is loaded."""
    script = textwrap.dedent(
        f"""
        import sys
        sys.path[:0] = [{str(REPO)!r}]
        from pathlib import Path
        import dashboards.positional_stocks
        from dashboards.data.positional_stocks import StocksPaths, load_view
        view = load_view(StocksPaths(Path({str(book.path)!r}), Path({str(tmp_path)!r})))
        assert view.state == "ok" and view.positions, view.state_detail
        bad = (
            "runtimes.positional_stocks.fetch",
            "runtimes.positional_stocks.weekly_run",
            "runtimes.positional_stocks.run_config",
            "common.market_data.dhan",
            "common.market_data.dhan_historical",
            "common.market_data.scrip_master",
            "common.authentication",
            "dhanhq",
            "common.retention",
            "runtimes.intraday_options",
            "runtimes.positional_options",
            "orchestration.auto_start",
        )
        print(sorted(m for m in sys.modules if any(m == b or m.startswith(b + ".") for b in bad)))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=REPO, check=True
    )
    assert result.stdout.strip().splitlines()[-1] == "[]", result.stdout + result.stderr


# ================================================ never disturbs a run
def test_a_dashboard_read_never_disturbs_a_decide_run(tmp_path: Path) -> None:
    """Two identical books at week 3. On A, a dashboard connection holds an
    open read transaction and a thread keeps loading the page's view while
    week 4 is decided; B decides week 4 alone. Both runs succeed and leave
    identical books and journals, and every read taken during A's run is the
    week-3 state, the week-4 state or "book busy" — never an exception."""
    a = build_book(tmp_path / "a", weeks=(1, 2, 3))
    b = build_book(tmp_path / "b", weeks=(1, 2, 3))
    week3, week4 = a.as_of(3), a.as_of(4)

    reader = connect_readonly(a.db)
    reader.execute("BEGIN")
    held_open = reader.execute("SELECT count(*) FROM stock_fills").fetchone()[0]

    seen: list[tuple[str, str | None] | Exception] = []
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            try:
                view = view_of(a)
            except Exception as exc:  # the property under test: never happens
                seen.append(exc)
                return
            eq = view.book.latest_equity if view.book is not None else None
            seen.append((view.state, eq.week_ending if eq is not None else None))

    thread = threading.Thread(target=poll)
    thread.start()
    try:
        code_a = a.run("--as-of", week4)
    finally:
        stop.set()
        thread.join(timeout=120)
    # The open read transaction still sees its snapshot, then lets go.
    assert reader.execute("SELECT count(*) FROM stock_fills").fetchone()[0] == held_open
    reader.execute("COMMIT")
    reader.close()

    code_b = b.run("--as-of", week4)
    assert (code_a, code_b) == (0, 0), a.output
    repo_a, repo_b = a.repo(), b.repo()
    assert repo_a.dump() == repo_b.dump()
    repo_a.database.close()
    repo_b.database.close()
    assert (a.reports / "journal.csv").read_text() == (b.reports / "journal.csv").read_text()

    assert seen, "the poller must have read at least once"
    for item in seen:
        assert not isinstance(item, Exception), repr(item)
        assert item in {(ps.OK, week3), (ps.OK, week4), (ps.BUSY_STATE, None)}, item
    after = view_of(a)
    assert after.book is not None and after.book.latest_equity is not None
    assert after.book.latest_equity.week_ending == week4


def test_the_new_page_is_discovered_by_streamlit() -> None:
    shim = REPO / "dashboards" / "pages" / "5_Positional_Stocks.py"
    assert "from dashboards.positional_stocks import main" in shim.read_text()
