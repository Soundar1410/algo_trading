"""Phase 4b-1: the journal export (spec 11 v1.2m).

The header is compared with the spec's own code block, byte for byte. The
worked trade is the V1 plan's (spec 14): T1 40 @ 1,000, T2 32 @ 930, half 36
@ 1,120, the rest 36 @ 1,180 — driven through real decision weeks of the
accounting world, then exported.

Hand computation (spec 8 costs: 12 bps a buy; 11 bps + Rs 15 a sell):

* buys 40,000 + 29,760 = 69,760; fees 48.00 + 35.71
* sales 40,320 + 42,480 = 82,800; fees 44.35 + 15 and 46.73 + 15
* gross 13,040 (the V1 plan's figure); net 13,040 - 204.79 = 12,835.21
* average cost 69,760 / 72 = 968.89; 12,835.21 / A 100,000 = 12.84%
* T1 fill week 210 to exit week 216: 6 weeks
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from pathlib import Path

from _wsr1_rules_fixtures import week
from test_stock_accounting import _repo, _run, _trade_world

from runtimes.positional_stocks import journal
from runtimes.positional_stocks.repository import week_text

SPEC = (
    Path(__file__).resolve().parents[2]
    / "strategies/positional_stocks/wsr1_weekly_stochrsi/WSR1_WEEKLY_STOCH_RSI_SPEC.md"
)


def test_the_header_is_the_specs_line_exactly() -> None:
    text = SPEC.read_text(encoding="utf-8")
    block = text.split("**Journal export:**")[1].split("```")[1]
    assert block.strip() == journal.HEADER
    assert journal.render([]).splitlines() == [journal.HEADER]


def test_the_v1_worked_trade_row(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _trade_world()
    world.monday_open.update({214: 1120.0, 216: 1180.0})
    for i in range(209, 217):
        _run(repo, world, i)

    (row,) = journal.build_rows(repo)
    text = journal.render([row])
    assert list(csv.DictReader(io.StringIO(text))) == [row]

    expected = {
        "symbol": "A",
        "sector": "Sector-A",
        "promoter_group": "A",
        "event_risk": "no",
        "regime": "normal",
        "arm_week": week_text(week(207)),
        "trigger_week": week_text(week(209)),
        "K_trigger": "28.00",
        "D_trigger": "25.00",
        "close_trigger": "1000.00",
        "ema50_1w": "900.00",
        "perf6m_stock": "",
        "perf6m_nifty": "",
        "high_52w": "1100.00",
        "atr_pct": "6.00",
        "s": "10.00",
        "T1_price": "1000.00",
        "T1_shares": "40",
        "L1": "900.00",
        "L2": "800.00",
        "stop": "700.00",
        "T2_price": "930.00",
        "T2_shares": "32",
        "T3_date": "",
        "avg_cost": "968.89",
        "partial_price": "1120.00",
        "partial_shares": "36",
        "exit_price": "1180.00",
        "exit_shares": "36",
        "exit_type": "trail",
        "pnl_rs": "12835.21",
        "pnl_pct_of_A": "12.84",
        "weeks_held": "6",
        "rule_breaks": "",
        "notes": "",
        "screenshot": "",
    }
    assert {key: row[key] for key in expected} == expected
    assert Decimal(row["A"]) == Decimal("100000")
    assert row["trade_id"] == row["trade_id"].split()[0] and row["trade_id"].startswith("A-")


def test_notes_carry_fill_flags(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _trade_world()
    world.silent = {210}  # T1 fills at 211's Monday: not traded on its session
    for i in range(209, 217):
        _run(repo, world, i)
    rows = journal.build_rows(repo)
    assert rows and "BUY_T1" in rows[0]["notes"]
    assert "not traded on the execution session" in rows[0]["notes"]


def test_the_file_is_regenerated_atomically(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    path = journal.write(tmp_path / "reports" / "journal.csv", repo)
    assert path.read_text().splitlines() == [journal.HEADER]
    assert not path.with_suffix(".csv.tmp").exists()
