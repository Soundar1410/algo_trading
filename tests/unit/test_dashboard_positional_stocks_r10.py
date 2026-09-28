"""Phase 6-fix (audit round 10): the Positional Stocks page.

* **R10-1** — a failed weekend fetch is never "Operator actions: 0" in green:
  a red banner, "Last preview" shows the failure, and the failed symbols are
  the list the fetch wrote, or unknown. Every report here is written by the
  real fetch (``test_stock_fetch``'s in-memory Dhan) or the real refusal path.
* **R10-2** — refusals and NO TRADES reports newer than the last COMPLETED run
  are warned about on Overview; "book is behind" once a week's decide slot
  (from the YAML) plus 60 minutes has passed; before the first book, previews
  and refusals are still listed.
* **R10-3** — a real candidate with no quality row is counted as needs-quality,
  through the one ``rules.NEEDS_QUALITY`` constant.
* **Sidebar order** — Streamlit's own page discovery.
"""

from __future__ import annotations

import math
import random
import shutil
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import streamlit.commands.navigation as st_navigation
from _stock_run_fixtures import IST, REPO, Root, falling, wobble
from streamlit.testing.v1 import AppTest
from test_dashboard_positional_stocks_data import _with_config, build_book, paths
from test_stock_fetch import FRIDAY, FakeDhan, _fetch, _universe

from dashboards.data import positional_stocks as ps
from runtimes.positional_stocks import fetch as fetch_module
from runtimes.positional_stocks.fetch import FetchRefused
from runtimes.positional_stocks.run_config import RunConfig
from runtimes.positional_stocks.weekly_run import (
    EXIT_FETCH_FAILED,
    EXIT_NO_TRADES,
    EXIT_OK,
    EXIT_REFUSED,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import DailyBar
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar

PAGE = REPO / "dashboards" / "positional_stocks.py"
SATURDAY_0800 = datetime(2026, 9, 26, 8, 0, tzinfo=IST)
SUNDAY_1000 = datetime(2026, 9, 27, 10, 0, tzinfo=IST)


def view_at(root: Root, moment: datetime) -> ps.StocksView:
    return ps.load_view(paths(root), now=lambda: moment)


def _dhan(root: Root) -> FakeDhan:
    """Dhan's truth for the fixture book's symbols (A falls from week 2)."""
    return FakeDhan(
        root,
        {
            "NIFTY": lambda d: (20000.0 + (d.toordinal() % 7), 20000.0 + d.toordinal() % 5),
            "A": falling(root.first_session(2)),
            "B": wobble(800.0),
            "C": wobble(500.0),
        },
    )


def _action(view: ps.StocksView, label: str) -> ps.ActionItem:
    return next(a for a in view.actions if a.label == label)


# ============================================================== R10-1
@pytest.fixture
def book(tmp_path: Path) -> Root:
    return build_book(tmp_path / "project")


def test_r10_1_eleven_failed_symbols_banner_and_count(book: Root) -> None:
    dhan = _dhan(book)
    extra = [f"S{i:02d}" for i in range(11)]
    dhan.prices.update({s: wobble(100.0) for s in extra})
    _universe(book, extra)
    dhan.failing = set(extra)
    assert _fetch(book, dhan, now=lambda: SATURDAY_0800) == EXIT_FETCH_FAILED
    failed = book.reports / f"{FRIDAY}-preview-failed.md"
    assert failed.is_file()

    view = view_at(book, SATURDAY_0800)
    assert view.fetch_failure is not None and view.fetch_failure.path == failed
    assert view.fetch_failure_reason is not None
    assert "11 symbols failed after the second pass" in view.fetch_failure_reason
    assert view.last_preview_label == f"FAILED — {failed.name}"
    action = _action(view, "Failed fetch symbols")
    assert action.error is None and action.count == 11
    assert set(action.items) == set(extra)
    total = ps.total_actions(view.actions)
    assert total is not None and total >= 11


def test_r10_1_an_auth_refusal_is_unknown_never_zero(book: Root) -> None:
    dhan = _dhan(book)

    def refused(minimum: float, allow: bool) -> fetch_module.Credentials:
        raise FetchRefused("login failed: TOTP rejected")

    code = _fetch(book, dhan, now=lambda: SATURDAY_0800, fetch_services=dhan.services(auth=refused))
    assert code == EXIT_REFUSED
    view = view_at(book, SATURDAY_0800)
    assert view.fetch_failure is not None
    assert view.fetch_failure_reason is not None
    assert view.fetch_failure_reason.startswith("REFUSED (fetch) — ")
    assert "TOTP rejected" in view.fetch_failure_reason
    action = _action(view, "Failed fetch symbols")
    assert action.count is None
    assert action.error == (
        f"unknown: {view.fetch_failure.path.name} does not list the symbols (REFUSED (fetch))"
    )
    assert ps.total_actions(view.actions) is None


def test_r10_1_nifty_unpublished_at_the_final_attempt(book: Root) -> None:
    dhan = _dhan(book)
    dhan.missing = {"NIFTY": {FRIDAY}}
    assert _fetch(book, dhan, now=lambda: SUNDAY_1000) == EXIT_NO_TRADES
    view = view_at(book, SUNDAY_1000)
    assert view.fetch_failure is not None
    assert view.fetch_failure_reason is not None
    assert "not yet published" in view.fetch_failure_reason
    action = _action(view, "Failed fetch symbols")
    assert action.count is None and action.error is not None
    assert action.error.startswith("unknown: ")
    assert ps.total_actions(view.actions) is None


def test_r10_1_a_later_successful_preview_clears_the_banner(book: Root) -> None:
    dhan = _dhan(book)
    dhan.missing = {"NIFTY": {FRIDAY}}
    assert _fetch(book, dhan, now=lambda: SATURDAY_0800) == EXIT_NO_TRADES
    assert view_at(book, SATURDAY_0800).fetch_failure is not None
    dhan.missing.clear()
    later = SATURDAY_0800.replace(hour=14)
    assert _fetch(book, dhan, now=lambda: later) in (EXIT_OK, 6)
    view = view_at(book, later)
    assert view.fetch_failure is None and view.fetch_failure_reason is None
    assert view.last_preview_label == f"{FRIDAY}-preview.md"
    assert _action(view, "Failed fetch symbols").count == 0


def test_r10_1_contract_every_real_failure_report_parses(book: Root) -> None:
    """parse_failure / parse_failed_fetch_symbols on the three real outputs,
    and a wording mutation of either marker is an error or unknown — never 0."""
    dhan = _dhan(book)
    extra = [f"S{i:02d}" for i in range(11)]
    dhan.prices.update({s: wobble(100.0) for s in extra})
    _universe(book, extra)
    dhan.failing = set(extra)
    assert _fetch(book, dhan, now=lambda: SATURDAY_0800) == EXIT_FETCH_FAILED
    text = (book.reports / f"{FRIDAY}-preview-failed.md").read_text()
    info = ps.parse_failure(text)
    assert info.kind == "PREVIEW NOT WRITTEN — fetch failed"
    symbols = ps.parse_failed_fetch_symbols(info.reason)
    assert symbols is not None and sorted(symbols) == extra

    with pytest.raises(ps.ReportParseError):
        ps.parse_failure(text.replace("** ", "*: ", 1).replace(":**", "::"))
    with pytest.raises(ps.ReportParseError):
        ps.parse_failure(text.replace("NO TRADES", "NOTHING"))
    assert ps.parse_failed_fetch_symbols(info.reason.replace(" Failed: ", " Broken: ")) is None
    with pytest.raises(ps.ReportParseError):
        ps.parse_failed_fetch_symbols(info.reason.replace("S00 (", "s00 [").replace("(", "["))


def test_r10_1_page_banner_and_never_green(
    book: Root, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dhan = _dhan(book)
    dhan.missing = {"NIFTY": {FRIDAY}}
    assert _fetch(book, dhan, now=lambda: SUNDAY_1000) == EXIT_NO_TRADES
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PROJECT_ROOT", str(book.path))
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.run()
    assert list(at.exception) == []
    overview = at.tabs[0]
    errors = [str(e.value) for e in overview.error]
    assert any(
        e.startswith("Weekend fetch failed: ") and e.endswith("— see Health") for e in errors
    )
    markdown = [str(m.value) for m in overview.markdown]
    assert f"**Last preview:** FAILED — {FRIDAY}-preview-failed.md" in markdown
    assert "#### Operator actions: could not be counted — see below" in markdown
    assert not any(":green[" in m for m in markdown if "Operator actions" in m)


# ============================================================== R10-2
def test_r10_2_a_refused_decide_after_the_last_completed_week(tmp_path: Path) -> None:
    """The audit case: W25 COMPLETED, then the W26 decide refused."""
    root = build_book(tmp_path / "project", weeks=(1, 2, 3))
    assert root.run("--as-of", root.as_of(4), config=RunConfig()) == EXIT_REFUSED
    refused = root.reports / f"{root.last_session(4)}-refused.md"
    assert refused.is_file()
    view = view_at(root, datetime(2026, 6, 30, 12, 0, tzinfo=IST))  # Tue after W26's Monday slot
    matching = [w for w in view.warnings if w.startswith(f"{refused.name}: ")]
    assert len(matching) == 1
    assert "REFUSED (decide) — " in matching[0]
    assert any(
        w.startswith("Book is behind: last decided 2026-W25, expected 2026-W26")
        for w in view.warnings
    )


def test_r10_2_an_older_refusal_is_not_warned_about(tmp_path: Path) -> None:
    root = build_book(tmp_path / "project", weeks=(1,))
    assert root.run("--as-of", root.as_of(2), config=RunConfig()) == EXIT_REFUSED
    for n in (2, 3):
        assert root.run("--as-of", root.as_of(n)) == 0, root.output
    view = view_at(root, datetime(2026, 6, 23, 12, 0, tzinfo=IST))  # Tue after W25's slot
    assert not any("-refused.md" in w for w in view.warnings)
    assert any(r.path.name.endswith("-refused.md") for r in view.refusals)


def test_r10_2_a_decide_cold_cache_is_a_failure_report_not_a_decision(tmp_path: Path) -> None:
    root = build_book(tmp_path / "project", weeks=(1, 2, 3))
    root.write(
        "NIFTY",
        lambda d: (20000.0 + (d.toordinal() % 7), 20000.0 + d.toordinal() % 5),
        skip={root.last_session(4)},
    )
    assert root.run("--as-of", root.as_of(4)) == EXIT_NO_TRADES
    cold = root.reports / f"{root.last_session(4)}.md"
    assert cold.read_text().splitlines()[0].endswith(")")  # a NO TRADES title
    view = view_at(root, datetime(2026, 6, 30, 12, 0, tzinfo=IST))  # Tue after W26's Monday slot
    kinds = {r.path.name: r.kind for r in ps.list_reports(root.reports)}
    assert kinds[cold.name] == "no-trades"
    assert kinds[f"{root.last_session(3)}.md"] == "decision"
    assert view.latest_decision is not None
    assert view.latest_decision.path.name == f"{root.last_session(3)}.md"
    assert view.marks_note is None  # marks still from the W25 decision report
    assert any(w.startswith(f"{cold.name}: ") for w in view.warnings)


def _preview_and_refusal_before_go_live(root: Root) -> tuple[Path, Path]:
    """The go-live weekend: a Saturday preview, then a Monday decide refused
    (both flags still off), with no book yet."""
    dhan = FakeDhan(
        root,
        {
            "NIFTY": lambda d: (20000.0 + (d.toordinal() % 7), 20000.0 + d.toordinal() % 5),
            "A": wobble(1000.0),
            "B": wobble(800.0),
            "C": wobble(500.0),
        },
    )
    assert _fetch(root, dhan, now=lambda: SATURDAY_0800) == EXIT_OK
    monday = datetime(2026, 9, 28, 8, 30, tzinfo=IST)
    assert root.run("--as-of", "auto", config=RunConfig(), now=lambda: monday) == EXIT_REFUSED
    assert not root.db.exists()
    return root.reports / f"{FRIDAY}-preview.md", root.reports / f"{FRIDAY}-refused.md"


def test_r10_2_before_go_live_previews_and_refusals_are_listed(tmp_path: Path) -> None:
    root = _with_config(Root.create(tmp_path / "project"))
    preview, refused = _preview_and_refusal_before_go_live(root)
    view = view_at(root, datetime(2026, 9, 28, 9, 0, tzinfo=IST))
    assert view.state == ps.NOT_STARTED_STATE
    assert [r.path for r in view.previews] == [preview]
    assert [r.path for r in view.refusals] == [refused]
    assert view.latest_preview is not None and view.latest_preview.path == preview
    assert view.fetch_failure is None and view.book is None and view.actions == ()


def test_r10_2_before_go_live_the_page_lists_them_newest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _with_config(Root.create(tmp_path / "project"))
    preview, refused = _preview_and_refusal_before_go_live(root)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PROJECT_ROOT", str(root.path))
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.run()
    assert list(at.exception) == []
    tabs = {t.label: t for t in at.tabs}
    for tab in at.tabs:
        assert str(tab.info[0].value).startswith("Not started yet"), tab.label
        assert len(tab.dataframe) == (1 if tab.label == "Overview" else 0), tab.label
    listed = [str(m.value) for m in tabs["Overview"].markdown if str(m.value).startswith("- `")]
    assert listed == [f"- `{refused.name}` — refused", f"- `{preview.name}` — preview"]
    report_md = [str(m.value) for m in tabs["Latest report"].markdown]
    assert any("PREVIEW" in m and m.startswith("# wsr1_weekly_stochrsi") for m in report_md)
    assert [e.label for e in tabs["Health"].expander] == [refused.name]
    for label in ("Positions & orders", "Trades & performance", "Equity"):
        assert len(tabs[label].markdown) == 0, label


# ------------------------------------------------ "book is behind" (revised)
def _book_through(iso_week: str, week_ending: str) -> ps.BookData:
    run = ps.RunRow(week_ending, iso_week, "COMPLETED", "2026-01-01T00:00:00+00:00", None)
    return ps.BookData((run,), (), (), (), (), {}, ())


CONFIG = REPO / "config"


@pytest.mark.parametrize(
    ("moment", "last", "expected"),
    [
        (datetime(2026, 9, 26, 10, 0, tzinfo=IST), ("2026-W38", "2026-09-18"), None),
        (datetime(2026, 9, 28, 9, 0, tzinfo=IST), ("2026-W38", "2026-09-18"), None),
        (datetime(2026, 9, 28, 9, 30, tzinfo=IST), ("2026-W38", "2026-09-18"), None),
        (
            datetime(2026, 9, 28, 9, 31, tzinfo=IST),
            ("2026-W38", "2026-09-18"),
            "Book is behind: last decided 2026-W38, expected 2026-W39 — the Monday decide "
            "run did not complete; see Health.",
        ),
        (datetime(2026, 9, 29, 12, 0, tzinfo=IST), ("2026-W39", "2026-09-25"), None),
        (
            datetime(2026, 9, 29, 12, 0, tzinfo=IST),
            ("2026-W37", "2026-09-11"),
            "Book is behind: last decided 2026-W37, expected 2026-W39 — the Monday decide "
            "run did not complete; see Health.",
        ),
        # A Monday-holiday week: 2026-01-26 (Republic Day) still decides at its slot.
        (datetime(2026, 1, 26, 9, 29, tzinfo=IST), ("2026-W03", "2026-01-16"), None),
        (
            datetime(2026, 1, 26, 9, 31, tzinfo=IST),
            ("2026-W03", "2026-01-16"),
            "Book is behind: last decided 2026-W03, expected 2026-W04 — the Monday decide "
            "run did not complete; see Health.",
        ),
    ],
    ids=[
        "saturday",
        "monday-0900",
        "monday-0930-grace",
        "monday-0931",
        "tuesday-decided",
        "one-week-behind",
        "monday-holiday-before",
        "monday-holiday-after",
    ],
)
def test_r10_2_book_is_behind_from_the_decide_slot_plus_grace(
    moment: datetime, last: tuple[str, str], expected: str | None
) -> None:
    config = ps.load_config_view(CONFIG)
    assert config.schedule_error is None
    assert ps.behind_warning(_book_through(*last), config, CONFIG, moment) == expected


@pytest.mark.parametrize("slot", [None, "TUESDAY 07:15", "SUNDAY 23:00"])
def test_r10_2_the_decide_slot_is_the_runs_own(tmp_path: Path, slot: str | None) -> None:
    root = _with_config(Root.create(tmp_path / "project"))
    strategy = (
        root.path / "config" / "strategies" / "positional_stocks" / "wsr1_weekly_stochrsi.yaml"
    )
    if slot is not None:
        strategy.write_text(
            strategy.read_text().replace('decide: "MONDAY 08:30"', f'decide: "{slot}"')
        )
    bound = RunConfig.from_config(root.path / "config")
    shown = ps.load_config_view(root.path / "config")
    calendar = TradingCalendar.from_config(root.path / "config")
    # Normal weeks, a Monday-holiday week (W04: Mon 26 Jan) and Friday-holiday
    # weeks (W14: Good Friday 3 Apr; W26: 26 Jun; W40: 2 Oct).
    for week in ((2026, 4), (2026, 14), (2026, 26), (2026, 39), (2026, 40), (2026, 45)):
        ending = calendar.expected_last_session(week)
        assert shown.decide_after(ending) == bound.schedule.decide_after(ending, IST), week


def test_r10_2_an_unreadable_slot_says_it_cannot_check(tmp_path: Path) -> None:
    root = _with_config(Root.create(tmp_path / "project"))
    strategy = (
        root.path / "config" / "strategies" / "positional_stocks" / "wsr1_weekly_stochrsi.yaml"
    )
    strategy.write_text(strategy.read_text().replace('decide: "MONDAY 08:30"', 'decide: "SOON"'))
    config = ps.load_config_view(root.path / "config")
    assert config.schedule_error is not None
    warning = ps.behind_warning(
        _book_through("2026-W38", "2026-09-18"),
        config,
        root.path / "config",
        datetime(2026, 9, 29, 12, 0, tzinfo=IST),
    )
    assert warning is not None and warning.startswith("Cannot check whether the book is behind: ")


# ============================================================== R10-3
def _triggering_bars(root: Root) -> list[DailyBar]:
    """A noisy uptrend (seed 2) with a 3-week 12% pullback ending a week before
    W39: at W39's close the real Stoch RSI crosses up from oversold with K < 50,
    and every other filter passes (found by search, D146)."""
    rng = random.Random(2)
    end = date(2026, 9, 25)
    dip_start = end - timedelta(weeks=4) - timedelta(days=4)
    dip_end = dip_start + timedelta(weeks=3)
    price = prev = 400.0
    bars: list[DailyBar] = []
    day = date(2021, 1, 4)
    while day <= end:
        if root.calendar.is_trading_day(day):
            drift = 0.25 / 250
            if dip_start <= day <= dip_end:
                drift = -0.12 / 15
            elif day > dip_end:
                drift = 0.12 / 8
            price *= math.exp(drift + rng.gauss(0, 0.012))
            open_ = prev * math.exp(rng.gauss(0, 0.004))
            bars.append(
                DailyBar(
                    day,
                    round(open_, 2),
                    round(max(open_, price) * 1.006, 2),
                    round(min(open_, price) * 0.994, 2),
                    round(price, 2),
                    5e6,
                )
            )
            prev = price
        day += timedelta(days=1)
    return bars


def _trigger_root(tmp: Path, *, quality: bool) -> Root:
    root = _with_config(Root.create(tmp))
    root.standard_cache()
    root.cache.write("T", _triggering_bars(root), fetched_at=datetime(2026, 9, 26, tzinfo=UTC))
    stock = root.path / "config" / "positional_stocks"
    universe = stock / "universe.csv"
    universe.write_text(
        universe.read_text() + "T,INE000000009,Tau Ltd.,Metals,true,,hold,2026-07-22\n"
    )
    if quality:
        gate = stock / "quality_gate.csv"
        gate.write_text(gate.read_text() + "T,PASS,2026-01-01,2099-01-01,\n")
    return root


def test_r10_3_a_real_candidate_without_a_quality_row_is_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtimes.positional_stocks import weekly_run
    from runtimes.positional_stocks.report import ReportData, render
    from runtimes.positional_stocks.telegram_summary import operator_actions

    captured: list[ReportData] = []

    def spy(data: ReportData) -> str:
        captured.append(data)
        return render(data)

    monkeypatch.setattr(weekly_run, "render", spy)
    root = _trigger_root(tmp_path / "missing", quality=False)
    assert root.run("--as-of", "2026-09-25") == EXIT_OK, root.output
    reason = (
        sqlite3.connect(root.db)
        .execute("SELECT stage, reason FROM stock_signals WHERE symbol = 'T'")
        .fetchone()
    )
    assert tuple(reason) == ("filtered", ps.NEEDS_QUALITY)
    view = view_at(root, datetime(2026, 9, 28, 12, 0, tzinfo=IST))
    action = _action(view, "Candidates waiting for a quality row")
    assert (action.count, action.items) == (1, ("T",))
    assert operator_actions(captured[-1]).needs_quality == 1

    # The same symbol with a PASS row is taken: quality was its only refusal.
    passing = _trigger_root(tmp_path / "pass", quality=True)
    assert passing.run("--as-of", "2026-09-25") == EXIT_OK, passing.output
    stage = (
        sqlite3.connect(passing.db)
        .execute("SELECT stage FROM stock_signals WHERE symbol = 'T'")
        .fetchone()[0]
    )
    assert stage == "taken"
    assert (
        _action(
            view_at(passing, datetime(2026, 9, 28, 12, 0, tzinfo=IST)),
            "Candidates waiting for a quality row",
        ).count
        == 0
    )


# ======================================================== sidebar order
def test_the_sidebar_order_is_streamlits_own_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streamlit's own ``pages/`` discovery (``script_runner._mpa_v1``) hands
    its pages to ``_navigation``; this captures that list from a real AppTest
    run of Home.py."""
    shutil.copytree(REPO / "config", tmp_path / "config")
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    seen: list[tuple[str, str]] = []
    real = st_navigation._navigation

    def capture(pages, *, position, expanded):  # type: ignore[no-untyped-def]
        seen.extend((p.title, p.url_path) for p in pages)
        return real(pages, position=position, expanded=expanded)

    monkeypatch.setattr(st_navigation, "_navigation", capture)
    at = AppTest.from_file(str(REPO / "dashboards" / "Home.py"), default_timeout=60)
    at.run()
    assert seen == [
        ("Home", ""),
        ("Intraday Options", "Intraday_Options"),
        ("Positional Options", "Positional_Options"),
        ("Intraday Stocks", "Intraday_Stocks"),
        ("Positional Stocks", "Positional_Stocks"),
        ("System Health", "System_Health"),
    ]
    shim = REPO / "dashboards" / "pages" / "4_Positional_Stocks.py"
    assert "from dashboards.positional_stocks import main" in shim.read_text()
