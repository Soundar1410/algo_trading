"""Read-only Streamlit page — Intraday Options.

Eight tabs (spec): Overview, Live Positions, Orders & Fills, Closed Trades,
Performance, Strategy Comparison, Signals & Events, Health — all backed by
:mod:`dashboards.data.intraday_options`, never by SQL written in this file.

**What this page does not show, and why.** Engine type, open legs/baskets,
selected strikes/expiry, per-leg P&L and roll count are not shown: those
describe ``MultiLegEngine``/``FixedStrikeEngine``, and per the runbook's
D56/D34 neither engine is ported into this codebase yet — there is no data
to read. Current price / points / unrealised MTM on Live Positions are not
shown either: no mark-to-market is persisted for paper positions (see
``dashboards/data/intraday_options.py``'s module docstring). Inventing
either would be exactly the "looks finished but isn't" pattern the runbook
already declines elsewhere.

Read-only/no-side-effect discipline is identical to ``dashboards/app.py`` —
see that module's docstring. Rankings on the Strategy Comparison tab are
computed read-only, on demand — the reference dashboard's "save today's
snapshot" write button is deliberately not ported.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

for _parent in Path(__file__).resolve().parents:
    if (_parent / "pyproject.toml").is_file():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from dashboards import system_health as health_page  # noqa: E402
from dashboards._shared import SnapshotUnavailable, load_snapshot, run_bounded  # noqa: E402
from dashboards.data.calendar_stats import (  # noqa: E402
    TRADING_DAY_CAVEAT,
    execution_day_stats,
    n_trading_days_back,
)
from dashboards.data.intraday_options import (  # noqa: E402
    MIN_SAMPLE_SIZE,
    ClosedTradeRow,
    OrderRow,
    OverviewRow,
    build_strategy_comparison,
    compute_metrics,
    drawdown_curve,
    equity_curve,
    load_closed_trades,
    load_daily_outcomes,
    load_errors,
    load_live_positions,
    load_notifications,
    load_orders,
    load_overview,
    load_signals,
    pnl_by_day,
    pnl_by_month,
)
from dashboards.formatting import (  # noqa: E402
    MISSING,
    format_age,
    format_inr,
    format_ist,
    format_pct,
    mode_label,
    to_csv_bytes,
)

NOT_YET_AVAILABLE = (
    "Engine type, open legs/baskets, selected strikes/expiry, per-leg P&L "
    "and roll count are not shown: MultiLegEngine/FixedStrikeEngine are not "
    "ported into this codebase yet (runbook D56/D34)."
)

_PRESETS = ("Today", "Last 7 trading days", "Last 30 trading days", "Custom")


def _resolve_date_range(streamlit: Any, key: str, today: date) -> tuple[date, date]:
    preset = streamlit.selectbox("Date range", _PRESETS, key=f"{key}_preset")
    if preset == "Today":
        return today, today
    if preset == "Last 7 trading days":
        return n_trading_days_back(today, 7), today
    if preset == "Last 30 trading days":
        return n_trading_days_back(today, 30), today
    cols = streamlit.columns(2)
    start = cols[0].date_input("Start", value=n_trading_days_back(today, 7), key=f"{key}_start")
    end = cols[1].date_input("End", value=today, key=f"{key}_end")
    if start > end:
        streamlit.warning("Start date is after end date — showing an empty range.")
    return start, end


def _fmt_ratio(value: float | None) -> str:
    if value is None:
        return MISSING
    if value == float("inf"):
        return "∞"
    return f"{value:.2f}"


# ============================================================== Overview
def _render_overview(streamlit: Any, rows: tuple[OverviewRow, ...]) -> None:
    if not rows:
        streamlit.info("No strategy has reported a heartbeat yet.")
        return
    for row in rows:
        streamlit.markdown(f"**{row.strategy_id} — {mode_label(row.execution_mode)}**")
        cols = streamlit.columns(6)
        cols[0].metric("Health", row.health_state)
        cols[1].metric("Heartbeat age", format_age(row.heartbeat_age_seconds))
        cols[2].metric("PID", row.pid if row.pid is not None else "—")
        cols[3].metric("Open positions", row.open_positions)
        cols[4].metric("Square-off", row.square_off_state or "—")
        cols[5].metric(
            "Today trades / P&L", f"{row.today_trade_count} / {format_inr(row.today_net_pnl)}"
        )
        if row.entries_blocked:
            streamlit.warning(f"{row.strategy_id}: entries blocked")
        if row.current_position_instrument:
            qty = abs(row.current_position_quantity or 0)
            streamlit.write(
                f"Current position: {row.current_position_side} {qty} x "
                f"{row.current_position_instrument}"
            )
        else:
            streamlit.caption("No open position.")
        if row.latest_error:
            streamlit.error(
                f"{format_ist(row.latest_error.occurred_at)} — "
                f"[{row.latest_error.severity}] {row.latest_error.message}"
            )
        streamlit.caption(
            "Warm-up status / context trust: not exposed as a structured "
            "column yet — omitted rather than parsed from an internal payload."
        )
    streamlit.caption(NOT_YET_AVAILABLE)


# =========================================================== Live positions
def _render_live_positions(streamlit: Any, rows: tuple[Any, ...]) -> None:
    if not rows:
        streamlit.info("No open positions.")
        return
    table = [
        {
            "Strategy": r.strategy_id,
            "Mode": mode_label(r.execution_mode),
            "Instrument": r.instrument,
            "Side": r.side,
            "Quantity": r.quantity,
            "Entry time (IST)": format_ist(r.entry_time),
            "Entry price": format_inr(r.entry_price),
            "Current price": MISSING,
            "Points": MISSING,
            "MTM": MISSING,
            "Stop": format_inr(r.stop_price) if r.stop_price is not None else MISSING,
            "Target": format_inr(r.target_price) if r.target_price is not None else MISSING,
            "Highest favourable": (
                format_inr(r.highest_favourable) if r.highest_favourable is not None else MISSING
            ),
            "Lowest favourable": (
                format_inr(r.lowest_favourable) if r.lowest_favourable is not None else MISSING
            ),
            "Duration": format_age(r.duration_seconds),
        }
        for r in rows
    ]
    streamlit.dataframe(table, hide_index=True, width="stretch")
    streamlit.caption(
        "Current price / points / MTM are not shown: no mark-to-market is "
        "persisted for paper positions today. A stale value is never shown "
        "as current."
    )


# ============================================================ Orders & fills
def _render_orders(streamlit: Any, rows: tuple[OrderRow, ...]) -> None:
    if not rows:
        streamlit.info("No orders in this window.")
        return
    table = [
        {
            "Correlation ID": o.correlation_id,
            "Strategy": o.strategy_id,
            "Mode": mode_label(o.execution_mode),
            "Intent time (IST)": format_ist(o.intent_time),
            "Instrument": o.instrument,
            "Side": o.side,
            "Qty": o.quantity,
            "Order type": o.order_type,
            "Status": o.status or "—",
            "Broker order ID": o.broker_order_id or "—",
            "Filled qty": o.filled_quantity,
            "Avg fill": (
                format_inr(o.average_fill_price) if o.average_fill_price is not None else MISSING
            ),
            "Rejection reason": o.rejection_reason or "—",
            "Charges": format_inr(o.total_charges),
            "Fills": len(o.fills),
        }
        for o in rows
    ]
    streamlit.dataframe(table, hide_index=True, width="stretch")

    correlation_ids = [o.correlation_id for o in rows]
    selected = streamlit.selectbox("Inspect fills for order", ["—", *correlation_ids])
    if selected != "—":
        order = next(o for o in rows if o.correlation_id == selected)
        if order.fills:
            fills_table = [
                {
                    "Broker fill ID": f.broker_fill_id,
                    "Qty": f.quantity,
                    "Price": format_inr(f.price),
                    "Reference price": (
                        format_inr(f.reference_price) if f.reference_price is not None else MISSING
                    ),
                    "Slippage": (
                        format_inr(f.slippage_amount)
                        if f.slippage_amount is not None
                        else MISSING
                    ),
                    "Latency (ms)": f.latency_ms if f.latency_ms is not None else MISSING,
                    "Fill method": f.fill_method or "—",
                    "Charges": format_inr(f.charges),
                    "Filled at (IST)": format_ist(f.filled_at),
                }
                for f in order.fills
            ]
            streamlit.dataframe(fills_table, hide_index=True, width="stretch")
        else:
            streamlit.caption("No fills recorded for this order.")

    streamlit.download_button(
        "Download orders (CSV)", data=to_csv_bytes(table), file_name="orders.csv", mime="text/csv"
    )


# ============================================================= Closed trades
def _closed_trades_table(trades: tuple[ClosedTradeRow, ...]) -> list[dict[str, object]]:
    table = []
    for t in trades:
        table.append(
            {
                "Strategy": t.strategy_id,
                "Mode": mode_label(t.execution_mode),
                "Instrument": t.instrument,
                "Entry time (IST)": format_ist(t.entry_time),
                "Exit time (IST)": format_ist(t.exit_time),
                "Side": t.side or "—",
                "Quantity": t.quantity,
                "Entry price": format_inr(t.entry_price),
                "Exit price": format_inr(t.exit_price) if t.exit_price is not None else MISSING,
                "Points": f"{t.points:.2f}" if t.points is not None else MISSING,
                "Gross P&L": format_inr(t.gross_pnl),
                "Charges": format_inr(t.charges),
                "Net P&L": format_inr(t.net_pnl),
                "Exit reason": "—",
            }
        )
    return table


def _render_closed_trades(streamlit: Any, trades: tuple[ClosedTradeRow, ...]) -> None:
    if not trades:
        streamlit.info("No closed trades in this range.")
        return
    table = _closed_trades_table(trades)
    streamlit.dataframe(table, hide_index=True, width="stretch")
    streamlit.caption("Exit reason is not persisted anywhere yet.")
    streamlit.download_button(
        "Download closed trades (CSV)",
        data=to_csv_bytes(table),
        file_name="closed_trades.csv",
        mime="text/csv",
    )


# ============================================================= Performance
def _render_performance(
    streamlit: Any,
    trades: tuple[ClosedTradeRow, ...],
    outcomes: tuple[Any, ...],
    window_start: date,
    window_end: date,
) -> None:
    metrics = compute_metrics(trades)
    row1 = streamlit.columns(4)
    row1[0].metric("Trades", metrics.sample_size)
    row1[1].metric(
        "Win rate", format_pct(metrics.win_rate) if metrics.win_rate is not None else MISSING
    )
    row1[2].metric("Profit factor", _fmt_ratio(metrics.profit_factor))
    row1[3].metric(
        "Expectancy", format_inr(metrics.expectancy) if metrics.expectancy is not None else MISSING
    )
    row2 = streamlit.columns(4)
    row2[0].metric("Net P&L", format_inr(metrics.net_profit))
    row2[1].metric(
        "Max drawdown",
        format_inr(metrics.max_drawdown) if metrics.max_drawdown is not None else MISSING,
    )
    row2[2].metric("Charges", format_inr(metrics.total_charges))
    row2[3].metric(
        "Avg win / loss",
        f"{format_inr(metrics.avg_win)} / {format_inr(metrics.avg_loss)}",
    )
    if not metrics.reliable:
        streamlit.warning(
            f"Only {metrics.sample_size} trade(s) in this range — statistics are not yet "
            f"a reliable sample (n≥{MIN_SAMPLE_SIZE} recommended)."
        )

    equity = equity_curve(trades)
    if equity:
        import pandas as pd

        streamlit.line_chart(
            pd.DataFrame(equity, columns=["Exit time", "Cumulative net P&L"]).set_index(
                "Exit time"
            )
        )
        drawdown = drawdown_curve(equity)
        streamlit.line_chart(
            pd.DataFrame(drawdown, columns=["Exit time", "Drawdown"]).set_index("Exit time")
        )
        daily = pnl_by_day(trades)
        streamlit.bar_chart(pd.DataFrame(daily, columns=["Date", "Net P&L"]).set_index("Date"))
        monthly = pnl_by_month(trades)
        streamlit.bar_chart(
            pd.DataFrame(monthly, columns=["Month", "Net P&L"]).set_index("Month")
        )
    else:
        streamlit.caption("No closed trades in this range yet — charts will appear once there are.")

    day_stats = execution_day_stats(outcomes, window_start=window_start, window_end=window_end)
    execution_pct_label = (
        format_pct(day_stats.execution_pct) if day_stats.execution_pct is not None else MISSING
    )
    streamlit.caption(
        f"Executed {day_stats.executed_days} of {day_stats.eligible_trading_days} eligible "
        f"trading days ({execution_pct_label})."
    )
    streamlit.caption(TRADING_DAY_CAVEAT)

    table = _closed_trades_table(trades)
    streamlit.download_button(
        "Download performance trades (CSV)",
        data=to_csv_bytes(table),
        file_name="performance_trades.csv",
        mime="text/csv",
    )


# ========================================================= Strategy comparison
def _render_comparison(streamlit: Any, rows: tuple[Any, ...]) -> None:
    if not rows:
        streamlit.info("No strategy has any closed trade in this range.")
        return
    table = []
    for r in rows:
        m = r.metrics
        table.append(
            {
                "Strategy": r.strategy_id,
                "Net P&L": format_inr(m.net_profit),
                "ROI %": format_pct(r.roi_pct) if r.roi_pct is not None else MISSING,
                "Trades": m.sample_size,
                "Win rate": format_pct(m.win_rate) if m.win_rate is not None else MISSING,
                "Profit factor": _fmt_ratio(m.profit_factor),
                "Expectancy": format_inr(m.expectancy) if m.expectancy is not None else MISSING,
                "Max drawdown": (
                    format_inr(m.max_drawdown) if m.max_drawdown is not None else MISSING
                ),
                "Charges": format_inr(m.total_charges),
                "Executed days": f"{r.execution_days}/{r.eligible_days}",
                "Sample": "reliable" if m.reliable else f"insufficient (n={m.sample_size})",
            }
        )
    streamlit.dataframe(table, hide_index=True, width="stretch")
    streamlit.caption(
        "Rankings are computed read-only, on demand, from closed trades in the "
        "selected range — nothing is written or snapshotted. ROI is shown only "
        "for a strategy that declares its own capital_base in config."
    )
    streamlit.download_button(
        "Download comparison (CSV)",
        data=to_csv_bytes(table),
        file_name="strategy_comparison.csv",
        mime="text/csv",
    )


# ================================================================ Signals
def _render_signals(
    streamlit: Any,
    signals: tuple[Any, ...],
    notifications: tuple[Any, ...],
    errors: tuple[Any, ...],
) -> None:
    streamlit.markdown("**Signals**")
    if signals:
        table = [
            {
                "Time (IST)": format_ist(s.evaluated_at),
                "Strategy": s.strategy_id,
                "Side": s.side,
                "Candle O/H/L/C": (
                    f"{s.candle_open:.2f}/{s.candle_high:.2f}/{s.candle_low:.2f}/"
                    f"{s.candle_close:.2f}"
                ),
                "Candle end (IST)": format_ist(s.candle_end_at),
                "Reference price": format_inr(s.reference_price),
                "Reason": s.reason or "—",
                "Order": s.order_correlation_id or "—",
            }
            for s in signals
        ]
        streamlit.dataframe(table, hide_index=True, width="stretch")
        streamlit.download_button(
            "Download signals (CSV)", data=to_csv_bytes(table), file_name="signals.csv",
            mime="text/csv",
        )
    else:
        streamlit.info("No signals recorded today.")

    streamlit.markdown("**Notifications**")
    if notifications:
        table = [
            {
                "Time (IST)": format_ist(n.created_at),
                "Strategy": n.strategy_id or "—",
                "Channel": n.channel,
                "Event": n.event_type,
                "Delivered": n.delivered,
                "Failure reason": n.failure_reason or "—",
            }
            for n in notifications
        ]
        streamlit.dataframe(table, hide_index=True, width="stretch")
    else:
        streamlit.caption("No notifications recorded.")

    streamlit.markdown("**Errors & operational events**")
    if errors:
        table = [
            {
                "Time (IST)": format_ist(e.occurred_at),
                "Strategy": e.strategy_id or "—",
                "Severity": e.severity,
                "Component": e.component,
                "Message": e.message,
            }
            for e in errors
        ]
        streamlit.dataframe(table, hide_index=True, width="stretch")
    else:
        streamlit.caption("No errors recorded.")


# =================================================================== main
def main() -> None:  # pragma: no cover - exercised manually via `streamlit run`
    import streamlit as st

    from common.config import load_paths

    st.set_page_config(page_title="algo_trading — Intraday Options", layout="wide", page_icon="📈")
    st.title("Intraday Options")

    paths = load_paths()
    runtime_id = "intraday_options"
    database_path = paths.database_path(runtime_id)
    today = date.today()
    trading_date = today.isoformat()

    result = load_snapshot(database_path, runtime_id, trading_date)
    if isinstance(result, SnapshotUnavailable):
        st.info(result.reason)
        return
    strategy_ids = tuple(s.strategy_id for s in result.strategies)

    tabs = st.tabs(
        [
            "Overview",
            "Live Positions",
            "Orders & Fills",
            "Closed Trades",
            "Performance",
            "Strategy Comparison",
            "Signals & Events",
            "Health",
        ]
    )

    with tabs[0]:

        @st.fragment(run_every=5)
        def _overview() -> None:
            overview = run_bounded(
                database_path, lambda conn: load_overview(conn, runtime_id, trading_date)
            )
            _render_overview(st, () if isinstance(overview, SnapshotUnavailable) else overview)

        _overview()

    with tabs[1]:

        @st.fragment(run_every=5)
        def _positions() -> None:
            positions = run_bounded(
                database_path, lambda conn: load_live_positions(conn, runtime_id, trading_date)
            )
            rows = () if isinstance(positions, SnapshotUnavailable) else positions
            _render_live_positions(st, rows)

        _positions()

    with tabs[2]:

        @st.fragment(run_every=5)
        def _orders() -> None:
            orders = run_bounded(
                database_path, lambda conn: load_orders(conn, runtime_id, trading_date)
            )
            _render_orders(st, () if isinstance(orders, SnapshotUnavailable) else orders)

        _orders()

    with tabs[3]:
        start, end = _resolve_date_range(st, "closed_trades", today)

        @st.fragment(run_every=30)
        def _closed(start: date = start, end: date = end) -> None:
            trades = run_bounded(
                database_path,
                lambda conn: load_closed_trades(
                    conn, runtime_id, start_date=start.isoformat(), end_date=end.isoformat()
                ),
            )
            _render_closed_trades(st, () if isinstance(trades, SnapshotUnavailable) else trades)

        _closed()

    with tabs[4]:
        start, end = _resolve_date_range(st, "performance", today)

        @st.fragment(run_every=30)
        def _performance(start: date = start, end: date = end) -> None:
            trades = run_bounded(
                database_path,
                lambda conn: load_closed_trades(
                    conn, runtime_id, start_date=start.isoformat(), end_date=end.isoformat()
                ),
            )
            outcomes = run_bounded(
                database_path,
                lambda conn: tuple(
                    o
                    for sid in strategy_ids
                    for o in load_daily_outcomes(
                        conn,
                        runtime_id,
                        strategy_id=sid,
                        execution_mode=None,
                        start_date=start.isoformat(),
                        end_date=end.isoformat(),
                    )
                ),
            )
            _render_performance(
                st,
                () if isinstance(trades, SnapshotUnavailable) else trades,
                () if isinstance(outcomes, SnapshotUnavailable) else outcomes,
                start,
                end,
            )

        _performance()

    with tabs[5]:
        start, end = _resolve_date_range(st, "comparison", today)

        @st.fragment(run_every=30)
        def _comparison(start: date = start, end: date = end) -> None:
            rows = run_bounded(
                database_path,
                lambda conn: build_strategy_comparison(
                    conn,
                    runtime_id,
                    paths.config_root,
                    strategy_ids,
                    start_date=start.isoformat(),
                    end_date=end.isoformat(),
                ),
            )
            _render_comparison(st, () if isinstance(rows, SnapshotUnavailable) else rows)

        _comparison()

    with tabs[6]:

        @st.fragment(run_every=30)
        def _signals() -> None:
            signals = run_bounded(
                database_path, lambda conn: load_signals(conn, runtime_id, trading_date)
            )
            notifications = run_bounded(
                database_path, lambda conn: load_notifications(conn, runtime_id)
            )
            errors = run_bounded(database_path, lambda conn: load_errors(conn, runtime_id))
            _render_signals(
                st,
                () if isinstance(signals, SnapshotUnavailable) else signals,
                () if isinstance(notifications, SnapshotUnavailable) else notifications,
                () if isinstance(errors, SnapshotUnavailable) else errors,
            )

        _signals()

    with tabs[7]:

        @st.fragment(run_every=5)
        def _health() -> None:
            health_page.render(
                st, health_page.load_system_health(database_path, runtime_id, trading_date)
            )

        _health()


if __name__ == "__main__":  # pragma: no cover
    main()
