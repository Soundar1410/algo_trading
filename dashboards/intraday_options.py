"""Read-only Streamlit page — Intraday Options.

Eight tabs: Overview, Open Positions, Baskets, Orders & Fills, Closed
Trades, Performance, Strategy Comparison, Signals & Events — all backed by
:mod:`dashboards.data.intraday_options`, never by SQL written in this file.

A ninth "Health" tab existed until 9 September 2026 and was removed on the
operator's own call: it rendered exactly what the standalone System Health
page renders (same ``load_system_health``, same ``render``), merely filtered
to the selected strategies, so it duplicated a page already one click away in
the sidebar. Per-strategy scoping of the health view is the only thing that
went with it. The other diagnostic tabs were deliberately kept — each holds
data reachable nowhere else: Signals & Events is the only view of a signal
that fired without becoming a trade and of the incident history (26 CRITICAL
rows as of that date), Orders & Fills the only view of a rejected or unfilled
order (Closed Trades shows completed round trips only), Baskets the only view
of a multi-leg strategy's legs.

**A persistent "Strategy:" selector**, right below the title and above the
tabs, scopes every tab except Strategy Comparison (which has its own
"Compare strategies" multiselect — see its section below) to one strategy
or "All Strategies". Built from
:func:`dashboards.data.strategy_scope.discover_strategy_options` — the
reusable component this page and the two stub pages share, not a
per-tab re-derivation — so a strategy appears the moment it is configured,
running, disabled, or has any historical record, never only once it has
traded.

**What this page does not show, and why.** Engine type, open legs/baskets,
selected strikes/expiry, per-leg P&L and roll count are not shown: those
describe ``MultiLegEngine``/``FixedStrikeEngine``, and per the runbook's
D56/D34 neither engine is ported into this codebase yet — there is no data
to read. Current price / points / unrealised MTM on Open Positions **are**
shown, from migration 0014's ``position_marks`` (see ``dashboards/data/
intraday_options.py``'s module docstring) — written once per closed
underlying candle by the strategy's own worker process, never invented from
the entry fill price. A mark older than ``_MARK_STALE_AFTER_SECONDS`` is
shown as stale (with its age) rather than as current — inventing either would
be exactly the "looks finished but isn't" pattern the runbook already
declines elsewhere. The multi-leg basket/leg drill-down does not have a mark
yet — a separate, later phase.

**Performance vs. Strategy Comparison.** They answer different questions and
deliberately scope differently. Performance shows *every* strategy id with
activity in the window — including an id no config file declares any more,
labelled retired, and one that was up all session without firing — scoped only
by the page-level Strategy selector, as a per-strategy table plus one explicit
combined row. Strategy Comparison stays a ranked leaderboard over the
configured ids you pick in its own multiselect. The combined row is computed
from the union of the in-scope trades, never by adding the per-strategy rows
up: win rate, profit factor, expectancy and max drawdown are not additive.

Read-only/no-side-effect discipline is identical to ``dashboards/Home.py`` —
see that module's docstring. Rankings on the Strategy Comparison tab are
computed read-only, on demand — the reference dashboard's "save today's
snapshot" write button is deliberately not ported.
"""

from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

for _parent in Path(__file__).resolve().parents:
    if (_parent / "pyproject.toml").is_file():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from dashboards._shared import SnapshotUnavailable, load_snapshot, run_bounded  # noqa: E402
from dashboards.data.calendar_stats import (  # noqa: E402
    TRADING_DAY_CAVEAT,
    n_trading_days_back,
)
from dashboards.data.intraday_options import (  # noqa: E402
    MIN_SAMPLE_SIZE,
    ClosedTradeRow,
    OrderRow,
    OverviewRow,
    PerformanceBreakdown,
    StrategyPerformanceRow,
    build_performance_breakdown,
    build_strategy_comparison,
    equity_curves_by_strategy,
    load_closed_trades,
    load_errors,
    load_live_positions,
    load_notifications,
    load_orders,
    load_overview,
    load_signals,
    load_strategy_config_raw,
)
from dashboards.data.multi_leg import (  # noqa: E402
    BasketRow,
    LegRow,
    RollAnchorRow,
    RollRow,
    load_baskets,
    load_legs_for_basket,
    load_roll_anchor,
    load_rolls_for_basket,
)
from dashboards.data.strategy_scope import (  # noqa: E402
    StrategyOption,
    discover_strategy_options,
    render_strategy_multiselect,
)
from dashboards.formatting import (  # noqa: E402
    MISSING,
    format_age,
    format_inr,
    format_ist,
    format_pct,
    freshness_note,
    health_badge,
    mode_label,
    to_csv_bytes,
)

NOT_YET_AVAILABLE = (
    "Open legs/baskets, selected strikes/expiry, per-leg P&L and adjustment "
    "count for a multi-leg strategy (e.g. straddle_920) are shown in the "
    "Baskets tab instead of here — this Overview row stays single-position-"
    "shaped. FixedStrikeEngine is still not ported into this codebase "
    "(runbook D34)."
)

#: A position_marks row older than this is shown as stale (with its age) —
#: never as current. Marks are written once per closed underlying candle
#: (see repository.update_position_marks), and the longest timeframe any
#: strategy configured today uses is c921_ema_cross_buy's 5 minutes
#: (config/strategies/intraday_options/c921_ema_cross_buy.yaml). This is
#: deliberately generous headroom above that, not a tight SLA — revisit if a
#: future strategy configures a longer candle timeframe.
_MARK_STALE_AFTER_SECONDS = 900.0

_PRESETS = ("Today", "Yesterday", "Last 7 trading days", "Last 30 trading days", "Custom")
_MODES = ("All", "Paper", "Live")
_MODE_VALUES = {"All": None, "Paper": "paper", "Live": "live"}
_SECRET_KEY_PATTERN = re.compile(r"secret|token|password|pin|api[_-]?key", re.IGNORECASE)

#: ``StrategyOption.style`` is a bare ``common.config.models.StrategyStyle``
#: value ("buying"/"selling") — these are the pills labels shown instead,
#: matching Trading_Automation's own "Options Buying"/"Options Selling"
#: wording.
_STYLE_LABELS = ("Options Buying", "Options Selling")
_STYLE_VALUES = {"Options Buying": "buying", "Options Selling": "selling"}

_OUTCOMES = ("Winning", "Losing")
_SEVERITIES = ("INFO", "WARNING", "ERROR", "CRITICAL")


def _previous_trading_day(today: date) -> date:
    """The most recent trading day strictly before ``today`` — what "Yesterday"
    means to a trader, not the literal calendar day before.

    Calendar-yesterday would be an empty page every Monday (Sunday) and every
    Sunday (Saturday). Walking back to the previous *session* instead keeps the
    preset useful on all seven days: Monday shows Friday, Saturday and Sunday
    both show Friday.

    Inherits :data:`~dashboards.data.calendar_stats.TRADING_DAY_CAVEAT` — a
    "trading day" here is Monday-Friday, because ``calendar_stats`` models no
    exchange holiday calendar. The day after a holiday therefore resolves to
    that holiday and shows nothing, exactly as the "Last 7/30 trading days"
    presets already over-count it. Fixing that is a change to the shared
    calendar helper, not to this preset.
    """
    return n_trading_days_back(today - timedelta(days=1), 1)


def _resolve_date_range(streamlit: Any, key: str, today: date) -> tuple[date, date]:
    preset = streamlit.selectbox("Date range", _PRESETS, key=f"{key}_preset")
    if preset == "Today":
        return today, today
    if preset == "Yesterday":
        previous = _previous_trading_day(today)
        return previous, previous
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


def _resolve_mode(streamlit: Any, key: str) -> str | None:
    choice = streamlit.selectbox("Mode", _MODES, key=key)
    return _MODE_VALUES.get(choice)


def _fmt_ratio(value: float | None) -> str:
    if value is None:
        return MISSING
    if value == float("inf"):
        return "∞"
    return f"{value:.2f}"


def _redact_secrets(value: object) -> object:
    """Recursively blank any key that looks secret-shaped. No committed
    strategy YAML has ever held a real secret (CLAUDE.md: secrets live only
    in the gitignored .env) — this is defence in depth for the
    Configuration summary section, not a response to a known leak."""
    if isinstance(value, dict):
        return {
            k: ("REDACTED" if _SECRET_KEY_PATTERN.search(str(k)) else _redact_secrets(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(v) for v in value]
    return value


def _render_config_summary(streamlit: Any, config_root: object, strategy_id: str) -> None:
    with streamlit.expander("Configuration summary"):
        config = load_strategy_config_raw(config_root, strategy_id)
        if config is None:
            streamlit.caption("No configuration file found for this strategy.")
            return
        streamlit.caption("Current committed configuration — not a historical snapshot.")
        streamlit.json(_redact_secrets(config))


# ============================================================== Overview
def _render_overview(streamlit: Any, rows: tuple[OverviewRow, ...]) -> None:
    if not rows:
        streamlit.info("No strategy has reported a heartbeat yet.")
        return
    for row in rows:
        streamlit.markdown(f"**{row.strategy_id} — {mode_label(row.execution_mode)}**")
        # Health is a colored markdown badge, not a boxed metric: its value
        # (RUNNING_PAPER, RUNNING_LIVE, ...) is exactly the "avoid clipped
        # values such as 'RUNNING...'" example the spec names, and markdown
        # text wraps instead of ellipsizing regardless of column width.
        # Every other field here is short enough for a three-column row —
        # confirmed against COMPLETED/IN_PROGRESS, the longest square-off
        # values. Today's trade count and P&L are two genuinely separate
        # numbers, not one concatenated string.
        streamlit.markdown(f"Health: {health_badge(row.health_state)}")
        top = streamlit.columns(3)
        top[0].metric("Heartbeat age", format_age(row.heartbeat_age_seconds))
        top[1].metric("PID", row.pid if row.pid is not None else "—")
        top[2].metric("Open positions", row.open_positions)
        bottom = streamlit.columns(3)
        bottom[0].metric("Square-off", row.square_off_state or "—")
        bottom[1].metric("Today's trades", row.today_trade_count)
        bottom[2].metric("Today's P&L", format_inr(row.today_net_pnl))
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
def _mark_is_fresh(row: Any) -> bool:
    """``True`` iff ``row`` has a mark that is not too old to trust — see
    ``_MARK_STALE_AFTER_SECONDS``. A row with no mark yet (``mark_age_seconds
    is None``) is not "fresh", it simply has nothing to show yet."""
    return (
        row.last_price is not None
        and row.mark_age_seconds is not None
        and freshness_note(row.mark_age_seconds, stale_after_seconds=_MARK_STALE_AFTER_SECONDS)
        is None
    )


def _render_live_positions(streamlit: Any, rows: tuple[Any, ...]) -> None:
    if not rows:
        streamlit.info("No open positions.")
        return
    any_stale = False
    table = []
    for r in rows:
        fresh = _mark_is_fresh(r)
        if r.last_price is not None and not fresh:
            any_stale = True
        points: float | None = None
        if fresh:
            points = (
                r.last_price - r.entry_price
                if r.side == "BUY"
                else r.entry_price - r.last_price
            )
        table.append(
            {
                "Strategy": r.strategy_id,
                "Mode": mode_label(r.execution_mode),
                "Instrument": r.instrument,
                "Side": r.side,
                "Quantity": r.quantity,
                "Entry time (IST)": format_ist(r.entry_time),
                "Entry price": format_inr(r.entry_price),
                "Current price": format_inr(r.last_price) if fresh else MISSING,
                "Points": format_inr(points),
                "MTM": format_inr(r.unrealised_pnl) if fresh else MISSING,
                "Stop": format_inr(r.stop_price) if r.stop_price is not None else MISSING,
                "Target": format_inr(r.target_price) if r.target_price is not None else MISSING,
                "Highest favourable": (
                    format_inr(r.highest_favourable)
                    if r.highest_favourable is not None
                    else MISSING
                ),
                "Lowest favourable": (
                    format_inr(r.lowest_favourable) if r.lowest_favourable is not None else MISSING
                ),
                "Duration": format_age(r.duration_seconds),
            }
        )
    streamlit.dataframe(table, hide_index=True, width="stretch")
    streamlit.caption(
        "Current price / points / MTM are the mark from this position's last "
        "closed candle, written by the strategy's own worker process — never "
        "invented from the entry price. Shown as — for a position with no "
        "mark yet (before its first candle has closed)"
        + (
            f", or where the mark is stale — older than "
            f"{format_age(_MARK_STALE_AFTER_SECONDS)} — and no longer shown as current."
            if any_stale
            else "."
        )
    )


# =================================================================== Baskets
def _render_baskets(
    streamlit: Any,
    baskets: tuple[BasketRow, ...],
    legs_by_basket: dict[str, tuple[LegRow, ...]],
    rolls_by_basket: dict[str, tuple[RollRow, ...]] | None = None,
    anchor_by_basket: dict[str, RollAnchorRow | None] | None = None,
) -> None:
    """Generic multi-leg basket/leg drill-down — reusable by any multi-leg
    strategy, not specific to straddle_920 or rolling_strangle_otm1.
    Read-only, typed data only. ``rolls_by_basket``/``anchor_by_basket``
    default ``None`` so every existing caller (and every existing test)
    keeps working unchanged; a caller that does not pass them simply shows
    no roll history section, exactly as before this was added."""
    if not baskets:
        streamlit.info("No basket for a multi-leg strategy on this date.")
        return
    rolls_by_basket = rolls_by_basket or {}
    anchor_by_basket = anchor_by_basket or {}
    for basket in baskets:
        with streamlit.expander(
            f"{basket.basket_id} — {basket.lifecycle_state} "
            f"(adjustments: {basket.adjustment_count})",
            expanded=True,
        ):
            top = streamlit.columns(4)
            top[0].metric("Entries consumed", "Yes" if basket.entries_consumed else "No")
            top[1].metric("Adjustment count", basket.adjustment_count)
            top[2].metric("Square-off", basket.square_off_state)
            top[3].metric(
                "Original basis",
                format_inr(basket.original_combined_basis)
                if basket.original_combined_basis is not None
                else MISSING,
            )
            if basket.day_blocked_reason:
                streamlit.warning(f"Day blocked: {basket.day_blocked_reason}")
            if basket.pending_replacement_role:
                streamlit.info(
                    f"Pending replacement: {basket.pending_replacement_role} "
                    f"({basket.pending_replacement_state or 'unknown state'})"
                )

            anchor = anchor_by_basket.get(basket.basket_id)
            rolls = rolls_by_basket.get(basket.basket_id, ())
            if anchor is not None or rolls:
                streamlit.caption(
                    "Reference anchor: "
                    + (
                        f"{format_inr(anchor.reference_price)} as of "
                        f"{anchor.anchor_candle_ts or MISSING}"
                        if anchor is not None and anchor.reference_price is not None
                        else MISSING
                    )
                )
            if rolls:
                rolls_table = [
                    {
                        "Role": roll.leg_role,
                        "Roll #": roll.roll_sequence,
                        "State": roll.lifecycle_state,
                        "From leg": roll.target_leg_id,
                        "From strike": (
                            roll.target_strike if roll.target_strike is not None else MISSING
                        ),
                        "To leg": roll.replacement_leg_id or MISSING,
                        "To strike": (
                            roll.replacement_strike
                            if roll.replacement_strike is not None
                            else MISSING
                        ),
                        "Trigger spot": (
                            format_inr(roll.reference_price_at_claim)
                            if roll.reference_price_at_claim is not None
                            else MISSING
                        ),
                        "Claim candle (IST)": roll.claim_candle_ts,
                        "Close correlation": roll.close_correlation_id or MISSING,
                    }
                    for roll in rolls
                ]
                streamlit.dataframe(rolls_table, hide_index=True, width="stretch")

            legs = legs_by_basket.get(basket.basket_id, ())
            if not legs:
                streamlit.caption("No leg recorded yet.")
                continue
            table = [
                {
                    "Leg": leg.leg_id,
                    "Role": leg.leg_role,
                    "Seq": leg.leg_sequence,
                    "Replacement": "Yes" if leg.is_replacement else "No",
                    "Security ID": leg.security_id or MISSING,
                    "Strike": leg.strike if leg.strike is not None else MISSING,
                    "Expiry": leg.expiry or MISSING,
                    "Side": leg.side or MISSING,
                    "Qty": leg.quantity if leg.quantity is not None else MISSING,
                    "Entry": (
                        format_inr(leg.entry_price) if leg.entry_price is not None else MISSING
                    ),
                    "Exit": format_inr(leg.exit_price) if leg.exit_price is not None else MISSING,
                    "State": leg.state,
                    "Exit reason": leg.exit_reason or MISSING,
                    "Gross realised": (
                        format_inr(leg.realized_gross_pnl)
                        if leg.realized_gross_pnl is not None
                        else MISSING
                    ),
                    "Charges": format_inr(leg.charges) if leg.charges is not None else MISSING,
                    "Net": format_inr(leg.net_pnl) if leg.net_pnl is not None else MISSING,
                }
                for leg in legs
            ]
            streamlit.dataframe(table, hide_index=True, width="stretch")
    streamlit.caption(
        "Gross unrealised P&L for a currently OPEN leg is not shown: no live "
        "mark-to-market is persisted for paper positions today, matching the "
        "same documented gap Open Positions has. Charges/net are read from "
        "the append-only trade ledger for a CLOSED leg only, for reporting — "
        "never used in the strategy's own gross-P&L risk triggers."
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
def _filter_by_outcome(
    trades: tuple[ClosedTradeRow, ...], outcome: tuple[str, ...]
) -> tuple[ClosedTradeRow, ...]:
    """Client-side, like the reference dashboard's own Outcome pills —
    "winning"/"losing" isn't a stored column, it's ``net_pnl``'s sign, so
    there's no SQL to push this into. Empty ``outcome`` means no filter."""
    if not outcome:
        return trades
    wanted = set(outcome)
    keep = []
    for t in trades:
        winning = t.net_pnl > 0
        matches = (winning and "Winning" in wanted) or (not winning and "Losing" in wanted)
        if matches:
            keep.append(t)
    return tuple(keep)


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


def _closed_trades_summary_table(trades: tuple[ClosedTradeRow, ...]) -> list[dict[str, object]]:
    """One row per strategy — Gross/Charges/Net P&L summed across every
    closed trade for that strategy in the current filter. Shown whether one
    strategy or "All Strategies" is selected: with one strategy selected
    this degrades to a single summary row, matching the individual-trades
    table below it. Strategies are ordered by first appearance in ``trades``
    (already ordered by ``load_closed_trades``)."""
    order: list[str] = []
    totals: dict[str, dict[str, float]] = {}
    for t in trades:
        bucket = totals.setdefault(
            t.strategy_id, {"count": 0.0, "gross": 0.0, "charges": 0.0, "net": 0.0}
        )
        if t.strategy_id not in order:
            order.append(t.strategy_id)
        bucket["count"] += 1
        bucket["gross"] += t.gross_pnl
        bucket["charges"] += t.charges
        bucket["net"] += t.net_pnl
    return [
        {
            "Strategy": strategy_id,
            "Trades": int(totals[strategy_id]["count"]),
            "Gross P&L": format_inr(totals[strategy_id]["gross"]),
            "Charges": format_inr(totals[strategy_id]["charges"]),
            "Net P&L": format_inr(totals[strategy_id]["net"]),
        }
        for strategy_id in order
    ]


def _render_closed_trades(streamlit: Any, trades: tuple[ClosedTradeRow, ...]) -> None:
    if not trades:
        streamlit.info("No closed trades in this range.")
        return
    streamlit.subheader("Summary by strategy")
    streamlit.dataframe(_closed_trades_summary_table(trades), hide_index=True, width="stretch")

    streamlit.subheader("Trades")
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
#: Table label for a ``strategy_id`` no longer declared by any config file.
#: Its ``trade_ledger`` history is real and stays counted; only the strategy
#: picker stops offering the id (see ``dashboards/data/strategy_scope.py``'s
#: 31 August 2026 note), so without this label a rename would look like a
#: strategy that mysteriously stopped rather than one that changed name.
RETIRED_LABEL = "retired (not in config)"

_COMBINED_SERIES = "Combined"

#: Streamlit's own dataframe metrics, used to size the per-strategy table
#: to its row count instead of letting it scroll internally.
_TABLE_ROW_PX = 35
_TABLE_HEADER_PX = 38

_DAY_COUNT_CAVEAT = (
    "\"Days ran\" counts distinct dates with a recorded runtime session for "
    "that strategy — days it was up, whether or not it fired. \"Days traded\" "
    "counts dates with at least one closed trade. They are counted "
    "independently and routinely differ; a selective strategy is not a "
    "stopped one. ₹/trading day divides net P&L by days traded, never by "
    "days ran."
)

_COMBINED_CAVEAT = (
    "The combined row is computed from every in-scope trade, never by adding "
    "the rows up. Win rate, profit factor, expectancy and max drawdown are "
    "not additive — two strategies drawing down on different days have a "
    "combined drawdown shallower than their sum, so adding them would "
    "overstate the portfolio's worst moment."
)


def _series_label(strategy_id: str, execution_mode: str, *, dual_mode: bool) -> str:
    """A chart/table series name. The mode is appended only for a strategy
    that appears in more than one mode in this window — with a single mode it
    is noise on every label."""
    return f"{strategy_id} · {execution_mode}" if dual_mode else strategy_id


def _dual_mode_ids(rows: tuple[StrategyPerformanceRow, ...]) -> set[str]:
    seen: dict[str, set[str]] = {}
    for row in rows:
        seen.setdefault(row.strategy_id, set()).add(row.execution_mode)
    return {sid for sid, modes in seen.items() if len(modes) > 1}


def _performance_table(breakdown: PerformanceBreakdown) -> list[dict[str, object]]:
    """The per-strategy table, TOTAL row appended last. Split out from the
    renderer so the numbers can be asserted without a Streamlit stub."""
    dual = _dual_mode_ids(breakdown.rows)
    table = []
    for row in (*breakdown.rows, breakdown.combined):
        combined = row is breakdown.combined
        m = row.metrics
        table.append(
            {
                "Strategy": "TOTAL" if combined else _series_label(
                    row.strategy_id, row.execution_mode, dual_mode=row.strategy_id in dual
                ),
                "Mode": mode_label(row.execution_mode),
                "Status": (
                    "all strategies"
                    if combined
                    else ("active" if row.configured else RETIRED_LABEL)
                ),
                "Days ran": row.days_ran,
                "Days traded": row.days_traded,
                "Trades": m.sample_size,
                "Win rate": format_pct(m.win_rate) if m.win_rate is not None else MISSING,
                "Profit factor": _fmt_ratio(m.profit_factor),
                "Expectancy": format_inr(m.expectancy) if m.expectancy is not None else MISSING,
                "Net P&L": format_inr(m.net_profit),
                "Charges": format_inr(m.total_charges),
                "Max drawdown": (
                    format_inr(m.max_drawdown) if m.max_drawdown is not None else MISSING
                ),
                "Avg win / loss": (
                    f"{format_inr(m.avg_win)} / {format_inr(m.avg_loss)}"
                    if m.avg_win is not None or m.avg_loss is not None
                    else MISSING
                ),
                "₹ / trading day": (
                    format_inr(row.pnl_per_trading_day)
                    if row.pnl_per_trading_day is not None
                    else MISSING
                ),
                "First trade": row.first_trade_date or MISSING,
                "Last trade": row.last_trade_date or MISSING,
                "Sample": "reliable" if m.reliable else f"insufficient (n={m.sample_size})",
            }
        )
    return table


def _render_equity_chart(
    streamlit: Any,
    breakdown: PerformanceBreakdown,
    curves: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> None:
    """One line per strategy plus a combined line, on a shared exit-time axis.

    Each series is seeded at zero at the window's first exit and
    forward-filled after its last, so a strategy that started trading late
    does not draw a line back through time it was never in, and one that
    stopped holds its final equity instead of vanishing.
    """
    import pandas as pd

    dual = _dual_mode_ids(breakdown.rows)
    series: dict[str, dict[str, float]] = {}
    # Iterate the breakdown's own rows, not the curves dict, so the legend
    # reads top-to-bottom in the same order as the table above it. A curve
    # with no points (a strategy that ran without trading) contributes no
    # series — a flat zero line would claim it broke even, not that it
    # never traded.
    for row in breakdown.rows:
        points = curves.get((row.strategy_id, row.execution_mode), ())
        if not points:
            continue
        label = _series_label(
            row.strategy_id, row.execution_mode, dual_mode=row.strategy_id in dual
        )
        series[label] = {ts: value for ts, value in points}

    if not series:
        streamlit.caption(
            "No closed trades in this range yet — the equity curve appears once there are."
        )
        return

    frame = pd.DataFrame(series)
    # Parse the ISO exit timestamps into a real datetime index. As plain
    # strings the axis is categorical: every one of the (up to hundreds of)
    # timestamps is drawn as its own rotated tick label, and points are
    # spaced evenly regardless of the real gaps between them, so an
    # overnight gap looks the same as two seconds.
    frame.index = pd.to_datetime(frame.index, format="mixed", utc=True)
    frame = frame.sort_index().ffill().fillna(0.0)
    # Summing the *cumulative* per-strategy series at each timestamp is
    # exactly the combined cumulative equity — the same figure
    # ``equity_curve`` over every trade would produce — because each
    # series is already forward-filled to that timestamp.
    frame[_COMBINED_SERIES] = frame.sum(axis=1)
    frame.index.name = "Exit time"
    streamlit.line_chart(frame)


def _render_performance(
    streamlit: Any,
    breakdown: PerformanceBreakdown,
    curves: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> None:
    combined = breakdown.combined
    metrics = combined.metrics

    streamlit.subheader("All strategies combined")
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
    row3 = streamlit.columns(4)
    # Distinct ids, not table rows: a strategy that has run in both modes
    # occupies two rows but is still one strategy.
    row3[0].metric("Strategies", len({r.strategy_id for r in breakdown.rows}))
    row3[1].metric("Days ran", f"{combined.days_ran}/{breakdown.eligible_days}")
    row3[2].metric("Days traded", f"{combined.days_traded}/{breakdown.eligible_days}")
    row3[3].metric(
        "₹ / trading day",
        format_inr(combined.pnl_per_trading_day)
        if combined.pnl_per_trading_day is not None
        else MISSING,
    )
    if not metrics.reliable:
        streamlit.warning(
            f"Only {metrics.sample_size} trade(s) in this range — statistics are not yet "
            f"a reliable sample (n≥{MIN_SAMPLE_SIZE} recommended)."
        )

    streamlit.subheader("Per strategy")
    if not breakdown.rows:
        streamlit.info("No strategy ran or traded in this range.")
        return
    table = _performance_table(breakdown)
    # Height the table to its own contents. Streamlit's default caps a
    # dataframe at ten rows and scrolls the rest inside its own frame, which
    # here would put the TOTAL row — the one row the reader most wants —
    # below the fold of an inner scrollbar they have no reason to expect.
    streamlit.dataframe(
        table,
        hide_index=True,
        width="stretch",
        height=_TABLE_HEADER_PX + len(table) * _TABLE_ROW_PX,
    )
    streamlit.caption(_DAY_COUNT_CAVEAT)
    streamlit.caption(_COMBINED_CAVEAT)
    if any(not r.configured for r in breakdown.rows):
        streamlit.caption(
            f"A row marked \"{RETIRED_LABEL}\" is a strategy id no config file "
            "declares any more — usually a rename. Its trades are real and stay "
            "counted here; only the Strategy picker above stops offering the id."
        )
    streamlit.caption(TRADING_DAY_CAVEAT)
    streamlit.download_button(
        "Download per-strategy performance (CSV)",
        data=to_csv_bytes(table),
        file_name="strategy_performance.csv",
        mime="text/csv",
    )

    streamlit.subheader("Equity curve")
    _render_equity_chart(streamlit, breakdown, curves)


# ========================================================= Strategy comparison
def _render_comparison(
    streamlit: Any, rows: tuple[Any, ...], *, active_strategy_id: str | None = None
) -> str | None:
    """Renders the comparison leaderboard. Returns a newly clicked
    strategy id when the dataframe's row-selection reports one, else
    ``None`` — the caller decides whether to write it into the page-level
    strategy-selector's session-state key and rerun."""
    if not rows:
        streamlit.info("No strategy has any closed trade in this range.")
        return None

    strategy_ids_in_view = sorted({r.strategy_id for r in rows})
    if len(strategy_ids_in_view) < 2:
        streamlit.caption(
            "Add at least one more strategy to the comparison above for a "
            "meaningful comparison — a single strategy's own numbers are "
            "shown here for reference, not ranked against anything."
        )

    table = []
    for r in rows:
        m = r.metrics
        table.append(
            {
                "Strategy": r.strategy_id,
                "Mode": mode_label(r.execution_mode),
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
    event = streamlit.dataframe(
        table,
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        key="io_comparison_table",
    )
    streamlit.caption(
        "Rankings are computed read-only, on demand, from closed trades in the "
        "selected range — nothing is written or snapshotted. ROI is shown only "
        "for a strategy that declares its own capital_base in config. Paper and "
        "live results for the same strategy are always shown as separate rows, "
        "never blended. Click a row to view that strategy in the tabs above."
    )
    if active_strategy_id is not None:
        streamlit.caption(f"Currently viewing: {active_strategy_id} in the tabs above.")
    streamlit.download_button(
        "Download comparison (CSV)",
        data=to_csv_bytes(table),
        file_name="strategy_comparison.csv",
        mime="text/csv",
    )

    if event:
        selected_rows = event.get("selection", {}).get("rows", [])
        if selected_rows:
            return str(rows[selected_rows[0]].strategy_id)
    return None


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

    options_result = run_bounded(
        database_path,
        lambda conn: discover_strategy_options(conn, paths.config_root, runtime_id),
    )
    all_options: tuple[StrategyOption, ...] = (
        () if isinstance(options_result, SnapshotUnavailable) else options_result
    )
    all_strategy_ids = tuple(o.strategy_id for o in all_options)

    style_choice = st.pills(
        "Style", _STYLE_LABELS, selection_mode="multi", key="io_style_filter"
    )
    wanted_styles = {_STYLE_VALUES[s] for s in style_choice} if style_choice else None
    options = (
        all_options
        if wanted_styles is None
        else tuple(o for o in all_options if o.style in wanted_styles)
    )

    selected_strategies = render_strategy_multiselect(st, options, key="io_strategy")

    tabs = st.tabs(
        [
            "Overview",
            "Open Positions",
            "Baskets",
            "Orders & Fills",
            "Closed Trades",
            "Performance",
            "Strategy Comparison",
            "Signals & Events",
        ]
    )

    with tabs[0]:

        @st.fragment(run_every=5)
        def _overview(strategy_ids: tuple[str, ...] = selected_strategies) -> None:
            overview = run_bounded(
                database_path,
                lambda conn: load_overview(
                    conn, runtime_id, trading_date, strategy_ids=strategy_ids or None
                ),
            )
            _render_overview(st, () if isinstance(overview, SnapshotUnavailable) else overview)
            if len(strategy_ids) == 1:
                _render_config_summary(st, paths.config_root, strategy_ids[0])

        _overview()

    with tabs[1]:
        positions_mode = _resolve_mode(st, "io_positions_mode")

        @st.fragment(run_every=5)
        def _positions(
            strategy_ids: tuple[str, ...] = selected_strategies,
            execution_mode: str | None = positions_mode,
        ) -> None:
            positions = run_bounded(
                database_path,
                lambda conn: load_live_positions(
                    conn,
                    runtime_id,
                    trading_date,
                    strategy_ids=strategy_ids or None,
                    execution_mode=execution_mode,
                ),
            )
            rows = () if isinstance(positions, SnapshotUnavailable) else positions
            _render_live_positions(st, rows)

        _positions()

    with tabs[2]:

        @st.fragment(run_every=5)
        def _baskets(strategy_ids: tuple[str, ...] = selected_strategies) -> None:
            if not strategy_ids:
                st.info("Select a strategy to see its baskets.")
                return
            if len(strategy_ids) > 1:
                st.info(
                    "Select exactly one strategy to see its baskets — this "
                    "view shows one strategy's legs/rolls in detail, not a "
                    "multi-strategy summary."
                )
                return
            strategy_id = strategy_ids[0]
            baskets_result = run_bounded(
                database_path,
                lambda conn: load_baskets(
                    conn,
                    strategy_id=strategy_id,
                    execution_mode="paper",
                    trading_date=trading_date,
                ),
            )
            baskets = () if isinstance(baskets_result, SnapshotUnavailable) else baskets_result

            def _load_all_legs(
                conn: Any, basket_ids: tuple[str, ...] = tuple(b.basket_id for b in baskets)
            ) -> dict[str, tuple[LegRow, ...]]:
                return {bid: load_legs_for_basket(conn, basket_id=bid) for bid in basket_ids}

            legs_result = run_bounded(database_path, _load_all_legs)
            legs_by_basket: dict[str, tuple[LegRow, ...]] = (
                {} if isinstance(legs_result, SnapshotUnavailable) else legs_result
            )

            def _load_all_rolls(
                conn: Any, basket_ids: tuple[str, ...] = tuple(b.basket_id for b in baskets)
            ) -> dict[str, tuple[RollRow, ...]]:
                return {bid: load_rolls_for_basket(conn, basket_id=bid) for bid in basket_ids}

            rolls_result = run_bounded(database_path, _load_all_rolls)
            rolls_by_basket: dict[str, tuple[RollRow, ...]] = (
                {} if isinstance(rolls_result, SnapshotUnavailable) else rolls_result
            )

            def _load_all_anchors(
                conn: Any, basket_ids: tuple[str, ...] = tuple(b.basket_id for b in baskets)
            ) -> dict[str, RollAnchorRow | None]:
                return {bid: load_roll_anchor(conn, basket_id=bid) for bid in basket_ids}

            anchor_result = run_bounded(database_path, _load_all_anchors)
            anchor_by_basket: dict[str, RollAnchorRow | None] = (
                {} if isinstance(anchor_result, SnapshotUnavailable) else anchor_result
            )
            _render_baskets(st, baskets, legs_by_basket, rolls_by_basket, anchor_by_basket)

        _baskets()

    with tabs[3]:
        orders_mode = _resolve_mode(st, "io_orders_mode")

        @st.fragment(run_every=5)
        def _orders(
            strategy_ids: tuple[str, ...] = selected_strategies,
            execution_mode: str | None = orders_mode,
        ) -> None:
            orders = run_bounded(
                database_path,
                lambda conn: load_orders(
                    conn,
                    runtime_id,
                    trading_date,
                    strategy_ids=strategy_ids or None,
                    execution_mode=execution_mode,
                ),
            )
            _render_orders(st, () if isinstance(orders, SnapshotUnavailable) else orders)

        _orders()

    with tabs[4]:
        start, end = _resolve_date_range(st, "closed_trades", today)
        closed_mode = _resolve_mode(st, "io_closed_mode")
        closed_outcome = st.pills(
            "Outcome", _OUTCOMES, selection_mode="multi", key="io_closed_outcome"
        )

        @st.fragment(run_every=30)
        def _closed(
            start: date = start,
            end: date = end,
            strategy_ids: tuple[str, ...] = selected_strategies,
            execution_mode: str | None = closed_mode,
            outcome: tuple[str, ...] = tuple(closed_outcome or ()),
        ) -> None:
            trades = run_bounded(
                database_path,
                lambda conn: load_closed_trades(
                    conn,
                    runtime_id,
                    strategy_ids=strategy_ids or None,
                    execution_mode=execution_mode,
                    start_date=start.isoformat(),
                    end_date=end.isoformat(),
                ),
            )
            rows = () if isinstance(trades, SnapshotUnavailable) else trades
            _render_closed_trades(st, _filter_by_outcome(rows, outcome))

        _closed()

    with tabs[5]:
        start, end = _resolve_date_range(st, "performance", today)
        performance_mode = _resolve_mode(st, "io_performance_mode")

        @st.fragment(run_every=30)
        def _performance(
            start: date = start,
            end: date = end,
            strategy_ids: tuple[str, ...] = selected_strategies,
            execution_mode: str | None = performance_mode,
        ) -> None:
            def _read(
                conn: Any,
            ) -> tuple[
                PerformanceBreakdown, dict[tuple[str, str], tuple[tuple[str, float], ...]]
            ]:
                """Breakdown and equity curves off one connection — the two
                reads must see the same snapshot, or the chart could show a
                trade the table has not counted."""
                breakdown = build_performance_breakdown(
                    conn,
                    runtime_id,
                    paths.config_root,
                    strategy_ids=strategy_ids or None,
                    execution_mode=execution_mode,
                    start_date=start.isoformat(),
                    end_date=end.isoformat(),
                )
                trades = load_closed_trades(
                    conn,
                    runtime_id,
                    strategy_ids=strategy_ids or None,
                    execution_mode=execution_mode,
                    start_date=start.isoformat(),
                    end_date=end.isoformat(),
                )
                return breakdown, equity_curves_by_strategy(trades)

            result = run_bounded(database_path, _read)
            if isinstance(result, SnapshotUnavailable):
                st.info(result.reason)
                return
            breakdown, curves = result
            _render_performance(st, breakdown, curves)

        _performance()

    with tabs[6]:
        start, end = _resolve_date_range(st, "comparison", today)
        compare_key = "io_compare_strategies"
        default_selection = list(all_strategy_ids)
        status_by_id = {o.strategy_id: o.status_label for o in all_options}
        compare_choice = st.multiselect(
            "Compare strategies",
            all_strategy_ids,
            default=default_selection,
            key=compare_key,
            format_func=lambda sid: f"{sid} ({status_by_id.get(sid, 'Unknown')})",
        )
        compare_ids = tuple(compare_choice)

        @st.fragment(run_every=30)
        def _comparison(
            start: date = start,
            end: date = end,
            compare_ids: tuple[str, ...] = compare_ids,
            active_strategy_id: str | None = (
                selected_strategies[0] if len(selected_strategies) == 1 else None
            ),
        ) -> None:
            if not compare_ids:
                st.info("Select at least one strategy above to compare.")
                return
            rows = run_bounded(
                database_path,
                lambda conn: build_strategy_comparison(
                    conn,
                    runtime_id,
                    paths.config_root,
                    compare_ids,
                    start_date=start.isoformat(),
                    end_date=end.isoformat(),
                ),
            )
            clicked = _render_comparison(
                st,
                () if isinstance(rows, SnapshotUnavailable) else rows,
                active_strategy_id=active_strategy_id,
            )
            if clicked is not None and st.session_state.get("io_strategy") != [clicked]:
                st.session_state["io_strategy"] = [clicked]
                st.rerun()

        _comparison()

    with tabs[7]:
        signals_severity = st.pills(
            "Severity", _SEVERITIES, selection_mode="multi", key="io_signals_severity"
        )

        @st.fragment(run_every=30)
        def _signals(
            strategy_ids: tuple[str, ...] = selected_strategies,
            severities: tuple[str, ...] = tuple(signals_severity or ()),
        ) -> None:
            signals = run_bounded(
                database_path,
                lambda conn: load_signals(
                    conn, runtime_id, trading_date, strategy_ids=strategy_ids or None
                ),
            )
            notifications = run_bounded(
                database_path,
                lambda conn: load_notifications(
                    conn, runtime_id, strategy_ids=strategy_ids or None
                ),
            )
            errors = run_bounded(
                database_path,
                lambda conn: load_errors(
                    conn,
                    runtime_id,
                    strategy_ids=strategy_ids or None,
                    severities=severities or None,
                ),
            )
            _render_signals(
                st,
                () if isinstance(signals, SnapshotUnavailable) else signals,
                () if isinstance(notifications, SnapshotUnavailable) else notifications,
                () if isinstance(errors, SnapshotUnavailable) else errors,
            )

        _signals()



if __name__ == "__main__":  # pragma: no cover
    main()
