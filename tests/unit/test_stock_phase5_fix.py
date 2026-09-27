"""Phase 5-fix (audit round 7): execution time and catch-up fills (F1), binder
gaps (F4), a STARTED first week (D128) and ``check_config`` (D129).

The execution-time table is the audit's ``p4h_exec_time``, on the 4b-1
fixture (A seeded, falling from week 2; W3's stop decided at W3):

| W3 decide run starts | fills at                          |
|----------------------|-----------------------------------|
| Mon 08:30            | Monday's open, 664.83             |
| Mon 11:00            | Tuesday's open, 638.24            |
| Tue 10:00            | Wednesday's open, 612.71 (D122)   |

The audit's table said Tuesday for Tue 10:00; the rule it states ("the first
session whose 09:15 open is strictly after the run's start"), spec 3 and its
own catch-up case all give Wednesday, and the operator confirmed Wednesday.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
import yaml
from _stock_run_fixtures import IST, Root, falling

from runtimes.positional_stocks import check_config, journal
from runtimes.positional_stocks.run_config import RunConfig, RunRefused
from runtimes.positional_stocks.week_inputs import execution_session
from runtimes.positional_stocks.weekly_run import EXIT_OK, EXIT_REFUSED
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar

REPO = Path(__file__).resolve().parents[2]
CALENDAR = TradingCalendar.from_config(REPO / "config")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ======================================================================= F1
def _stop_fill(tmp_path: Path, start: timedelta) -> tuple[str, str]:
    root = Root.create(tmp_path)
    root.standard_cache(a=falling(root.first_session(2)))
    root.seed_entry()
    for n in (1, 2):
        assert root.run("--as-of", root.as_of(n)) == EXIT_OK
    at = datetime.combine(root.first_session(4), time(0), IST) + start
    assert root.run("--as-of", root.as_of(3), now=lambda: at) == EXIT_OK
    assert root.run("--as-of", root.as_of(4)) == EXIT_OK
    row = (
        root.repo()
        .database.connect()
        .execute("SELECT session, price, catch_up FROM stock_fills WHERE action = 'SELL_ALL'")
        .fetchone()
    )
    assert row["catch_up"] == 0
    return row["session"], row["price"]


@pytest.mark.parametrize(
    ("start", "session", "price"),
    [
        (timedelta(hours=8, minutes=30), "2026-06-22", "664.83"),
        (timedelta(hours=11), "2026-06-23", "638.24"),
        (timedelta(days=1, hours=10), "2026-06-24", "612.71"),
    ],
    ids=["mon-0830", "mon-1100", "tue-1000"],
)
def test_f1_an_order_executes_at_the_first_open_after_the_run_started(
    tmp_path: Path, start: timedelta, session: str, price: str
) -> None:
    assert _stop_fill(tmp_path, start) == (session, price)


def test_f1_the_report_and_telegram_say_when_the_clock_moved_it(tmp_path: Path) -> None:
    root = Root.create(tmp_path)
    root.standard_cache(a=falling(root.first_session(2)))
    root.seed_entry()
    for n in (1, 2):
        assert root.run("--as-of", root.as_of(n)) == EXIT_OK
    late = datetime.combine(root.first_session(4), time(11), IST)
    assert root.run("--as-of", root.as_of(3), now=lambda: late) == EXIT_OK
    note = "orders execute at Tue 23 Jun open: this run started after Monday's open"
    assert note in root.report(3).split("## 3.")[1].split("## 4.")[0]
    assert "Orders execute at Tue 23 Jun open" in root.notifier.events[-1].message  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("started", "expected", "moved"),
    [
        # 2026-W04 ends Fri 23 Jan; Mon 26 Jan is Republic Day.
        (datetime(2026, 1, 26, 8, 30, tzinfo=IST), date(2026, 1, 27), False),
        (datetime(2026, 1, 26, 11, 0, tzinfo=IST), date(2026, 1, 27), False),
        (datetime(2026, 1, 27, 10, 0, tzinfo=IST), date(2026, 1, 28), True),
        # The 09:15 edge, W25 (ends Fri 19 Jun).
        (datetime(2026, 6, 22, 9, 14, 59, tzinfo=IST), date(2026, 6, 22), False),
        (datetime(2026, 6, 22, 9, 15, 0, tzinfo=IST), date(2026, 6, 23), True),
        (datetime(2026, 6, 22, 9, 15, 1, tzinfo=IST), date(2026, 6, 23), True),
        # A Saturday preview executes at Monday's open, as before.
        (datetime(2026, 6, 20, 8, 0, tzinfo=IST), date(2026, 6, 22), False),
    ],
)
def test_f1_execution_session_edges(started: datetime, expected: date, moved: bool) -> None:
    week_ending = date(2026, 1, 23) if started.month == 1 else date(2026, 6, 19)
    session, note = execution_session(week_ending, CALENDAR, started)
    assert session == expected and (note is not None) is moved
    assert execution_session(week_ending, CALENDAR, None) == (
        CALENDAR.next_session_after(week_ending),
        None,
    )


def test_f1_a_catch_up_on_tuesday_fills_catch_up_weeks_historically_and_flags_them(
    tmp_path: Path,
) -> None:
    """A 3-week catch-up (W23-W25) started Tuesday 10:00 of W26. The stop
    decided in the catch-up week W24 fills at W25's Monday open — its own
    historical session — flagged catch_up (not late_fill). The final week's
    stop (second book) fills at Wednesday's open, not flagged."""
    early = Root.create(tmp_path / "early")
    early.standard_cache(a=falling(early.first_session(1), rate=0.05))
    early.seed_entry()
    tuesday = datetime.combine(early.first_session(4) + timedelta(days=1), time(10), IST)
    assert early.run("--as-of", early.as_of(3), now=lambda: tuesday) == EXIT_OK
    conn = early.repo().database.connect()
    fill = conn.execute(
        "SELECT session, price, catch_up, late_fill FROM stock_fills WHERE action = 'SELL_ALL'"
    ).fetchone()
    assert (fill["session"], fill["price"], fill["catch_up"], fill["late_fill"]) == (
        "2026-06-15",
        "598.74",
        1,
        0,
    )
    buy = conn.execute("SELECT catch_up FROM stock_fills WHERE action = 'BUY_T1'").fetchone()
    assert buy["catch_up"] == 0  # decided by the earlier run, not this catch-up
    section = early.report(3).split("## 9.")[1]
    assert "**catch_up fills**" in section and "A SELL_ALL on 2026-06-15" in section
    assert "catch_up" in early.report(3).split("## 2.")[1].split("## 3.")[0]
    (row,) = journal.build_rows(early.repo())
    assert "SELL_ALL 2026-06-15: catch_up" in row["notes"]

    late = Root.create(tmp_path / "late")
    late.standard_cache(a=falling(late.first_session(2)))
    late.seed_entry()
    assert late.run("--as-of", late.as_of(3), now=lambda: tuesday) == EXIT_OK
    assert late.run("--as-of", late.as_of(4)) == EXIT_OK
    final = (
        late.repo()
        .database.connect()
        .execute("SELECT session, price, catch_up FROM stock_fills WHERE action = 'SELL_ALL'")
        .fetchone()
    )
    assert (final["session"], final["price"], final["catch_up"]) == ("2026-06-24", "612.71", 0)


def test_f1_the_preview_uses_the_same_rule_with_its_own_clock(tmp_path: Path) -> None:
    from test_stock_fetch import _fetch, _preview, _setup

    root, dhan = _setup(tmp_path)
    monday_noon = datetime(2026, 9, 28, 12, 0, tzinfo=IST)
    assert _fetch(root, dhan, now=lambda: monday_noon) == EXIT_OK
    text = _preview(root).read_text()
    assert "orders execute at Tue 29 Sep open: this run started after Monday's open" in text


# ======================================================================= F4
def _config_copy(tmp_path: Path) -> Path:
    import shutil

    root = tmp_path / "config"
    shutil.copytree(REPO / "config", root)
    return root


def _strategy(root: Path) -> Path:
    return root / "strategies" / "positional_stocks" / "wsr1_weekly_stochrsi.yaml"


@pytest.mark.parametrize(
    ("key", "value"),
    [("fetch_minutes", 0), ("decide_minutes", -5), ("fetch_requests_per_second", 0)],
)
def test_f4_deadlines_and_the_rate_must_be_positive(tmp_path: Path, key: str, value: int) -> None:
    root = _config_copy(tmp_path)
    data = yaml.safe_load(_strategy(root).read_text())
    data["parameters"]["deadlines"][key] = value
    _strategy(root).write_text(yaml.safe_dump(data))
    with pytest.raises(RunRefused, match=f"deadlines.{key} must be > 0"):
        RunConfig.from_config(root)


@pytest.mark.parametrize("which", ["runtime", "strategy"])
def test_f4_a_duplicate_key_is_refused(tmp_path: Path, which: str) -> None:
    root = _config_copy(tmp_path)
    path = root / "runtimes" / "positional_stocks.yaml" if which == "runtime" else _strategy(root)
    path.write_text(path.read_text() + "enabled: true\n")
    with pytest.raises(RunRefused, match="duplicate key 'enabled' on line"):
        RunConfig.from_config(root)


# ============================================================== D128 (note)
def test_an_older_started_first_week_is_refused_with_one_instruction(tmp_path: Path) -> None:
    """The audit's p4h_started_gap: STARTED W30, nothing COMPLETED, now W39."""
    root = Root.create(tmp_path)
    root.standard_cache()
    repo = root.repo()
    w30 = (2026, 30)
    repo.mark_started(root.calendar.expected_last_session(w30), w30, "crashed")
    repo.database.close()
    before = _sha(root.db)
    friday = datetime.combine(date(2026, 9, 25), time(20, 0), IST)
    assert root.run("--as-of", "auto", now=lambda: friday) == EXIT_REFUSED
    (line,) = [x for x in root.output if x.startswith("REFUSED")]
    assert "STARTED first week 2026-W30 is older than the latest completed week, 2026-W39" in line
    assert f"mv {root.db} {root.db}.abandoned-20260925T143000Z" in line
    assert _sha(root.db) == before
    assert not any(": COMPLETED" in x for x in root.output)
    assert len(list(root.reports.glob("*-refused.md"))) == 1


def test_a_started_first_week_that_is_the_latest_completed_week_is_redone(
    tmp_path: Path,
) -> None:
    root = Root.create(tmp_path)
    root.standard_cache()
    repo = root.repo()
    w39 = (2026, 39)
    repo.mark_started(date(2026, 9, 25), w39, "crashed")
    repo.database.close()
    friday = datetime.combine(date(2026, 9, 25), time(20, 0), IST)
    assert root.run("--as-of", "auto", now=lambda: friday) == EXIT_OK
    assert root.repo().run_status(date(2026, 9, 25)) == "COMPLETED"


# ============================================================== D129
def test_check_config_reports_ok_on_the_committed_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert check_config.main(["--config-root", str(REPO / "config")]) == 0
    assert capsys.readouterr().out.startswith("OK:")


def test_check_config_names_a_broken_wsr1_file_and_warns_about_the_paper_runtimes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _config_copy(tmp_path)
    _strategy(root).write_text(_strategy(root).read_text() + "parameters: [unclosed\n")
    assert check_config.main(["--config-root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "wsr1_weekly_stochrsi.yaml" in out
    assert "intraday_options and positional_options would refuse to start" in out


def test_check_config_catches_a_duplicate_key_and_a_live_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _config_copy(tmp_path)
    text = _strategy(root).read_text().replace("mode: paper", "mode: live")
    _strategy(root).write_text(text + "live_approved: false\n")
    assert check_config.main(["--config-root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "duplicate key 'live_approved'" in out
    assert "live-config guard:" in out
