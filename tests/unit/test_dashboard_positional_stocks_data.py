"""Phase 6 (spec v1.3.2 11.1): the Positional Stocks read model.

Every book here is written by the real decide run (``_stock_run_fixtures``), so
each value the dashboard shows is checked against what the run persisted or
reported — never against a second computation. The report-text parsers get a
contract test each: a real report is rendered by
``runtimes.positional_stocks.report`` from a book or outcome that contains the
item, and the parser must extract the exact value. A change of wording in the
report fails one of these tests instead of silently showing zero.
"""

from __future__ import annotations

import functools
import shutil
import sqlite3
from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest
from _stock_run_fixtures import IST, REPO, Root, falling

import dashboards
from common.persistence import connect_readonly
from dashboards import _shared
from dashboards.data import positional_stocks as ps
from runtimes.positional_stocks import weekly_run
from runtimes.positional_stocks.accounting import CorporateActionEvent
from runtimes.positional_stocks.report import PreviewInfo, ReportData, render
from runtimes.positional_stocks.telegram_summary import operator_actions
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import PositionState, money
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar


def test_the_suite_imports_the_dashboard_from_this_checkout() -> None:
    """Phase 6 is built in a worktree while .venv's editable install points at
    the live folder: the modules under test must be this checkout's."""
    assert Path(dashboards.__file__).resolve().parents[1] == REPO


# ============================================================= fixtures
def _with_config(root: Root) -> Root:
    for relative in (
        Path("runtimes") / "positional_stocks.yaml",
        Path("strategies") / "positional_stocks" / "wsr1_weekly_stochrsi.yaml",
    ):
        target = root.path / "config" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "config" / relative, target)
    return root


def build_book(tmp: Path, weeks: tuple[int, ...] = (1, 2, 3, 4)) -> Root:
    """A falls to its stop (T1 week 1, stop decided week 3, filled week 4: a
    closed trade); C enters in week 3 and is held; B's entry stays pending."""
    root = _with_config(Root.create(tmp))
    root.standard_cache(a=falling(root.first_session(2)))
    root.seed_entry()
    root.seed_entry("B", industry="Chemicals", execute_on=date(2026, 12, 31), mark_week=False)
    root.seed_entry("C", industry="Services", execute_on=root.first_session(3), mark_week=False)
    for n in weeks:
        assert root.run("--as-of", root.as_of(n)) == 0, root.output
    return root


def paths(root: Root) -> ps.StocksPaths:
    return ps.StocksPaths(root.path, root.path / "LaunchAgents")


def view_of(root: Root) -> ps.StocksView:
    return ps.load_view(paths(root), now=lambda: datetime.combine(date(2026, 9, 26), time(9), IST))


@pytest.fixture
def book(tmp_path: Path) -> Root:
    return build_book(tmp_path)


def _captured(root: Root, monkeypatch: pytest.MonkeyPatch, n: int) -> ReportData:
    """The ReportData a real run of week ``n`` rendered (not a dry run)."""
    captured: list[ReportData] = []
    assert weekly_run.__dict__["render"] is render

    def spy(data: ReportData) -> str:
        captured.append(data)
        return render(data)  # weekly_run's render is report.render

    monkeypatch.setattr(weekly_run, "render", spy)
    assert root.run("--as-of", root.as_of(n)) == 0, root.output
    monkeypatch.setattr(weekly_run, "render", render)
    return captured[-1]


# ======================================================== the book itself
def test_every_book_value_is_the_persisted_one(book: Root) -> None:
    view = view_of(book)
    assert view.state == ps.OK and view.book is not None
    repo = book.repo()
    conn = repo.database.connect()
    latest = conn.execute("SELECT * FROM stock_equity ORDER BY week_ending DESC LIMIT 1").fetchone()
    eq = view.book.latest_equity
    assert eq is not None
    assert (eq.week_ending, eq.regime) == (latest["week_ending"], latest["regime"])
    for name in ("cash", "positions_value", "equity", "peak", "drawdown_pct"):
        assert getattr(eq, name) == Decimal(latest[name]), name
    assert eq.brake1_until == latest["brake1_until"]
    assert eq.brake2_fired_on == latest["brake2_fired_on"]
    assert eq.entries_blocked == latest["entries_blocked"]
    assert len(view.book.equity) == conn.execute("SELECT count(*) FROM stock_equity").fetchone()[0]

    held = sorted(
        (p for p in repo.positions().values() if p.state is not PositionState.CLOSED),
        key=lambda p: p.symbol,
    )
    assert [v.position for v in view.positions] == held
    assert [p.symbol for p in held] == ["C"]
    assert [(o.symbol, o.action) for o in view.book.pending] == [("B", "BUY_T1")]
    pending = repo.pending_orders()
    assert [o.amount for o in view.book.pending] == [o.amount for o in pending]
    assert view.pending_entries == 1
    assert view.committed_held == sum((p.committed for p in held), Decimal("0"))
    assert [(r.iso_week, r.status) for r in view.book.runs] == [
        (r["iso_week"], r["status"])
        for r in conn.execute("SELECT * FROM stock_weekly_runs ORDER BY week_ending")
    ]
    fills = conn.execute("SELECT count(*) FROM stock_fills").fetchone()[0]
    assert len(view.book.fills) == fills
    repo.database.close()


def test_marks_are_the_reports_own(book: Root, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mark, unrealised P&L and weeks held are what the run reported for
    the latest COMPLETED week — compared with the run's own ReportData."""
    data = _captured(book, monkeypatch, 5)
    view = view_of(book)
    assert view.marks_note is None
    by_symbol = {v.position.symbol: v for v in view.positions}
    assert data.positions, "the comparison must not be vacuous"
    for row in data.positions:
        p = row.position
        shown = by_symbol[p.symbol]
        assert row.mark is not None and shown.mark == row.mark
        assert shown.unrealised == money(row.mark * p.shares_held - p.average_cost * p.shares_held)
        assert shown.weeks_held == row.weeks_held
        cost = p.average_cost * p.shares_held
        assert shown.unrealised_pct == (shown.unrealised / cost * 100).quantize(Decimal("0.01"))


def test_a_report_for_another_week_shows_no_marks(book: Root) -> None:
    (book.reports / f"{book.last_session(4)}.md").unlink()
    view = view_of(book)
    assert all(v.mark is None and v.unrealised is None for v in view.positions)
    assert view.marks_note is not None
    assert "no report for week 2026-W26" in view.marks_note
    stale = next(a for a in view.actions if a.label.startswith("Stale"))
    assert stale.count is None and stale.error == view.marks_note
    assert ps.total_actions(view.actions) is None


# ================================================ report-text contracts
def test_contract_section_4_marks_unrealised_weeks_and_no_bar_flag(
    book: Root, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _captured(book, monkeypatch, 5)
    row = data.positions[0]
    data = replace(data, positions=(replace(row, stale_weeks=2), *data.positions[1:]))
    parsed = ps.parse_open_positions(render(data))
    p = row.position
    got = parsed[p.symbol]
    assert row.mark is not None and got.mark == row.mark
    assert got.unrealised == money(row.mark * p.shares_held - p.average_cost * p.shares_held)
    assert got.weeks_held == row.weeks_held
    assert got.stale_weeks == 2
    assert "no bar 2 week(s)" in got.flags
    # One week without a bar is reported but is not an operator action.
    one = replace(data, positions=(replace(row, stale_weeks=1),))
    assert ps.parse_open_positions(render(one))[p.symbol].stale_weeks == 1


def test_contract_section_4_with_no_position(book: Root, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _captured(book, monkeypatch, 5)
    assert ps.parse_open_positions(render(replace(data, positions=()))) == {}


def test_contract_title(book: Root, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _captured(book, monkeypatch, 5)
    assert ps.parse_report_week(render(data)) == ("2026-W27", book.as_of(5))


def test_contract_section_8_silent_lift(book: Root, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _captured(book, monkeypatch, 5)
    lift = CorporateActionEvent("silent_lift", "C", "C-2026W25", "frozen last run, not frozen now")
    last = data.weeks[-1]
    data = replace(
        data,
        weeks=(
            *data.weeks[:-1],
            replace(last, outcome=replace(last.outcome, silent_lifts=(lift,))),
        ),
    )
    assert ps.parse_silent_lifts(render(data)) == (
        ps.SilentLift("C", "C-2026W25", "frozen last run, not frozen now"),
    )
    assert operator_actions(data).silent_lifts == 1


def test_contract_preview_fetch_failed(book: Root, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _captured(book, monkeypatch, 5)
    failed = replace(data, preview=PreviewInfo(lines=("cache refreshed",), failed=("B", "C")))
    assert ps.parse_fetch_failed(render(failed)) == ("B", "C")
    assert operator_actions(failed).fetch_failed == 2
    clean = replace(data, preview=PreviewInfo(lines=("cache refreshed",), failed=()))
    assert ps.parse_fetch_failed(render(clean)) == ()


@pytest.mark.parametrize(
    ("parser", "text"),
    [
        (ps.parse_open_positions, "# t\n\n## 4. Open positions\n\n| Symbol | Mark |\n|---|---|\n"),
        (
            ps.parse_open_positions,
            "# t\n\n## 4. Open positions\n\n" + ps.POSITIONS_HEADER + "\n|---|\n| A | x |\n",
        ),
        (ps.parse_open_positions, "# t\n\n## 5. Closed trades\n"),
        (
            ps.parse_silent_lifts,
            "# t\n\n## 8. Corporate actions (spec 4.14)\n\n- A — freeze lifted with neither an "
            "acknowledgement nor a rescale\n",
        ),
        (ps.parse_silent_lifts, "# t\n\n## 9. Fill flags\n"),
        (
            ps.parse_fetch_failed,
            "# t\n\n## 0. Fetch\n\n- **Fetch failed (3), treated as stale (spec 6.2):** B, C\n",
        ),
        (
            ps.parse_fetch_failed,
            "# t\n\n## 0. Fetch\n\n- **Fetch failed (2), treated as gone:** B\n",
        ),
        (ps.parse_fetch_failed, "# t\n\n## 1. Regime\n"),
    ],
)
def test_an_unrecognised_section_is_an_error_not_zero(parser: object, text: str) -> None:
    with pytest.raises(ps.ReportParseError):
        parser(text)  # type: ignore[operator]


def test_an_unreadable_section_8_is_shown_not_counted_as_zero(book: Root) -> None:
    report = book.reports / f"{book.last_session(4)}.md"
    report.write_text(report.read_text().replace("## 8. Corporate actions", "## 8. Corp. acts"))
    view = view_of(book)
    lifts = next(a for a in view.actions if a.label == "Silent freeze lifts")
    assert lifts.count is None
    assert lifts.error is not None
    assert lifts.error.startswith(f"could not read silent freeze lifts from {report.name}")
    assert ps.total_actions(view.actions) is None


def test_an_unreadable_section_4_is_shown_not_counted_as_zero(book: Root) -> None:
    report = book.reports / f"{book.last_session(4)}.md"
    report.write_text(report.read_text().replace("| Weeks held |", "| Held |"))
    view = view_of(book)
    assert view.marks_note is not None
    assert view.marks_note.startswith(f"could not read open positions from {report.name}")
    assert all(v.mark is None for v in view.positions)
    stale = next(a for a in view.actions if a.label.startswith("Stale"))
    assert stale.count is None and stale.error == view.marks_note


def test_an_unreadable_preview_is_shown_not_counted_as_zero(book: Root) -> None:
    preview = book.reports / f"{book.last_session(5)}-preview.md"
    preview.write_text("# garbage\n")
    view = view_of(book)
    fetch = next(a for a in view.actions if a.label == "Failed fetch symbols")
    assert fetch.count is None
    assert fetch.error is not None
    assert fetch.error.startswith(f"could not read failed fetch symbols from {preview.name}")
    assert view.newer_preview is not None and view.newer_preview.path == preview


def test_a_preview_older_than_the_decision_is_not_an_action(book: Root) -> None:
    (book.reports / f"{book.last_session(3)}-preview.md").write_text("# garbage\n")
    view = view_of(book)
    assert view.latest_preview is not None and view.newer_preview is None
    fetch = next(a for a in view.actions if a.label == "Failed fetch symbols")
    assert fetch.count == 0 and fetch.error is None


# ======================================== operator actions vs the runtime
def test_operator_actions_equal_the_runs_own_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frozen, escalated position (a 1:1 bonus the run has not been told
    about): the dashboard's counts equal telegram_summary.operator_actions of
    the run's own ReportData."""
    root = _with_config(Root.create(tmp_path))

    def bonus(day: date) -> tuple[float, float]:
        return (500.0, 500.0) if day >= root.first_session(2) else (1000.0, 1000.0)

    root.standard_cache(a=bonus)
    root.seed_entry()
    for n in (1, 2, 3):
        assert root.run("--as-of", root.as_of(n)) == 0
    data = _captured(root, monkeypatch, 4)
    expected = operator_actions(data)
    assert expected.frozen == 1 and expected.escalated == 1
    view = view_of(root)
    counts = {a.label: a.count for a in view.actions}
    assert counts["Frozen positions"] == expected.frozen
    assert counts["Escalated freezes"] == expected.escalated
    assert counts["Silent freeze lifts"] == expected.silent_lifts
    assert counts["Candidates waiting for a quality row"] == expected.needs_quality
    assert counts["Stale held symbols (2+ weeks without a bar)"] == expected.stale
    assert ps.total_actions(view.actions) == expected.total
    (shown,) = view.positions
    assert shown.frozen and shown.escalated
    assert shown.review_reason is not None and shown.review_reason.startswith("frozen:")


def test_needs_quality_comes_from_the_persisted_funnel(tmp_path: Path) -> None:
    root = _with_config(Root.create(tmp_path))
    root.standard_cache()
    root.seed_entry()
    assert root.run("--as-of", root.as_of(1)) == 0
    conn = sqlite3.connect(root.db)
    conn.execute(
        "UPDATE stock_signals SET reason = ? WHERE symbol = 'B' AND week_ending = ?",
        (ps.NEEDS_QUALITY, root.as_of(1)),
    )
    conn.commit()
    conn.close()
    action = next(a for a in view_of(root).actions if a.label.startswith("Candidates"))
    assert (action.count, action.items) == (1, ("B",))


# =========================================================== bad inputs
def test_no_database_is_not_started(tmp_path: Path) -> None:
    root = _with_config(Root.create(tmp_path))
    view = view_of(root)
    assert view.state == ps.NOT_STARTED_STATE and not view.started
    assert view.book is None and view.positions == () and view.actions == ()
    assert "Not started yet" in view.state_detail
    assert "go-live checklist" in view.state_detail
    assert view.config.runtime_enabled is False and view.config.strategy_enabled is False


def test_a_corrupt_database_is_unreadable_not_a_traceback(book: Root) -> None:
    for sidecar in ("-wal", "-shm"):
        book.db.with_name(book.db.name + sidecar).unlink(missing_ok=True)
    book.db.write_bytes(b"this is not a database" * 100)
    view = view_of(book)
    assert view.state == ps.UNREADABLE_STATE
    assert view.state_detail.startswith("The book could not be read")
    assert view.book is None


def test_a_locked_book_is_busy(book: Root, monkeypatch: pytest.MonkeyPatch) -> None:
    """In WAL mode a writer never blocks this reader (see the page tests). A
    rollback-journal writer holding EXCLUSIVE does: that is "book busy"."""
    conn = sqlite3.connect(book.db, isolation_level=None)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("BEGIN EXCLUSIVE")
    monkeypatch.setattr(
        _shared, "connect_readonly", functools.partial(connect_readonly, busy_timeout_ms=100)
    )
    try:
        view = view_of(book)
    finally:
        conn.execute("ROLLBACK")
        conn.close()
    assert view.state == ps.BUSY_STATE
    assert view.state_detail == ps.BUSY
    # The file-backed sections still render.
    assert view.journal.exists and view.journal.error is None
    assert view.latest_decision is not None


@pytest.mark.parametrize(
    "content",
    [
        b"not,the,journal,header\n1,2,3,4\n",
        None,  # a truncated row (built below)
        b"\xff\xfe\x00garbage",
    ],
    ids=["wrong-header", "truncated-row", "not-utf8"],
)
def test_a_corrupt_journal_is_a_message_and_the_rest_renders(
    book: Root, content: bytes | None
) -> None:
    journal = book.reports / "journal.csv"
    if content is None:
        lines = journal.read_text().splitlines()
        content = ("\n".join([*lines, lines[-1].rsplit(",", 5)[0]]) + "\n").encode()
    journal.write_bytes(content)
    view = view_of(book)
    assert view.journal.error is not None
    assert view.journal.error.startswith("journal.csv could not be read")
    assert view.journal.totals is None
    assert view.state == ps.OK and view.positions


def test_journal_totals(book: Root) -> None:
    journal = view_of(book).journal
    assert journal.error is None and journal.totals is not None
    (row,) = journal.rows
    assert row["symbol"] == "A" and row["exit_type"]
    pnl = Decimal(row["pnl_rs"])
    t = journal.totals
    assert (t.trades, t.net, t.best, t.worst) == (1, pnl, pnl, pnl)
    assert (t.wins, t.losses) == ((1, 0) if pnl > 0 else (0, 1))


# ================================================= config and constants
def test_config_values_equal_the_runs_binding(tmp_path: Path) -> None:
    from runtimes.positional_stocks.run_config import RunConfig

    root = _with_config(Root.create(tmp_path))
    strategy = (
        root.path / "config" / "strategies" / "positional_stocks" / "wsr1_weekly_stochrsi.yaml"
    )
    for text in (
        strategy.read_text(),
        strategy.read_text()
        .replace("capital: 1000000", "capital: 2500000")
        .replace("max_positions: 10", "max_positions: 8")
        .replace("committed_cap_pct: 100", "committed_cap_pct: 80")
        .replace("dd1_pct: 10,", "dd1_pct: 12.5,")
        .replace("dd2_pct: 20,", "dd2_pct: 25,")
        .replace("\nenabled: false", "\nenabled: true"),
    ):
        strategy.write_text(text)
        bound = RunConfig.from_config(root.path / "config")
        shown = ps.load_config_view(root.path / "config")
        assert shown.error is None
        assert shown.runtime_enabled == bound.runtime_enabled
        assert shown.strategy_enabled == bound.strategy_enabled
        assert shown.capital == bound.params.capital
        assert shown.max_positions == bound.params.max_positions
        assert shown.committed_cap == bound.params.committed_cap
        assert shown.dd1_pct == bound.params.dd1_pct
        assert shown.dd2_pct == bound.params.dd2_pct
    assert shown.max_positions == 8 and shown.strategy_enabled is True


def test_a_broken_config_is_a_message(tmp_path: Path) -> None:
    root = _with_config(Root.create(tmp_path))
    (root.path / "config" / "runtimes" / "positional_stocks.yaml").write_text("runtime_id: [\n")
    shown = ps.load_config_view(root.path / "config")
    assert shown.error is not None and shown.error.startswith("could not read the configuration")
    assert shown.runtime_enabled is None


def test_the_config_keys_are_the_runs_own() -> None:
    from runtimes.positional_stocks.run_config import _RULES

    for (section, key), attr in ps._CONFIG_KEYS.items():
        dotted = key if section is None else f"{section}.{key}"
        assert _RULES[dotted] == (section, attr)


def test_constants_are_the_runtimes_own() -> None:
    from orchestration.launchd.generate_plists import LABEL_PREFIX, operator_installed_specs
    from runtimes.positional_stocks import telegram_summary
    from runtimes.positional_stocks.run_config import INDEX_SYMBOL, STRATEGY_ID
    from strategies.positional_stocks.wsr1_weekly_stochrsi import rules

    assert ps.LABEL_PREFIX == LABEL_PREFIX
    assert tuple(s.short_name for s in operator_installed_specs()) == ps.AGENT_NAMES
    assert ps.INDEX_SYMBOL == INDEX_SYMBOL
    assert ps.STRATEGY_ID == STRATEGY_ID
    # R10-3: one constant, exported by the rules, used by all three.
    assert ps.NEEDS_QUALITY is rules.NEEDS_QUALITY is telegram_summary.NEEDS_QUALITY
    assert rules.NEEDS_QUALITY == "needs quality check"


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 9, 26, 9, 0, tzinfo=IST),
        datetime(2026, 9, 25, 15, 0, tzinfo=IST),
        datetime(2026, 9, 25, 23, 0, tzinfo=IST),
        datetime(2026, 9, 28, 8, 30, tzinfo=IST),
        datetime(2026, 10, 2, 12, 0, tzinfo=IST),
    ],
)
def test_latest_complete_week_is_the_runs_target_week(moment: datetime) -> None:
    calendar = TradingCalendar.from_config(REPO / "config")
    assert ps.latest_complete_week(moment, calendar) == weekly_run.target_week(moment, calendar)


def test_agents_installed_is_a_file_check(tmp_path: Path) -> None:
    agents = tmp_path / "LaunchAgents"
    assert ps.agents_installed(agents) == dict.fromkeys(ps.AGENT_NAMES, False)
    agents.mkdir()
    (agents / f"{ps.LABEL_PREFIX}.positional_stocks_decide.plist").write_text("<plist/>")
    assert ps.agents_installed(agents) == {
        "positional_stocks_fetch": False,
        "positional_stocks_decide": True,
    }


def test_the_repository_guard_refuses_a_write_capable_fallback() -> None:
    from runtimes.positional_stocks.repository import StockRepository

    repo = StockRepository(ps._NoDatabase(), ps.STRATEGY_ID, Decimal("1"))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="never opens a write-capable connection"):
        repo.positions()


def test_cache_freshness_and_backups(book: Root) -> None:
    view = view_of(book)
    assert view.cache.error is None
    assert view.cache.index_last_session == date(2026, 9, 25)
    assert view.cache.expected_week == "2026-W39"
    assert view.cache.fresh is True
    assert view.backups.count == len(list(book.backups.glob("positional_stocks_*.db")))
    assert view.backups.count >= 1 and view.backups.newest is not None
