"""Phase 4a-fix: strict week order (10.2 v1.2j), late fills (8 v1.2j) and
spec 9's drawdown column, through real SQLite files.

Uses the one-symbol world of ``test_stock_accounting``: a trigger at 209, T1
filling at 210's Monday open of 1,000 (L1 900), 210's weekly low of 890
touching L1, and 211's close of 950 above 210's high of 940 — the reversal
that adds T2.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from _wsr1_rules_fixtures import Tape, friday, open_position, week
from test_stock_accounting import (
    CALENDAR,
    PARAMS,
    _decided,
    _inputs,
    _repo,
    _run,
    _trade_world,
)

from runtimes.positional_stocks.accounting import RunOrderError, run_decision_week
from runtimes.positional_stocks.repository import StockRepository, drawdown_pct, week_text
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import OrderAction
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import replay_touch_memory

D = Decimal


# ========================================================= strict week order
def test_a_skipped_week_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _trade_world()
    _run(repo, world, 209)
    before = repo.dump()
    with pytest.raises(RunOrderError, match=f"week {week_text(week(210))} is not COMPLETED"):
        _run(repo, world, 211)
    # Refused before anything was written, not even a STARTED row.
    assert repo.dump() == before
    assert _run(repo, world, 210).status == "COMPLETED"
    assert _run(repo, world, 211).status == "COMPLETED"


def test_the_first_run_may_be_any_week(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    assert _run(repo, _trade_world(), 211).status == "COMPLETED"


def test_a_crashed_first_run_can_be_redone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _trade_world()

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("crash")

    monkeypatch.setattr(StockRepository, "save_decision", boom)
    with pytest.raises(RuntimeError):
        _run(repo, world, 209)
    monkeypatch.undo()
    assert repo.run_status(friday(209)) == "STARTED"
    assert _run(repo, world, 209).status == "COMPLETED"


# ================================================================ late fills
def test_a_late_candle_flags_the_fill_and_replays_the_touch(tmp_path: Path) -> None:
    world = _trade_world()

    on_time = _repo(tmp_path / "on_time.db")
    _run(on_time, world, 209)
    _run(on_time, world, 210)
    reference = _decided(on_time, world, 211)

    repo = _repo(tmp_path / "late.db")
    _run(repo, world, 209)
    # Run 210: the cache does not have week 210's candles yet.
    missing = _inputs(world, 210)
    missing = replace(missing, daily={"A": world.daily(209)})
    run_decision_week(repo, missing, calendar=CALENDAR, params=PARAMS)
    assert repo.positions() == {} and len(repo.pending_orders()) == 1

    # Run 211: 210's Monday candle has arrived. T1 fills there — a week late.
    late = _decided(repo, world, 211)
    (position,) = repo.positions().values()
    assert position.buys[0].session == friday(210) - timedelta(days=4)
    row = (
        repo.database.connect()
        .execute("SELECT late_fill, not_traded_on_execution_session FROM stock_fills")
        .fetchone()
    )
    assert (row["late_fill"], row["not_traded_on_execution_session"]) == (1, 0)
    # Week 210's low of 890 touched L1 900; without the replay, 211 (low 940)
    # would say "L1 not touched" and no T2 would be decided.
    assert position.touch_week == week(210)
    assert [o.action for o in late.orders] == [o.action for o in reference.orders]
    assert [o.action for o in late.orders] == [OrderAction.BUY_T2]


def test_an_on_time_fill_is_not_late(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _trade_world()
    _run(repo, world, 209)
    _run(repo, world, 210)
    flag = repo.database.connect().execute("SELECT late_fill FROM stock_fills").fetchone()
    assert flag["late_fill"] == 0


# ================================================================== drawdown
def test_drawdown_pct_is_written_every_week(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _trade_world()
    for i in range(209, 213):
        _run(repo, world, i)
    rows = (
        repo.database.connect()
        .execute("SELECT equity, peak, drawdown_pct FROM stock_equity ORDER BY week_ending")
        .fetchall()
    )
    assert len(rows) == 4
    for row in rows:
        assert D(row["drawdown_pct"]) == drawdown_pct(D(row["equity"]), D(row["peak"]))
    # 210 marks 40 shares at 900 against a 1,000 fill: below the peak.
    assert D(rows[1]["drawdown_pct"]) > 0


def test_drawdown_pct_arithmetic() -> None:
    assert drawdown_pct(D("900000"), D("1000000")) == D("10.00")
    assert drawdown_pct(D("1000000"), D("1000000")) == D("0.00")
    assert drawdown_pct(D("1100000"), D("1000000")) == D("0.00")
    assert drawdown_pct(D("996123.45"), D("1000000")) == D("0.39")


# ======================================================== the replay, pure
def _tape(low: dict[int, float], close: dict[int, float] | None = None) -> Tape:
    return Tape(n=215, low=low, close={**{j: 950.0 for j in range(215)}, **(close or {})})


def test_replay_touches_in_the_fill_week_when_filled_at_the_open() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=210)  # L1 900
    series = _tape({210: 890.0}).series()
    assert replay_touch_memory(position, series, before=week(212)).touch_week == week(210)


def test_replay_skips_the_fill_week_after_a_midweek_fill() -> None:
    # v1.2g: 210's low may predate a Wednesday fill; the window opens at 211.
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=210, at_week_open=False)
    assert (
        replay_touch_memory(position, _tape({210: 890.0}).series(), before=week(212)).touch_week
        is None
    )
    series = _tape({210: 890.0, 211: 895.0}).series()
    assert replay_touch_memory(position, series, before=week(212)).touch_week == week(211)


def test_replay_clears_on_a_close_at_or_above_p1_and_stops_before_the_run_week() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=210)
    cleared = _tape({210: 890.0}, close={211: 1000.0}).series()
    assert replay_touch_memory(position, cleared, before=week(212)).touch_week is None
    # The run week itself (212) is the normal decision's, not the replay's.
    only_run_week = _tape({212: 890.0}).series()
    assert replay_touch_memory(position, only_run_week, before=week(212)).touch_week is None
