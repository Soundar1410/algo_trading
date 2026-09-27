"""Phase 5-fix2 (audit round 8, spec v1.3.1).

* R8-2 (D130): catch-up weeks are defined by the run's clock. Only the latest
  completed week at the run's start executes after the run; every older week
  is a catch-up week however the invocations are split.
* R8-1 (D131): any error loading or binding either positional_stocks YAML is
  a clean refusal with a report and an alert, never a traceback.
* R8-3 (D132): check_config warns about the paper runtimes only when their
  own strategy load fails.
* R8-4 (D133): the execution note states the run's own start.
"""

from __future__ import annotations

import shutil
from datetime import date, datetime, time
from pathlib import Path

import pytest
import yaml
from _stock_run_fixtures import IST, REPO, Root, falling

from common.notifications.base import RecordingNotifier
from runtimes.positional_stocks import check_config, weekly_run
from runtimes.positional_stocks.run_config import load_run_config
from runtimes.positional_stocks.week_inputs import execution_session
from runtimes.positional_stocks.weekly_run import EXIT_OK, EXIT_REFUSED
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar

CALENDAR = TradingCalendar.from_config(REPO / "config")
STRATEGY = Path("strategies") / "positional_stocks" / "wsr1_weekly_stochrsi.yaml"


def _dump(root: Root) -> dict[str, list[tuple[object, ...]]]:
    repo = root.repo()
    try:
        return repo.dump()
    finally:
        repo.database.close()


# =================================================================== R8-2
def _missed_weeks(tmp_path: Path, *, split: bool) -> Root:
    """The audit's p4i_asof_cmp: W1-W2 decided on time; W3-W7 missed; the
    operator acts at Monday 10:00 of week 8 — with plain ``auto``, or with
    ``--as-of W3`` first and then ``auto``, both at that clock."""
    root = Root.create(tmp_path)
    root.standard_cache(a=falling(root.first_session(2)))
    root.seed_entry()
    for n in (1, 2):
        assert root.run("--as-of", root.as_of(n)) == EXIT_OK
    clock = datetime.combine(root.first_session(8), time(10), IST)
    if split:
        assert root.run("--as-of", root.as_of(3), now=lambda: clock) == EXIT_OK
    assert root.run("--as-of", "auto", now=lambda: clock) == EXIT_OK
    return root


def test_r8_2_splitting_a_catch_up_gives_the_same_book(tmp_path: Path) -> None:
    plain = _missed_weeks(tmp_path / "plain", split=False)
    split = _missed_weeks(tmp_path / "split", split=True)
    assert _dump(plain) == _dump(split)
    conn = plain.repo().database.connect()
    fill = conn.execute(
        "SELECT session, price, catch_up FROM stock_fills WHERE action = 'SELL_ALL'"
    ).fetchone()
    assert (fill["session"], fill["price"], fill["catch_up"]) == ("2026-06-22", "664.83", 1)
    assert conn.execute("SELECT net_pnl FROM stock_positions").fetchone()[0] == "-13499.05"


def test_r8_2_catch_up_equals_sequential_runs_with_orders_in_the_catch_up_weeks(
    tmp_path: Path,
) -> None:
    """Spec 14, not vacuous: the stop is decided inside catch-up week W24."""
    books = []
    for name, split in (("one", False), ("three", True)):
        root = Root.create(tmp_path / name)
        root.standard_cache(a=falling(root.first_session(1), rate=0.05))
        root.seed_entry()
        clock = datetime.combine(root.first_session(4), time(8, 30), IST)
        weeks = (1, 2, 3) if split else (3,)
        for n in weeks:
            assert root.run("--as-of", root.as_of(n), now=lambda c=clock: c) == EXIT_OK
        books.append(_dump(root))
        fill = (
            root.repo()
            .database.connect()
            .execute("SELECT session, catch_up FROM stock_fills WHERE action = 'SELL_ALL'")
            .fetchone()
        )
        assert (fill["session"], fill["catch_up"]) == ("2026-06-15", 1)
    assert books[0] == books[1]


def test_r8_2_the_on_time_path_is_unchanged(tmp_path: Path) -> None:
    """Each week decided at its own Monday 08:30: nothing is a catch-up."""
    root = Root.create(tmp_path)
    root.standard_cache(a=falling(root.first_session(2)))
    root.seed_entry()
    for n in (1, 2, 3, 4):
        assert root.run("--as-of", root.as_of(n)) == EXIT_OK
    conn = root.repo().database.connect()
    assert conn.execute("SELECT count(*) FROM stock_fills WHERE catch_up = 1").fetchone()[0] == 0
    fill = conn.execute(
        "SELECT session, price FROM stock_fills WHERE action = 'SELL_ALL'"
    ).fetchone()
    assert (fill["session"], fill["price"]) == ("2026-06-22", "664.83")


# =================================================================== R8-1
def _with_config(tmp_path: Path) -> Root:
    root = Root.create(tmp_path)
    root.standard_cache()
    for relative in (Path("runtimes") / "positional_stocks.yaml", STRATEGY):
        (root.path / "config" / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "config" / relative, root.path / "config" / relative)
    return root


def _break(root: Root, how: str, monkeypatch: pytest.MonkeyPatch) -> None:
    path = root.path / "config" / STRATEGY
    if how == "invalid_date":
        path.write_text(
            path.read_text().replace("brake_2_cleared_on: null", "brake_2_cleared_on: 2026-02-30")
        )
    elif how == "list_key":
        path.write_text(path.read_text() + "? [a, b]\n: 1\n")
    else:
        # A non-YAML exception inside the positional_stocks strict parse only;
        # every other YAML read (the calendar, the shared loader) is untouched.
        real = yaml.load

        def boom(stream: object, Loader: type) -> object:
            if Loader.__name__ == "_Strict":
                raise RuntimeError("injected loader failure")
            return real(stream, Loader=Loader)

        monkeypatch.setattr(yaml, "load", boom)


@pytest.mark.parametrize("how", ["invalid_date", "list_key", "injected"])
@pytest.mark.parametrize("mode", ["decide", "fetch", "dry_run"])
def test_r8_1_a_yaml_load_error_is_a_clean_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str, mode: str
) -> None:
    root = _with_config(tmp_path)
    _break(root, how, monkeypatch)
    config, error = load_run_config(root.path / "config")
    # The runtime file is parsed first, so an injected loader failure names it.
    named = "positional_stocks.yaml" if how == "injected" else "wsr1_weekly_stochrsi.yaml"
    assert error is not None and named in error
    argv = {"decide": ["--mode", "decide"], "fetch": ["--mode", "fetch"]}.get(
        mode, ["--mode", "decide", "--dry-run"]
    )
    env = root.env(config=config, config_error=error)
    assert weekly_run.main(argv, env) == EXIT_REFUSED  # no traceback
    (line,) = [x for x in root.output if x.startswith("REFUSED")]
    assert named in line
    assert isinstance(root.notifier, RecordingNotifier)
    if mode == "dry_run":
        assert root.notifier.events == [] and not root.reports.exists()
    else:
        assert len(root.notifier.events) == 1
        suffix = "-preview-failed.md" if mode == "fetch" else "-refused.md"
        assert len(list(root.reports.glob(f"*{suffix}"))) == 1


@pytest.mark.parametrize("how", ["invalid_date", "list_key", "injected"])
def test_r8_1_check_config_reports_a_yaml_load_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], how: str
) -> None:
    root = tmp_path / "config"
    shutil.copytree(REPO / "config", root)
    fake = Root(tmp_path, CALENDAR)
    _break(fake, how, monkeypatch)
    assert check_config.main(["--config-root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "PROBLEM: positional_stocks: configuration:" in out
    named = "positional_stocks.yaml" if how == "injected" else "wsr1_weekly_stochrsi.yaml"
    assert named in out.split("PROBLEM: positional_stocks: configuration:")[1]


# =================================================================== R8-3
def test_r8_3_a_wsr1_only_problem_does_not_warn_about_the_paper_runtimes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "config"
    shutil.copytree(REPO / "config", root)
    path = root / STRATEGY
    path.write_text(
        path.read_text().replace("strategy_id: wsr1_weekly_stochrsi", "strategy_id: wsr2")
    )
    assert check_config.main(["--config-root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "runtime positional_stocks: the shared config loader refuses" in out
    assert "would refuse to start" not in out


def test_r8_3_a_syntax_error_still_warns_about_the_paper_runtimes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "config"
    shutil.copytree(REPO / "config", root)
    (root / STRATEGY).write_text((root / STRATEGY).read_text() + "parameters: [unclosed\n")
    assert check_config.main(["--config-root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "runtime intraday_options:" in out
    assert "intraday_options and positional_options would refuse to start" in out


# =================================================================== R8-4
@pytest.mark.parametrize(
    ("started", "note"),
    [
        (
            datetime(2026, 6, 23, 10, 0, tzinfo=IST),
            "orders execute at Wed 24 Jun open: this run started Tue 23 Jun 10:00, "
            "after that session's open",
        ),
        (
            datetime(2026, 6, 25, 13, 30, tzinfo=IST),
            # Fri 26 Jun 2026 is a holiday (Muharram): Monday's open.
            "orders execute at Mon 29 Jun open: this run started Thu 25 Jun 13:30, "
            "after that session's open",
        ),
        (
            datetime(2026, 6, 25, 8, 0, tzinfo=IST),
            "orders execute at Thu 25 Jun open: this run started Thu 25 Jun 08:00, "
            "after Mon 22 Jun's open",
        ),
    ],
    ids=["tue-1000", "thu-mid-session", "thu-before-open"],
)
def test_r8_4_the_execution_note_states_the_runs_own_start(started: datetime, note: str) -> None:
    assert execution_session(date(2026, 6, 19), CALENDAR, started)[1] == note
