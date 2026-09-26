"""Phase 4b-1: the weekly report, one test per spec 11 bullet (v1.2m).

Most tests read the report a real run wrote. Where a synthetic cache cannot
produce a case (a watchlist entry, a silent freeze lift, a "partial sold 0"),
the run's own :class:`ReportData` is captured and re-rendered with that case
added — the renderer is the thing under test there.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from _stock_run_fixtures import Root, falling, wobble
from _wsr1_rules_fixtures import (
    PARAMS0,
    book,
    ctx,
    index_series,
    kd_tape,
    open_position,
    symbol_week,
)

from runtimes.positional_stocks import weekly_run
from runtimes.positional_stocks.accounting import CorporateActionEvent
from runtimes.positional_stocks.report import ReportData, render
from runtimes.positional_stocks.telegram_summary import operator_actions, summary_text
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    PositionReview,
    WatchEntry,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import watchlist


@pytest.fixture
def stopped(tmp_path: Path) -> Root:
    """T1 40 @ 1,000 in week 1, a smooth fall, the stop decided in week 3 and
    filled in week 4. B has a second entry pending for later."""
    root = Root.create(tmp_path)
    root.standard_cache(a=falling(root.first_session(2)))
    root.seed_entry()
    root.seed_entry("B", industry="Chemicals", execute_on=date(2026, 12, 31), mark_week=False)
    for n in (1, 2, 3, 4):
        assert root.run("--as-of", root.as_of(n)) == 0, root.output
    return root


def _section(report: str, n: int) -> str:
    return (
        report.split(f"## {n}.")[1].split(f"## {n + 1}.")[0]
        if n < 10
        else report.split("## 10.")[1]
    )


def _capture(root: Root, monkeypatch: pytest.MonkeyPatch, n: int) -> ReportData:
    captured: list[ReportData] = []
    real = weekly_run.render

    def spy(data: ReportData) -> str:
        captured.append(data)
        return real(data)

    monkeypatch.setattr(weekly_run, "render", spy)
    assert root.run("--as-of", root.as_of(n), "--dry-run") == 0
    return captured[-1]


# ------------------------------------------------ 1. regime, equity, brakes
def test_1_regime_equity_drawdown_and_brakes(stopped: Root) -> None:
    section = _section(stopped.report(4), 1)
    for text in (
        "Regime: **normal**",
        "Equity:",
        "Peak:",
        "drawdown 1.3",
        "Brake 1: off",
        "Brake 2: off",
    ):
        assert text in section


# -------------------------------------------------------------- 2. fills
def test_2_fills_since_the_last_run(stopped: Root) -> None:
    section = _section(stopped.report(1), 2)
    assert f"| {stopped.first_session(1)} | A | BUY_T1 | 40 | 1,000.00 | 48.00 |" in section


# ------------------------------------------------ 3. pending with levels
def test_3_pending_orders_with_levels(stopped: Root) -> None:
    section = _section(stopped.report(3), 3)
    assert "| A | SELL_ALL |" in section
    assert "P1 1,000.00 · L1 900.00 · L2 800.00 · Stop 700.00" in section
    assert "| B | BUY_T1 | 2026-12-31 | 40,000.00 | s 10.00% — levels set at the fill |" in section


# ---------------------------------------------------- 4. open positions
def test_4_open_positions(stopped: Root) -> None:
    section = _section(stopped.report(2), 4)
    assert "| A | OPEN | 40 | 1,000.00 | 10.00% | 900.00 | 800.00 | 700.00 |" in section
    row = next(line for line in section.splitlines() if line.startswith("| A |"))
    assert row.split("|")[11].strip() == "1"  # weeks held since the T1 fill week


# ---------------------------------------------------- 5. closed trades
def test_5_closed_trades_with_planned_and_actual_loss(stopped: Root) -> None:
    section = _section(stopped.report(4), 5)
    assert "**A** (A-2026W23) closed" in section and "stop: weekly close" in section
    assert "planned loss at Stop 700.00: -12,000.00" in section


# ------------------------------------------------------------- 6. funnel
def test_6_the_trigger_funnel(stopped: Root) -> None:
    section = _section(stopped.report(4), 6)
    assert "Triggered 0 → passed filters 0 → taken 0; not armed 1." in section
    assert "**undefined**" in section and "— flat stoch range" in section


# ---------------------------------------------------------- 7. watchlist
def test_7_the_watchlist_rule() -> None:
    """Armed, K <= D, K < 50, filters pass; not armed or K >= 50 is out."""
    armed_below = kd_tape([(15.0, 18.0), (20.0, 22.0), (25.0, 28.0)]).series()
    armed_high = kd_tape([(15.0, 18.0), (45.0, 40.0), (52.0, 55.0)]).series()
    not_armed = kd_tape([(40.0, 45.0), (41.0, 45.0), (42.0, 45.0)]).series()
    symbols = {
        "X": symbol_week("X", armed_below),
        "Y": symbol_week("Y", armed_high),
        "Z": symbol_week("Z", not_armed),
    }
    entries = watchlist(symbols, index_series(), book(), ctx(), PARAMS0)
    assert [(e.symbol, e.k, e.d) for e in entries] == [("X", 25.0, 28.0)]
    # A held symbol is never on it.
    held = open_position("X", atr_pct=0.06, p1=1000.0, fill_week=190)
    assert watchlist(symbols, index_series(), book(held), ctx(), PARAMS0) == []


def test_7_the_watchlist_section(stopped: Root, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _capture(stopped, monkeypatch, 5)
    data = replace(data, watchlist=(WatchEntry("C", 3.25, 22.5, 24.0),))
    assert "| 1 | C | +3.25 pp | 22.50 | 24.00 |" in _section(render(data), 7)


# -------------------------------------------------- 8. corporate actions
def test_8_a_frozen_position_prints_both_resolution_lines(tmp_path: Path) -> None:
    root = Root.create(tmp_path)

    def bonus(day: date) -> tuple[float, float]:
        return (500.0, 500.0) if day >= root.first_session(2) else (1000.0, 1000.0)

    root.standard_cache(a=bonus)
    root.seed_entry()
    for n in (1, 2, 3, 4):
        assert root.run("--as-of", root.as_of(n)) == 0
    section = _section(root.report(4), 8)
    ex = root.first_session(2)
    assert (
        "**⚠ ESCALATED — operator action.** A (A-2026W23): factor 0.5000, frozen 3 run(s)"
        in section
    )
    assert f"    A,{ex},0.5000,2026-09-25,real move: <why>" in section
    assert (
        f"    A,{ex},BONUS_SPLIT,<new shares per old share, ~2.00; check the exchange "
        "record>,2026-09-25,<note>"
    ) in section
    assert "**Real move only**" in section and "**Corporate action**" in section
    assert isinstance(root.notifier.events[-1].message, str)
    assert "frozen 1, escalated 1" in root.notifier.events[-1].message


def test_8_a_silent_freeze_lift_is_flagged(stopped: Root, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _capture(stopped, monkeypatch, 5)
    lift = CorporateActionEvent("silent_lift", "A", "A-2026W23", "frozen last run, not frozen now")
    last = data.weeks[-1]
    data = replace(
        data,
        weeks=(
            *data.weeks[:-1],
            replace(last, outcome=replace(last.outcome, silent_lifts=(lift,))),
        ),
    )
    section = _section(render(data), 8)
    assert (
        "**⚠ A (A-2026W23) — freeze lifted with neither an acknowledgement nor a rescale:**"
        in section
    )
    assert operator_actions(data).silent_lifts == 1
    assert "silent freeze lifts 1" in summary_text(data)


# ------------------------------------------- 9. fill flags, partials, gaps
def test_9_a_late_fill_is_flagged(tmp_path: Path) -> None:
    root = Root.create(tmp_path)
    root.standard_cache()
    root.seed_entry()
    week1 = {d for d in root.sessions() if root.first_session(1) <= d <= root.last_session(1)}
    root.write("A", wobble(1000.0), skip=week1)  # week 1's candles missing at run 1
    assert root.run("--as-of", root.as_of(1)) == 0
    root.write("A", wobble(1000.0))  # ... and back for run 2
    assert root.run("--as-of", root.as_of(2)) == 0
    section = _section(root.report(2), 9)
    assert f"A BUY_T1 on {root.first_session(1)}: late_fill" in section


def test_9_partial_sold_0_and_gaps(stopped: Root, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _capture(stopped, monkeypatch, 5)
    review = PositionReview(
        "A-X", "C", "partial with 1 share: nothing sold", None, ("partial sold 0",)
    )
    data = replace(data, decision=replace(data.decision, reviews=(review,)))
    section = _section(render(data), 9)
    assert "- C (A-X): partial with 1 share: nothing sold" in section
    assert "**Blocking gaps**" in section and "**Gaps ≥ 15% this week:**" in section


def test_9_a_blocking_gap_is_listed(tmp_path: Path) -> None:
    root = Root.create(tmp_path)
    root.standard_cache()
    jump = root.first_session(-8)
    root.write("C", lambda d: (650.0, 650.0) if d >= jump else (500.0, 500.0))
    root.seed_entry()
    assert root.run("--as-of", root.as_of(1)) == 0
    assert f"- C {jump} ratio 1.3000" in _section(root.report(1), 9)


# ----------------------------------------------------------- 10. warnings
def test_10_data_and_input_warnings(stopped: Root) -> None:
    section = _section(stopped.report(4), 10)
    assert "results_calendar.csv has no rows" in section
    assert "Telegram: sent via recording" in section


def test_the_report_carries_decision_warnings(
    stopped: Root, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _capture(stopped, monkeypatch, 5)
    last = data.weeks[-1]
    assert last.outcome.decision is not None
    decision = replace(last.outcome.decision, warnings=("sector X: 5 held, over the limit of 4",))
    data = replace(
        data,
        weeks=(*data.weeks[:-1], replace(last, outcome=replace(last.outcome, decision=decision))),
    )
    assert "- sector X: 5 held, over the limit of 4" in _section(render(data), 10)


def test_9_many_blocking_gaps_in_one_symbol_are_summarised(
    stopped: Root, monkeypatch: pytest.MonkeyPatch
) -> None:
    from strategies.positional_stocks.wsr1_weekly_stochrsi.gaps import scan
    from strategies.positional_stocks.wsr1_weekly_stochrsi.models import DailyBar

    closes = [100.0, 300.0, 100.0, 300.0, 100.0]
    bars = [DailyBar(date(2017, 1, 2 + i), c, c, c, c, 1.0) for i, c in enumerate(closes)]
    gaps = tuple(scan("P", bars))
    data = replace(_capture(stopped, monkeypatch, 5), blocking_gaps=gaps)
    section = _section(render(data), 9)
    assert "- P: 4 gaps from 2017-01-03 to 2017-01-06 (latest ratio 0.3333)" in section
