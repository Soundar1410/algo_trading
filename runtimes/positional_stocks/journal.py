"""The journal export (spec 11 v1.2m): one CSV row per closed trade.

The columns are the V1 plan's trade sheet, in its order, so the paper journal
compares line by line with a manual one. The file is regenerated in full from
``positional_stocks.db`` on every non-dry run:
``data/reports/positional_stocks/journal.csv``.

Conventions (the spec fixes the columns, not their formats):

* dates ISO ``YYYY-MM-DD``; weeks ``YYYY-Www``; flags ``yes`` / ``no``;
* ``s``, ``atr_pct``, ``perf6m_stock``, ``perf6m_nifty`` and ``pnl_pct_of_A``
  are **percent**, to 2 dp; money to the paisa;
* prices, shares and levels are the stored ones, in the units of their fills
  (a later rescale is listed in ``notes``);
* the trigger-week columns come from ``stock_entry_signals``, written at the
  decision so a later restatement cannot change them.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path

from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import week_of, weeks_between
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    OrderAction,
    Position,
    PositionState,
    money,
)

from .accounting import CLOSED_IN_LIEU, REISSUED, STUCK_EXIT
from .repository import StockRepository, week_text

#: Spec 11 v1.2m, verbatim.
HEADER = (
    "trade_id,symbol,sector,promoter_group,event_risk,regime,arm_week,trigger_week,K_trigger,"
    "D_trigger,close_trigger,ema50_1w,perf6m_stock,perf6m_nifty,high_52w,atr_1w,atr_pct,s,A,"
    "T1_date,T1_price,T1_shares,L1,L2,stop,T2_date,T2_price,T2_shares,T3_date,T3_price,T3_shares,"
    "avg_cost,partial_date,partial_price,partial_shares,exit_date,exit_price,exit_shares,"
    "exit_type,pnl_rs,pnl_pct_of_A,weeks_held,rule_breaks,notes,screenshot"
)
COLUMNS = HEADER.split(",")


def _num(value: float | None, places: int = 2) -> str:
    return "" if value is None else f"{value:.{places}f}"


def _pct(value: float | None) -> str:
    return "" if value is None else f"{value * 100:.2f}"


def exit_type(reason: str) -> str:
    """The closing order's reason, as one word per exit rule (spec 4.10)."""
    reason = reason.removesuffix(f"; {REISSUED}")
    for prefix, kind in (
        ("stop:", "stop"),
        ("trail time exit", "trail_time"),
        ("trail exit", "trail"),
        ("time exit", "time"),
        ("thesis exit", "thesis"),
        (STUCK_EXIT, "stuck_freeze_exit"),
    ):
        if reason.startswith(prefix):
            return kind
    return "other"


def build_rows(repository: StockRepository) -> list[dict[str, str]]:
    """One row per CLOSED position, oldest exit first."""
    conn = repository.database.connect()
    sid = repository.strategy_id
    fills = conn.execute(
        "SELECT f.*, o.reason FROM stock_fills f JOIN stock_pending_orders o USING (order_id) "
        "WHERE f.strategy_id = ? ORDER BY f.fill_id",
        (sid,),
    ).fetchall()
    by_position: dict[str, list[sqlite3.Row]] = {}
    for row in fills:
        by_position.setdefault(row["position_id"], []).append(row)
    actions: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT * FROM stock_corporate_actions WHERE strategy_id = ? ORDER BY ex_session", (sid,)
    ):
        actions.setdefault(row["position_id"], []).append(row)
    signals = repository.entry_signals()

    rows: list[dict[str, str]] = []
    closed = [p for p in repository.positions().values() if p.state is PositionState.CLOSED]
    for position in sorted(closed, key=lambda p: (p.exit_week or (0, 0), p.position_id)):
        rows.append(
            _row(
                position,
                by_position.get(position.position_id, []),
                actions.get(position.position_id, []),
                signals,
            )
        )
    return rows


def _row(
    position: Position,
    fills: list[sqlite3.Row],
    actions: list[sqlite3.Row],
    signals: dict[str, sqlite3.Row],
) -> dict[str, str]:
    t1_order = next((f["order_id"] for f in fills if f["action"] == "BUY_T1"), None)
    signal = signals.get(t1_order) if t1_order else None
    sizing = position.sizing
    row = dict.fromkeys(COLUMNS, "")
    row.update(
        trade_id=position.position_id,
        symbol=position.symbol,
        sector=position.sector,
        promoter_group=position.group,
        event_risk="yes" if sizing.event_risk else "no",
        s=_pct(float(sizing.spacing)),
        A=str(sizing.allocation),
        L1=str(position.l1),
        L2=str(position.l2),
        stop=str(position.stop),
        avg_cost=str(money(position.average_cost)),
        pnl_rs=str(position.net_pnl),
        pnl_pct_of_A=_pct(float(position.net_pnl / sizing.allocation)),
    )
    if signal is not None:
        row.update(
            regime=signal["regime"],
            arm_week=signal["arm_week"] or "",
            trigger_week=signal["trigger_week"],
            K_trigger=_num(signal["k_trigger"]),
            D_trigger=_num(signal["d_trigger"]),
            close_trigger=_num(signal["close_trigger"]),
            ema50_1w=_num(signal["ema50_1w"]),
            perf6m_stock=_pct(signal["perf6m_stock"]),
            perf6m_nifty=_pct(signal["perf6m_nifty"]),
            high_52w=_num(signal["high_52w"]),
            atr_1w=_num(signal["atr_1w"]),
            atr_pct=_pct(signal["atr_pct"]),
        )
    for buy in position.buys:
        prefix = f"T{buy.tranche}"
        row[f"{prefix}_date"] = buy.session.isoformat()
        row[f"{prefix}_price"] = str(buy.price)
        row[f"{prefix}_shares"] = str(buy.shares)
    notes: list[str] = []
    half = next((s for s in position.sales if s.action is OrderAction.SELL_HALF), None)
    if half is not None:
        row.update(
            partial_date=half.session.isoformat(),
            partial_price=str(half.price),
            partial_shares=str(half.shares),
        )
    elif position.half_sold_week is not None:
        notes.append(f"partial sold 0 (week {week_text(position.half_sold_week)})")

    closing = position.sales[-1] if position.sales and position.sales[-1].shares else None
    zeroed = actions[-1] if actions and int(actions[-1]["shares_after"]) == 0 else None
    exit_week = position.exit_week
    if zeroed is not None and (
        closing is None or zeroed["ex_session"] > closing.session.isoformat()
    ):
        row.update(
            exit_date=zeroed["ex_session"],
            exit_price=zeroed["reference_close"],
            exit_shares=str(zeroed["shares_before"]),
            exit_type="consolidation_cash_in_lieu",
        )
        notes.append(f"D101: {CLOSED_IN_LIEU} {zeroed['cash']}")
    elif closing is not None:
        reason = next(
            (f["reason"] for f in reversed(fills) if f["action"] == closing.action.value), ""
        )
        row.update(
            exit_date=closing.session.isoformat(),
            exit_price=str(closing.price),
            exit_shares=str(closing.shares),
            exit_type=exit_type(reason),
        )
    if exit_week is not None:
        row["weeks_held"] = str(weeks_between(week_of(position.buys[0].session), exit_week))

    for action in actions:
        applies = ", ".join(json.loads(action["applies_to"]))
        notes.append(
            f"{action['kind']} {action['ratio']} ex {action['ex_session']}: "
            f"{action['shares_before']} -> {action['shares_after']} shares ({applies}), "
            f"cash {action['cash']}"
        )
    for fill in fills:
        flags = []
        if fill["late_fill"]:
            flags.append("late_fill")
        if fill["not_traded_on_execution_session"]:
            flags.append("not traded on the execution session")
        if fill["reason"] == STUCK_EXIT:
            flags.append("stuck-freeze exit")
        if flags:
            notes.append(f"{fill['action']} {fill['session']}: {', '.join(flags)}")
    row["notes"] = "; ".join(notes)
    return row


def render(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def write(path: Path, repository: StockRepository) -> Path:
    """Regenerate the journal atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".csv.tmp")
    temporary.write_text(render(build_rows(repository)), encoding="utf-8")
    temporary.replace(path)
    return path
