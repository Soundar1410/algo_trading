"""Read-only Streamlit page — Positional Stocks (``wsr1_weekly_stochrsi``, paper).

Spec v1.3.2 section 11.1 (Phase 6). Six tabs: Overview, Positions & orders,
Trades & performance, Equity, Latest report, Health — all backed by
:mod:`dashboards.data.positional_stocks`, never by SQL written in this file.

Three states before anything else is drawn, each said plainly in every tab
it affects, never as an empty table that would read as "no trades":

* **not started** — no ``positional_stocks.db`` yet: every tab says so and
  points to the go-live checklist;
* **book busy** — a weekly run holds the database past the read's busy
  timeout: "refresh in a moment";
* **unreadable** — the reason, never a traceback.

File-backed sections (the reports, ``journal.csv``) render on their own in the
last two states.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

for _parent in Path(__file__).resolve().parents:
    if (_parent / "pyproject.toml").is_file():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from dashboards.data.positional_stocks import (  # noqa: E402
    NOT_STARTED_STATE,
    OK,
    ActionItem,
    StocksPaths,
    StocksView,
    load_view,
    total_actions,
)
from dashboards.formatting import (  # noqa: E402
    MISSING,
    PAPER_LABEL,
    colored,
    format_inr,
    format_ist,
    format_pct,
    format_signed_inr,
    pnl_color,
)

TABS = (
    "Overview",
    "Positions & orders",
    "Trades & performance",
    "Equity",
    "Latest report",
    "Health",
)


def _inr(value: Decimal | None) -> str:
    return format_inr(None if value is None else float(value))


def _signed(value: Decimal | None) -> str:
    return format_signed_inr(None if value is None else float(value))


def _pct(value: Decimal | float | None, decimals: int = 2) -> str:
    return format_pct(None if value is None else float(value), decimals=decimals)


def _yes_no(value: bool | None) -> str:
    return MISSING if value is None else ("yes" if value else "no")


def render(streamlit: Any, view: StocksView) -> None:
    streamlit.markdown(colored(f"● {PAPER_LABEL}", "blue"))
    streamlit.caption(
        "wsr1_weekly_stochrsi — a weekly batch job. Read-only: this page never writes, "
        "and reads the book the weekly run persisted. Reload to refresh."
    )
    tabs = streamlit.tabs(list(TABS))
    renderers = (
        _overview,
        _positions,
        _trades,
        _equity,
        _latest_report,
        _health,
    )
    for tab, fn in zip(tabs, renderers, strict=True):
        with tab:
            if not view.started:
                streamlit.info(view.state_detail)
                if fn is _overview:
                    _flags_and_agents(streamlit, view)
                continue
            fn(streamlit, view)


def _book_state(streamlit: Any, view: StocksView) -> bool:
    """Show the busy/unreadable state; True when the book was read."""
    if view.state == OK and view.book is not None:
        return True
    streamlit.warning(view.state_detail)
    return False


# ------------------------------------------------------------ Overview
def _flags_and_agents(streamlit: Any, view: StocksView) -> None:
    streamlit.markdown("#### Configuration and scheduling")
    config = view.config
    if config.error:
        streamlit.error(config.error)
    cols = streamlit.columns(2)
    cols[0].metric("Runtime enabled", _yes_no(config.runtime_enabled))
    cols[1].metric("Strategy enabled", _yes_no(config.strategy_enabled))
    streamlit.dataframe(
        [
            {"LaunchAgent": name, "plist in ~/Library/LaunchAgents": _yes_no(installed)}
            for name, installed in view.agents.items()
        ],
        hide_index=True,
        width="stretch",
    )
    streamlit.caption(
        "Installed = the plist file exists (a file check only; launchd is not queried)."
    )


def _overview(streamlit: Any, view: StocksView) -> None:
    if _book_state(streamlit, view):
        assert view.book is not None
        book = view.book
        eq = book.latest_equity
        if eq is None:
            streamlit.info("No week has been marked to market yet.")
        else:
            streamlit.markdown(f"#### Week {eq.iso_week} (ending {eq.week_ending})")
            row = streamlit.columns(4)
            row[0].metric("Regime", eq.regime)
            row[1].metric("Equity", _inr(eq.equity))
            row[2].metric("Cash", _inr(eq.cash))
            row[3].metric("Peak", _inr(eq.peak))
            row = streamlit.columns(4)
            row[0].metric("Drawdown", _pct(eq.drawdown_pct))
            row[1].metric(
                "Brake 1",
                f"paused through {eq.brake1_until}" if eq.brake1_until else "off",
                help=f"can fire: {_yes_no(eq.brake1_can_fire)}",
            )
            row[2].metric(
                "Brake 2", f"ACTIVE since {eq.brake2_fired_on}" if eq.brake2_fired_on else "off"
            )
            row[3].metric("New entries", eq.entries_blocked or "allowed")
        slots = view.config.max_positions
        held = len(book.held)
        row = streamlit.columns(2)
        row[0].metric(
            "Open positions",
            f"{held} / {slots if slots is not None else MISSING}",
            help="Held positions vs the position slots in the configuration.",
        )
        cap = view.config.committed_cap
        committed = view.committed_held
        row[1].metric(
            "Committed (held positions)",
            _pct(committed / cap * 100) if committed is not None and cap else MISSING,
            help="Σ committed of held positions ÷ the committed-capital cap. Pending entries "
            "are not added in; the rules count them inside the weekly decision.",
        )
        if view.pending_entries:
            streamlit.caption(
                f"+{view.pending_entries} pending entr{'y' if view.pending_entries == 1 else 'ies'}"
                " also hold a slot and a commitment until they fill (see Positions & orders)."
            )
        last = book.latest_run
        streamlit.markdown(
            "**Last decide run:** "
            + (
                f"{last.iso_week} — {last.status} — started {format_ist(last.started_at)}, "
                f"finished {format_ist(last.finished_at)}"
                if last
                else MISSING
            )
        )
    preview = view.latest_preview
    streamlit.markdown(
        "**Last preview:** " + (preview.path.name if preview is not None else "none yet")
    )
    if view.state == OK:
        _operator_actions(streamlit, view.actions)
    _flags_and_agents(streamlit, view)


def _operator_actions(streamlit: Any, actions: tuple[ActionItem, ...]) -> None:
    total = total_actions(actions)
    streamlit.markdown(
        "#### Operator actions: "
        + (
            "could not be counted — see below"
            if total is None
            else colored(str(total), "red" if total else "green")
        )
    )
    for action in actions:
        if action.error is not None:
            streamlit.warning(f"{action.label}: {action.error}")
            continue
        if not action.count:
            streamlit.markdown(f"- {action.label}: 0")
            continue
        streamlit.markdown(f"- **{action.label}: {action.count}** — {action.what_to_do}")
        for item in action.items:
            streamlit.markdown(f"    - {item}")


# ------------------------------------------------- Positions & orders
def _positions(streamlit: Any, view: StocksView) -> None:
    if not _book_state(streamlit, view):
        return
    assert view.book is not None
    streamlit.markdown("#### Open positions")
    if view.marks_note:
        streamlit.caption(f"Marks and unrealised P&L: {view.marks_note}.")
    if not view.positions:
        streamlit.info("No open position.")
    else:
        streamlit.dataframe(
            [
                {
                    "Symbol": v.position.symbol,
                    "State": v.position.state.value,
                    "P1": _inr(v.position.p1),
                    "s": _pct(v.position.sizing.spacing * 100),
                    "L1": _inr(v.position.l1),
                    "L2": _inr(v.position.l2),
                    "Stop": "trail (10W EMA)"
                    if v.position.state.value == "HALF_SOLD"
                    else _inr(v.position.stop),
                    "Shares": v.position.shares_held,
                    "Avg cost": _inr(v.position.average_cost),
                    "Mark": _inr(v.mark),
                    "Unrealised": _signed(v.unrealised),
                    "Unrealised %": _pct(v.unrealised_pct),
                    "Weeks held": MISSING if v.weeks_held is None else v.weeks_held,
                    "Frozen": ("ESCALATED" if v.escalated else "yes") if v.frozen else "no",
                    "Review": v.review_reason or MISSING,
                }
                for v in view.positions
            ],
            hide_index=True,
            width="stretch",
        )
    streamlit.markdown("#### Pending orders for the next session")
    held = {p.position_id: p for p in view.book.held}
    if not view.book.pending:
        streamlit.info("No pending order.")
        return
    rows = []
    for order in view.book.pending:
        position = held.get(order.position_id or "")
        if order.action == "BUY_T1":
            levels = f"s {_pct((order.spacing or Decimal(0)) * 100)} — levels set at the fill"
        elif position is not None:
            levels = (
                f"P1 {_inr(position.p1)} · L1 {_inr(position.l1)} · L2 {_inr(position.l2)} · "
                f"Stop {_inr(position.stop)}"
            )
        else:
            levels = MISSING
        rows.append(
            {
                "Symbol": order.symbol,
                "Action": order.action,
                "On or after": order.execute_on_or_after,
                "Amount / qty": _inr(order.amount)
                if order.amount is not None
                else f"{order.quantity} shares",
                "Levels": levels,
                "Decided": order.decided_week,
                "catch_up": _yes_no(order.catch_up),
                "Reason": order.reason,
            }
        )
    streamlit.dataframe(rows, hide_index=True, width="stretch")


# ----------------------------------------------- Trades & performance
def _trades(streamlit: Any, view: StocksView) -> None:
    journal = view.journal
    streamlit.markdown("#### Closed trades (journal.csv)")
    if journal.error:
        streamlit.error(journal.error)
    elif not journal.exists:
        streamlit.info("No journal.csv yet — it is written by the first writing decide run.")
    elif journal.totals is None or not journal.rows:
        streamlit.info("No closed trade yet.")
    else:
        t = journal.totals
        row = streamlit.columns(4)
        row[0].metric("Trades", t.trades)
        row[1].metric("Win rate", _pct(t.win_rate_pct, 1))
        row[2].metric("Net P&L", _signed(t.net))
        row[3].metric("Best / worst", f"{_signed(t.best)} / {_signed(t.worst)}")
        row = streamlit.columns(2)
        row[0].metric("Average win", _signed(t.avg_win))
        row[1].metric("Average loss", _signed(t.avg_loss))
        streamlit.dataframe(
            [
                {
                    "Symbol": r["symbol"],
                    "T1": r["T1_date"],
                    "Exit": r["exit_date"],
                    "Exit type": r["exit_type"],
                    "Net P&L": _signed(Decimal(r["pnl_rs"])),
                    "% of A": f"{r['pnl_pct_of_A']}%",
                    "Weeks held": r["weeks_held"],
                    "Notes": r["notes"],
                }
                for r in journal.rows
            ],
            hide_index=True,
            width="stretch",
        )
        streamlit.markdown(colored(f"Net: {_signed(t.net)}", pnl_color(float(t.net))))
    streamlit.markdown("#### Recent fills")
    if not _book_state(streamlit, view):
        return
    assert view.book is not None
    if not view.book.fills:
        streamlit.info("No fill yet.")
        return
    streamlit.dataframe(
        [
            {
                "Session": f.session,
                "Symbol": f.symbol,
                "Action": f.action,
                "Shares": f.shares,
                "Price": _inr(f.price),
                "Fees": _inr(f.fees),
                "late_fill": _yes_no(f.late_fill),
                "Not traded on session": _yes_no(f.not_traded),
                "catch_up": _yes_no(f.catch_up),
                "Recorded": f.recorded_week,
            }
            for f in view.book.fills
        ],
        hide_index=True,
        width="stretch",
    )


# ------------------------------------------------------------- Equity
def _equity(streamlit: Any, view: StocksView) -> None:
    if not _book_state(streamlit, view):
        return
    assert view.book is not None
    rows = view.book.equity
    if not rows:
        streamlit.info("No week has been marked to market yet.")
        return
    import pandas as pd

    index = pd.to_datetime([r.week_ending for r in rows])
    streamlit.markdown("#### Weekly equity")
    streamlit.line_chart(
        pd.DataFrame(
            {"Equity": [float(r.equity) for r in rows], "Peak": [float(r.peak) for r in rows]},
            index=index,
        )
    )
    streamlit.markdown("#### Drawdown %")
    frame: dict[str, list[float]] = {"Drawdown %": [float(r.drawdown_pct) for r in rows]}
    if view.config.dd1_pct is not None:
        frame[f"Brake 1 ({view.config.dd1_pct}%)"] = [float(view.config.dd1_pct)] * len(rows)
    if view.config.dd2_pct is not None:
        frame[f"Brake 2 ({view.config.dd2_pct}%)"] = [float(view.config.dd2_pct)] * len(rows)
    streamlit.line_chart(pd.DataFrame(frame, index=index))
    streamlit.dataframe(
        [
            {
                "Week": r.iso_week,
                "Ending": r.week_ending,
                "Equity": _inr(r.equity),
                "Cash": _inr(r.cash),
                "Positions": _inr(r.positions_value),
                "Peak": _inr(r.peak),
                "Drawdown": _pct(r.drawdown_pct),
                "Regime": r.regime,
            }
            for r in reversed(rows)
        ],
        hide_index=True,
        width="stretch",
    )


# ------------------------------------------------------ Latest report
def _latest_report(streamlit: Any, view: StocksView) -> None:
    shown = False
    for label, report in (
        ("Newest preview (newer than the last decision)", view.newer_preview),
        ("Newest decision report", view.latest_decision),
    ):
        if report is None:
            continue
        shown = True
        streamlit.markdown(f"#### {label}: `{report.path.name}`")
        try:
            text = report.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            streamlit.error(f"could not read {report.path.name}: {exc}")
            continue
        with streamlit.container(border=True):
            streamlit.markdown(text)
    if not shown:
        streamlit.info("No decision or preview report yet.")


# ------------------------------------------------------------- Health
def _health(streamlit: Any, view: StocksView) -> None:
    streamlit.markdown("#### Weekly runs")
    if _book_state(streamlit, view):
        assert view.book is not None
        if not view.book.runs:
            streamlit.info("No weekly run recorded.")
        else:
            streamlit.dataframe(
                [
                    {
                        "Week": r.iso_week,
                        "Ending": r.week_ending,
                        "Status": r.status,
                        "Started": format_ist(r.started_at),
                        "Finished": format_ist(r.finished_at),
                    }
                    for r in reversed(view.book.runs)
                ],
                hide_index=True,
                width="stretch",
            )
    streamlit.markdown("#### Recent refusals and failed previews")
    if not view.refusals:
        streamlit.markdown("None.")
    for report in reversed(view.refusals):
        with streamlit.expander(report.path.name):
            try:
                streamlit.markdown(report.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                streamlit.error(f"could not read {report.path.name}: {exc}")
    streamlit.markdown("#### Backups")
    streamlit.markdown(
        f"{view.backups.count} snapshot(s); newest: {view.backups.newest or MISSING}"
    )
    streamlit.markdown("#### Cache freshness (NIFTY)")
    cache = view.cache
    if cache.error:
        streamlit.error(cache.error)
        return
    if cache.fresh is None:
        verdict = MISSING
    elif cache.fresh:
        verdict = "fresh"
    else:
        verdict = "STALE — the decide run would stop on a cold cache"
    streamlit.markdown(
        f"Last cached session: {cache.index_last_session or 'none'} · expected for "
        f"{cache.expected_week}: {cache.expected_session} · **{verdict}**"
    )


def main() -> None:  # pragma: no cover - exercised manually via `streamlit run`
    import streamlit as st

    from common.config import load_paths

    st.set_page_config(page_title="algo_trading — Positional Stocks", layout="wide", page_icon="📈")
    st.title("Positional Stocks")
    paths = load_paths()
    view = load_view(
        StocksPaths(
            project_root=paths.project_root,
            launch_agents_dir=Path.home() / "Library" / "LaunchAgents",
        )
    )
    render(st, view)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["NOT_STARTED_STATE", "TABS", "main", "render"]
