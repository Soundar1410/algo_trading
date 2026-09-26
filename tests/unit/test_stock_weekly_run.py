"""Phase 4b-1: ``weekly_run --mode decide`` end to end on a synthetic root.

Every test runs the real CLI entry point (:func:`weekly_run.main`) against a
temp project root: real calendar, real cache files, real SQLite. Only the
clock, the notifier and the two environment checks are injected.
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
import textwrap
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from _stock_run_fixtures import REPO, Root, falling, week, wobble
from filelock import FileLock

from common.notifications.base import NotificationEvent, RecordingNotifier
from runtimes.positional_stocks import backup, weekly_run
from runtimes.positional_stocks.accounting import RunOrderError
from runtimes.positional_stocks.run_config import RunConfig
from runtimes.positional_stocks.weekly_run import (
    EXIT_DEADLINE,
    EXIT_LOCKED,
    EXIT_NO_TRADES,
    EXIT_OK,
    EXIT_REFUSED,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.inputs import (
    InputFileError,
    load_corporate_actions,
)


@pytest.fixture
def root(tmp_path: Path) -> Root:
    r = Root.create(tmp_path)
    r.standard_cache(a=falling(r.first_session(2)))
    r.seed_entry()
    return r


def _dump(root: Root) -> dict[str, list[tuple[object, ...]]]:
    repo = root.repo()
    try:
        return repo.dump()
    finally:
        repo.database.close()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ================================================================ offline
def test_decide_completes_with_every_network_class_raising(
    root: Root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec 10.3: no scrip master, no AuthBootstrap, no historical client, no
    market-feed adapter. Each raises on construction or use."""
    from common.authentication.bootstrap import AuthBootstrap
    from common.market_data.dhan import DhanMarketFeedAdapter
    from common.market_data.dhan_historical import DhanHistoricalDataClient
    from common.market_data.scrip_master import ScripMasterCache

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("decide mode must stay offline")

    for cls in (DhanHistoricalDataClient, ScripMasterCache, AuthBootstrap, DhanMarketFeedAdapter):
        monkeypatch.setattr(cls, "__init__", forbidden)
    monkeypatch.setattr(AuthBootstrap, "get_token", forbidden)
    for n in (1, 2, 3, 4):
        assert root.run("--as-of", root.as_of(n)) == EXIT_OK


NETWORK_MODULES = (
    "common.market_data.dhan_historical",
    "common.market_data.scrip_master",
    "common.market_data.dhan",
    "common.retention",
    "dhanhq",
    "runtimes.intraday_options",
    "runtimes.positional_options",
    "orchestration.auto_start.controller",
    "orchestration.auto_start.__main__",
)


def test_decide_never_loads_a_network_or_supervisor_module(tmp_path: Path) -> None:
    """In a fresh interpreter: a decide run — plus the two environment checks'
    own modules, imported as the real preflight does — loads none of the
    modules that fetch, resolve instruments, back up via common.retention or
    start a worker. (AuthBootstrap's module *is* loaded, by the shared
    ``orchestration.auto_start`` package the paper-safety check lives in; it is
    never constructed — see the test above.)"""
    script = textwrap.dedent(
        f"""
        import sys
        sys.path[:0] = [{str(REPO)!r}, {str(REPO / "tests" / "unit")!r}]
        from pathlib import Path
        from _stock_run_fixtures import Root, falling
        import orchestration.auto_start.paper_safety, scripts.validate_environment
        root = Root.create(Path({str(tmp_path)!r}))
        root.standard_cache(a=falling(root.first_session(2)))
        root.seed_entry()
        for n in (1, 2, 3, 4):
            assert root.run("--as-of", root.as_of(n)) == 0, root.output
        bad = {NETWORK_MODULES!r}
        print(sorted(m for m in sys.modules if any(m == b or m.startswith(b + ".") for b in bad)))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=REPO, check=True
    )
    assert result.stdout.strip().splitlines()[-1] == "[]", result.stdout + result.stderr


# ============================================================ refusals
def test_fetch_mode_is_not_built_yet(root: Root, capsys: pytest.CaptureFixture[str]) -> None:
    assert weekly_run.main(["--mode", "fetch"], root.env()) == EXIT_NO_TRADES
    assert "not built yet (Phase 4b-2)" in capsys.readouterr().out


def test_anything_but_paper_is_refused(root: Root) -> None:
    root.config = RunConfig(mode="live")
    assert root.run("--as-of", root.as_of(1)) == EXIT_REFUSED
    assert any("paper mode only" in line for line in root.output)


def test_a_failed_environment_check_refuses_before_anything(tmp_path: Path) -> None:
    root = Root.create(tmp_path)
    root.standard_cache()
    code = root.run("--as-of", root.as_of(1), preflight=lambda: ["validate_environment: x"])
    assert code == EXIT_REFUSED
    assert not root.db.exists() and not root.reports.exists()


def test_the_default_preflight_calls_both_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestration.auto_start import paper_safety
    from scripts import validate_environment

    calls: list[object] = []
    monkeypatch.setattr(
        validate_environment,
        "main",
        lambda argv: calls.append(argv) or validate_environment.EXIT_PROBLEMS,
    )
    monkeypatch.setattr(
        paper_safety,
        "verify_paper_only",
        lambda root, **kw: calls.append(kw) or paper_safety.PaperSafetyReport((), ("live on",)),
    )
    problems = weekly_run.default_preflight(Path("config"), "positional_stocks")
    assert calls[0] == ["--runtime-id", "positional_stocks"]
    assert calls[1] == {"check_legacy": False, "check_environment": False}
    assert len(problems) == 2 and "paper-safety: live on" in problems


# ============================================================ cold cache
def test_a_cold_cache_makes_no_trades_reports_and_alerts(tmp_path: Path) -> None:
    root = Root.create(tmp_path)
    root.standard_cache()
    # NIFTY lacks week 1's last session: not yet published.
    root.write("NIFTY", wobble(20000.0), skip={root.last_session(1)})
    root.seed_entry()
    before = _dump(root)
    assert root.run("--as-of", root.as_of(1)) == EXIT_NO_TRADES
    report = root.report(1)
    assert "NO TRADES (cold cache)" in report and "NIFTY 50 has no session" in report
    assert _dump(root) == before  # no run row, no fill
    assert isinstance(root.notifier, RecordingNotifier)
    assert [e.event_type for e in root.notifier.events] == ["weekly_run_no_trades"]


def test_one_stale_symbol_is_skipped_and_reported(tmp_path: Path) -> None:
    root = Root.create(tmp_path)
    root.standard_cache()
    root.write("B", wobble(800.0), skip={root.last_session(1)})
    root.seed_entry()
    assert root.run("--as-of", root.as_of(1)) == EXIT_OK
    report = root.report(1)
    assert "stale (1 week(s)) — B: last cached session" in report
    assert "skipped this week" in report
    funnel = report.split("## 6.")[1].split("## 7.")[0]
    assert "- B " not in funnel


def test_a_held_symbol_stale_two_weeks_is_an_operator_action(tmp_path: Path) -> None:
    root = Root.create(tmp_path)
    root.standard_cache()
    root.seed_entry()
    assert root.run("--as-of", root.as_of(1)) == EXIT_OK
    # A (held) stops trading after week 1: suspended.
    root.write("A", wobble(1000.0), until=root.last_session(1))
    assert root.run("--as-of", root.as_of(2)) == EXIT_OK
    assert "no bar 1 week(s)" in root.report(2)
    assert root.run("--as-of", root.as_of(3)) == EXIT_OK
    report = root.report(3)
    assert "⚠ no bar 2 week(s): cannot hit its stop" in report
    assert "a suspended stock cannot hit its stop" in report
    assert isinstance(root.notifier, RecordingNotifier)
    assert "stale 2+ weeks 1" in root.notifier.events[-1].message


# ======================================================= weeks and order
def test_the_same_week_twice_through_the_cli_changes_nothing(root: Root) -> None:
    assert root.run("--as-of", root.as_of(1)) == EXIT_OK
    before = _dump(root)
    snapshots = sorted(root.backups.glob("*.db"))
    assert root.run("--as-of", root.as_of(1)) == EXIT_OK
    assert root.output[-1].startswith("up to date")
    assert _dump(root) == before
    assert sorted(root.backups.glob("*.db")) == snapshots  # "up to date": no snapshot


def test_a_catch_up_over_3_weeks_equals_3_sequential_runs(tmp_path: Path) -> None:
    sequential = Root.create(tmp_path / "sequential")
    catch_up = Root.create(tmp_path / "catch_up")
    for r in (sequential, catch_up):
        r.standard_cache(a=falling(r.first_session(2)))
        r.seed_entry()
    for n in (1, 2, 3):
        assert sequential.run("--as-of", sequential.as_of(n)) == EXIT_OK
    assert catch_up.run("--as-of", catch_up.as_of(3)) == EXIT_OK
    assert [line.split(":")[0] for line in catch_up.output if ": COMPLETED" in line] == [
        f"{week(n)[0]}-W{week(n)[1]:02d}" for n in (1, 2, 3)
    ]
    assert _dump(catch_up) == _dump(sequential)


def test_a_week_whose_predecessor_is_not_completed_is_refused(
    root: Root, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the run to skip week 1: spec 10.2 refuses, whatever the caller.
    monkeypatch.setattr(weekly_run, "weeks_to_process", lambda latest, target: [week(2)])
    with pytest.raises(RunOrderError, match="is not COMPLETED"):
        root.run("--as-of", root.as_of(2))


# ================================================================ lock
def test_a_second_concurrent_run_exits_without_touching_anything(root: Root) -> None:
    root.path.joinpath("data", "runtime", "locks").mkdir(parents=True)
    before = _sha(root.db)
    held = FileLock(str(root.path / "data" / "runtime" / "locks" / weekly_run.LOCK_NAME))
    with held:
        assert root.run("--as-of", root.as_of(1)) == EXIT_LOCKED
    assert "another weekly run holds the lock" in root.output[-1]
    assert _sha(root.db) == before
    assert not root.reports.exists() and not root.backups.exists()


# ============================================================= dry run
def test_a_dry_run_never_creates_the_database(tmp_path: Path) -> None:
    root = Root.create(tmp_path)
    root.standard_cache()
    assert root.run("--as-of", root.as_of(1), "--dry-run") == EXIT_OK
    assert not root.db.exists() and not root.backups.exists()
    report = root.report(1, dry_run=True)
    assert "DRY RUN" in report and "Telegram: not sent (dry run)" in report
    assert isinstance(root.notifier, RecordingNotifier) and root.notifier.events == []
    assert not (root.reports / f"{root.last_session(1)}.md").exists()


def test_a_dry_run_leaves_an_existing_database_byte_identical(root: Root) -> None:
    assert root.run("--as-of", root.as_of(1)) == EXIT_OK
    before = _sha(root.db)
    snapshots = sorted(root.backups.glob("*.db"))
    assert root.run("--as-of", root.as_of(3), "--dry-run") == EXIT_OK
    assert _sha(root.db) == before
    assert sorted(root.backups.glob("*.db")) == snapshots
    assert "SELL_ALL" in root.report(3, dry_run=True)  # the copy did run weeks 2-3


# ============================================================ telegram
class _Broken:
    channel = "telegram"

    def send(self, event: NotificationEvent) -> bool:
        raise ConnectionError("no route to host")


def test_a_telegram_failure_is_non_fatal_and_reported(root: Root) -> None:
    assert root.run("--as-of", root.as_of(1), notifier=_Broken()) == EXIT_OK
    repo = root.repo()
    assert repo.run_status(root.last_session(1)) == "COMPLETED"
    assert "Telegram: NOT sent — ConnectionError" in root.report(1)


# ============================================================ deadline
def test_an_expired_deadline_fails_closed(root: Root) -> None:
    ticks = iter([0.0, 10_000.0, 10_000.0, 10_000.0])
    code = root.run("--as-of", root.as_of(1), monotonic=lambda: next(ticks))
    assert code == EXIT_DEADLINE
    assert "NO TRADES (deadline exceeded)" in root.report(1)
    assert root.repo().run_status(root.last_session(1)) is None


# ============================================================== backup
def test_a_snapshot_is_taken_and_restorable_before_the_weeks_writes(root: Root) -> None:
    assert root.run("--as-of", root.as_of(1)) == EXIT_OK
    (snap,) = sorted(root.backups.glob("positional_stocks_*.db"))
    backup._verify(snap)  # opens clean and restores clean
    conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    weeks = [row[0] for row in conn.execute("SELECT iso_week FROM stock_weekly_runs")]
    conn.close()
    assert weeks == ["2026-W22"]  # taken before week 1 was written


def test_the_13th_snapshot_prunes_the_oldest(tmp_path: Path) -> None:
    db = tmp_path / "positional_stocks.db"
    sqlite3.connect(db).execute("CREATE TABLE t (x)").connection.close()
    made = [
        backup.snapshot(db, tmp_path / "b", keep=12, now=datetime(2026, 1, n, tzinfo=UTC))
        for n in range(1, 14)
    ]
    kept = sorted((tmp_path / "b").glob("positional_stocks_*.db"))
    assert len(kept) == 12 and made[0] not in kept and made[-1] in kept


def test_no_snapshot_on_a_first_run_or_a_dry_run(tmp_path: Path) -> None:
    root = Root.create(tmp_path)
    root.standard_cache()
    assert root.run("--as-of", root.as_of(1), "--dry-run") == EXIT_OK
    assert root.run("--as-of", root.as_of(1)) == EXIT_OK  # the DB did not exist yet
    assert not root.backups.exists()


# ========================================================= catch-up report
def test_a_catch_up_reports_each_week_and_telegrams_all_exits(root: Root) -> None:
    for n in (1, 2, 3):
        assert root.run("--as-of", root.as_of(n)) == EXIT_OK
    assert root.run("--as-of", root.as_of(6)) == EXIT_OK
    report = root.report(6)
    closed = report.split("## 5.")[1].split("## 6.")[0]
    first, *rest = closed.split("### ")[1:]
    assert first.startswith("2026-W26") and "**A** (A-2026W23) closed" in first
    assert all("None." in section for section in rest)
    assert "weeks processed: 2026-W26, 2026-W27, 2026-W28" in report
    assert isinstance(root.notifier, RecordingNotifier)
    message = root.notifier.events[-1].message
    assert "Weekly run — 2026-W26, 2026-W27, 2026-W28" in message
    assert "Exits: A stop -Rs" in message


# ======================================================= spec 8: stop loss
def test_a_stop_that_gaps_through_shows_planned_and_actual_loss(tmp_path: Path) -> None:
    """Spec 8: T1 40 @ 1,000, stop 700. Week 3 closes 690 (each day under 15%);
    week 4 opens at 600, through the stop. Planned loss at the Stop: -12,000;
    actual at the fill: -16,000."""
    root = Root.create(tmp_path)
    week3 = [850.0, 800.0, 760.0, 720.0, 690.0]

    def path(day: date) -> tuple[float, float]:
        if day >= root.first_session(4):
            return 600.0, 600.0
        if day >= root.first_session(3):
            i = min((day - root.first_session(3)).days, 4)
            return week3[max(i - 1, 0)] if i else 900.0, week3[i]
        if day >= root.first_session(2):
            return 900.0, 900.0
        return 1000.0, 1000.0

    root.standard_cache(a=path)
    root.seed_entry()
    for n in (1, 2, 3, 4):
        assert root.run("--as-of", root.as_of(n)) == EXIT_OK
    closed = root.report(4).split("## 5.")[1].split("## 6.")[0]
    assert "closed" in closed and "stop: weekly close 690.00 below stop 700.00" in closed
    assert "planned loss at Stop 700.00: -12,000.00" in closed
    assert "actual at the fill 600.00: -16,000.00 (before costs)" in closed
    assert "⚠ gapped through the stop" in closed


# =================================================== A4: the CSV template
def test_an_unedited_corporate_action_template_fails_closed(tmp_path: Path) -> None:
    from runtimes.positional_stocks.report import resolution_lines

    _, template = resolution_lines("A", date(2026, 6, 8), Decimal("0.5000"), date(2026, 9, 26))
    csv = tmp_path / "corporate_actions.csv"
    csv.write_text("symbol,ex_session,kind,ratio,confirmed_on,note\n" + template + "\n")
    with pytest.raises(InputFileError) as error:
        load_corporate_actions(csv)
    assert str(csv) in str(error.value) and "line 2" in str(error.value)


# ============================================ truncated-week warnings (4b)
def test_truncated_week_warnings_only_for_calendar_covered_years(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec 15, 4b: outside the verified holiday list every holiday Friday
    looks truncated; only a covered year's truncated week is worth a warning.
    The bars are identical either way."""
    from datetime import time as dtime
    from zoneinfo import ZoneInfo

    from strategies.positional_stocks.wsr1_weekly_stochrsi.models import DailyBar
    from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import (
        TradingCalendar,
    )
    from strategies.positional_stocks.wsr1_weekly_stochrsi.weekly_bars import build_weekly_bars

    calendar = TradingCalendar.from_holidays(["2026-01-26"])
    as_of = datetime.combine(date(2026, 9, 25), dtime(20, 0), ZoneInfo("Asia/Kolkata"))

    def thursday_only(day: date) -> list[DailyBar]:
        return [DailyBar(day, 100.0, 101.0, 99.0, 100.0, 1.0)]

    old = thursday_only(date(2020, 4, 9))  # Good Friday 2020: not in the 2026 list
    new = thursday_only(date(2026, 9, 17))
    with caplog.at_level("WARNING"):
        quiet = build_weekly_bars(old, as_of=as_of, calendar=calendar, warn_uncovered=False)
    assert quiet[0].is_truncated and not caplog.records
    with caplog.at_level("WARNING"):
        build_weekly_bars(new, as_of=as_of, calendar=calendar, warn_uncovered=False)
    assert len(caplog.records) == 1 and "2026-W38" in caplog.records[0].getMessage()
    assert build_weekly_bars(old, as_of=as_of, calendar=calendar) == quiet
