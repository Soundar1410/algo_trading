"""Render tests for ``dashboards/intraday_options.py``'s eight tabs.

Every ``_render_*`` function takes the streamlit module as a plain
parameter, so each is exercised here with :class:`FakeStreamlit` — no real
Streamlit process, no database. Empty-state and populated-state are both
covered for every tab; DB-backed value correctness lives in
``test_dashboard_intraday_options_data.py``.
"""

from __future__ import annotations

from datetime import date, timedelta

from _dashboard_fakes import FakeStreamlit

import dashboards.intraday_options as page
from dashboards.data.incidents import EventErrorRow
from dashboards.data.intraday_options import (
    ClosedTradeRow,
    ComparisonRow,
    FillRow,
    LatestError,
    LivePositionRow,
    NotificationRow,
    OrderRow,
    OverviewRow,
    PerformanceBreakdown,
    SignalRow,
    StrategyPerformanceRow,
    compute_metrics,
    equity_curves_by_strategy,
)


# ============================================================== Overview
def test_overview_empty_state():
    st = FakeStreamlit()
    page._render_overview(st, ())
    assert any("no strategy" in i.lower() or "heartbeat" in i.lower() for i in st.infos)


def test_overview_shows_current_position_and_flags_blocked_entries():
    st = FakeStreamlit()
    row = OverviewRow(
        strategy_id="st01",
        execution_mode="paper",
        health_state="RUNNING_PAPER",
        heartbeat_age_seconds=2.0,
        pid=123,
        entries_blocked=True,
        square_off_state="PENDING",
        open_positions=1,
        current_position_instrument="NIFTY",
        current_position_quantity=75,
        current_position_side="BUY",
        today_trade_count=1,
        today_net_pnl=500.0,
        latest_error=LatestError(
            message="boom", severity="ERROR", occurred_at="2026-08-14T04:00:00+00:00"
        ),
    )
    page._render_overview(st, (row,))
    assert any("entries blocked" in w for w in st.warnings)
    assert any("BUY 75" in w for w in st.writes)
    assert any("boom" in e for e in st.errors)


# =========================================================== Live positions
def test_live_positions_empty_state():
    st = FakeStreamlit()
    page._render_live_positions(st, ())
    assert any("no open positions" in i.lower() for i in st.infos)


def _live_position_row(**overrides: object) -> LivePositionRow:
    defaults: dict[str, object] = dict(
        strategy_id="st01", execution_mode="paper", instrument="NIFTY", security_id="13",
        side="BUY", quantity=75, entry_time="2026-08-14T04:00:00+00:00", entry_price=100.0,
        stop_price=None, target_price=None, highest_favourable=None, lowest_favourable=None,
        duration_seconds=120.0, last_price=None, unrealised_pnl=None, marked_at=None,
        mark_age_seconds=None,
    )
    defaults.update(overrides)
    return LivePositionRow(**defaults)  # type: ignore[arg-type]


def test_live_positions_never_fabricates_current_price_with_no_mark():
    st = FakeStreamlit()
    row = _live_position_row()
    page._render_live_positions(st, (row,))
    table = st.dataframes[0]
    assert table[0]["Current price"] == "—"
    assert table[0]["MTM"] == "—"


def test_live_positions_shows_a_fresh_mark():
    st = FakeStreamlit()
    row = _live_position_row(
        last_price=105.0, unrealised_pnl=375.0, marked_at="2026-08-14T04:04:30+00:00",
        mark_age_seconds=30.0,
    )
    page._render_live_positions(st, (row,))
    table = st.dataframes[0]
    assert table[0]["Current price"] == "₹105.00"
    assert table[0]["Points"] == "₹5.00"
    assert table[0]["MTM"] == "₹375.00"


def test_live_positions_never_shows_a_stale_mark_as_current():
    """A mark older than ``_MARK_STALE_AFTER_SECONDS`` must not be shown as
    current -- exactly the "stale value shown as current" the spec forbids,
    even though a value technically exists in the row."""
    st = FakeStreamlit()
    row = _live_position_row(
        last_price=105.0,
        unrealised_pnl=375.0,
        marked_at="2026-08-14T03:00:00+00:00",
        mark_age_seconds=page._MARK_STALE_AFTER_SECONDS + 1.0,
    )
    page._render_live_positions(st, (row,))
    table = st.dataframes[0]
    assert table[0]["Current price"] == "—"
    assert table[0]["MTM"] == "—"
    assert any("stale" in c.lower() for c in st.captions)


# ============================================================ Orders & fills
def test_orders_empty_state():
    st = FakeStreamlit()
    page._render_orders(st, ())
    assert any("no orders" in i.lower() for i in st.infos)


def test_orders_render_shows_fills_count_and_charges():
    st = FakeStreamlit()
    fill = FillRow(
        broker_fill_id="f1", quantity=75, price=100.0, reference_price=100.0,
        slippage_amount=0.05, latency_ms=250, fill_method="market", charges=12.5,
        filled_at="2026-08-14T04:00:00+00:00",
    )
    order = OrderRow(
        correlation_id="p_st01_1", strategy_id="st01", execution_mode="paper",
        intent_time="2026-08-14T03:59:00+00:00", instrument="NIFTY", security_id="13",
        side="BUY", quantity=75, order_type="MARKET", status="FILLED", broker_order_id=None,
        filled_quantity=75, average_fill_price=100.0, rejection_reason=None, fills=(fill,),
    )
    page._render_orders(st, (order,))
    table = st.dataframes[0]
    assert table[0]["Fills"] == 1
    assert table[0]["Charges"] == "₹12.50"


# ============================================================= Closed trades
def test_closed_trades_empty_state():
    st = FakeStreamlit()
    page._render_closed_trades(st, ())
    assert any("no closed trades" in i.lower() for i in st.infos)


def _closed_trade(net_pnl: float = 500.0, *, strategy_id: str = "st01") -> ClosedTradeRow:
    return ClosedTradeRow(
        strategy_id=strategy_id, execution_mode="paper", instrument="NIFTY", security_id="13",
        trading_date="2026-08-14", side="BUY", quantity=75, entry_price=100.0,
        exit_price=110.0, points=10.0, gross_pnl=net_pnl + 10.0, charges=10.0, net_pnl=net_pnl,
        entry_time="2026-08-14T03:45:00+00:00", exit_time="2026-08-14T04:00:00+00:00",
    )


def test_closed_trades_render_and_csv_export():
    st = FakeStreamlit()
    page._render_closed_trades(st, (_closed_trade(),))
    # Summary-by-strategy table, then the individual-trades table.
    assert len(st.dataframes) == 2
    assert len(st.download_buttons) == 1
    _label, data = st.download_buttons[0]
    assert b"Net P" in data


def test_closed_trades_summary_sums_gross_charges_net_per_strategy():
    st = FakeStreamlit()
    trades = (
        _closed_trade(500.0, strategy_id="c921_ema_cross_buy"),
        _closed_trade(-200.0, strategy_id="c921_ema_cross_buy"),
        _closed_trade(300.0, strategy_id="c509_ema_cross_buy"),
    )
    page._render_closed_trades(st, trades)
    summary = st.dataframes[0]
    assert len(summary) == 2  # one row per strategy, not per trade
    by_strategy = {row["Strategy"]: row for row in summary}
    assert by_strategy["c921_ema_cross_buy"]["Trades"] == 2
    assert by_strategy["c921_ema_cross_buy"]["Gross P&L"] == "₹320.00"  # (510) + (-190)
    assert by_strategy["c921_ema_cross_buy"]["Charges"] == "₹20.00"  # 10 + 10
    assert by_strategy["c921_ema_cross_buy"]["Net P&L"] == "₹300.00"  # 500 + (-200)
    assert by_strategy["c509_ema_cross_buy"]["Trades"] == 1
    assert by_strategy["c509_ema_cross_buy"]["Net P&L"] == "₹300.00"

    # Individual trades still render below the summary, unaggregated.
    trades_table = st.dataframes[1]
    assert len(trades_table) == 3


# ============================================================= Performance
def _perf_row(
    strategy_id: str = "st01",
    *,
    trades: tuple[ClosedTradeRow, ...] = (),
    configured: bool = True,
    days_ran: int = 1,
    execution_mode: str = "paper",
) -> StrategyPerformanceRow:
    dates = sorted({t.trading_date for t in trades})
    return StrategyPerformanceRow(
        strategy_id=strategy_id,
        execution_mode=execution_mode,
        configured=configured,
        days_ran=days_ran,
        days_traded=len(dates),
        first_trade_date=dates[0] if dates else None,
        last_trade_date=dates[-1] if dates else None,
        metrics=compute_metrics(trades),
    )


def _breakdown(
    *rows: StrategyPerformanceRow,
    trades: tuple[ClosedTradeRow, ...] = (),
    eligible_days: int = 5,
) -> PerformanceBreakdown:
    dates = sorted({t.trading_date for t in trades})
    combined = StrategyPerformanceRow(
        strategy_id="ALL",
        execution_mode="paper",
        configured=True,
        days_ran=max((r.days_ran for r in rows), default=0),
        days_traded=len(dates),
        first_trade_date=dates[0] if dates else None,
        last_trade_date=dates[-1] if dates else None,
        metrics=compute_metrics(trades),
    )
    return PerformanceBreakdown(rows=rows, combined=combined, eligible_days=eligible_days)


def test_performance_flags_insufficient_sample():
    st = FakeStreamlit()
    trades = (_closed_trade(),)
    page._render_performance(st, _breakdown(_perf_row(trades=trades), trades=trades), {})
    assert any("not yet" in w and "reliable" in w for w in st.warnings)


def test_performance_table_ends_with_a_total_row_that_matches_the_combined_metrics():
    st = FakeStreamlit()
    alpha = tuple(_closed_trade(100.0, strategy_id="alpha") for _ in range(4))
    beta = tuple(_closed_trade(-50.0, strategy_id="beta") for _ in range(2))
    breakdown = _breakdown(
        _perf_row("alpha", trades=alpha, days_ran=3),
        _perf_row("beta", trades=beta, days_ran=2),
        trades=alpha + beta,
    )

    page._render_performance(st, breakdown, equity_curves_by_strategy(alpha + beta))

    table = st.dataframes[0]
    assert [row["Strategy"] for row in table] == ["alpha", "beta", "TOTAL"]
    assert table[-1]["Trades"] == 6
    assert table[-1]["Trades"] == sum(row["Trades"] for row in table[:-1])
    # Days ran is per strategy, never summed into the total by the renderer.
    assert table[0]["Days ran"] == 3
    assert table[-1]["Days ran"] == breakdown.combined.days_ran


def test_performance_marks_a_strategy_id_no_config_declares():
    st = FakeStreamlit()
    trades = tuple(_closed_trade(100.0, strategy_id="supertrend_buy_1_1p2") for _ in range(2))
    breakdown = _breakdown(
        _perf_row("supertrend_buy_1_1p2", trades=trades, configured=False), trades=trades
    )

    page._render_performance(st, breakdown, equity_curves_by_strategy(trades))

    table = st.dataframes[0]
    assert table[0]["Status"] == page.RETIRED_LABEL
    assert any("rename" in c for c in st.captions)


def test_performance_chart_carries_a_combined_series():
    st = FakeStreamlit()
    alpha = tuple(_closed_trade(100.0, strategy_id="alpha") for _ in range(3))
    beta = tuple(_closed_trade(-50.0, strategy_id="beta") for _ in range(3))
    breakdown = _breakdown(
        _perf_row("alpha", trades=alpha), _perf_row("beta", trades=beta), trades=alpha + beta
    )

    page._render_performance(st, breakdown, equity_curves_by_strategy(alpha + beta))

    assert len(st.charts) == 1
    columns = list(st.charts[0].columns)
    assert columns == ["alpha", "beta", "Combined"]
    # The combined line is the per-timestamp sum of the strategy lines.
    last = st.charts[0].iloc[-1]
    assert last["Combined"] == last["alpha"] + last["beta"]


def test_performance_reports_a_strategy_that_ran_without_trading():
    st = FakeStreamlit()
    traded = tuple(_closed_trade(100.0, strategy_id="alpha") for _ in range(5))
    breakdown = _breakdown(
        _perf_row("alpha", trades=traded, days_ran=4),
        _perf_row("quiet", trades=(), days_ran=4),
        trades=traded,
    )

    page._render_performance(st, breakdown, equity_curves_by_strategy(traded))

    quiet = next(row for row in st.dataframes[0] if row["Strategy"] == "quiet")
    assert quiet["Days ran"] == 4
    assert quiet["Days traded"] == 0
    assert quiet["Trades"] == 0
    assert any("Days traded" in c and "Days ran" in c for c in st.captions)


def test_performance_empty_state():
    st = FakeStreamlit()
    page._render_performance(st, _breakdown(), {})
    assert any("no strategy" in i.lower() for i in st.infos)
    assert st.charts == []


# ========================================================= Strategy comparison
def test_comparison_empty_state():
    st = FakeStreamlit()
    page._render_comparison(st, ())
    assert any("no strategy" in i.lower() for i in st.infos)


def _comparison_row(strategy_id: str = "st01", execution_mode: str = "paper") -> ComparisonRow:
    return ComparisonRow(
        strategy_id=strategy_id,
        execution_mode=execution_mode,
        metrics=compute_metrics((_closed_trade(),)),
        execution_days=1,
        eligible_days=1,
        roi_pct=None,
    )


def test_comparison_shows_insufficient_sample_label():
    st = FakeStreamlit()
    page._render_comparison(st, (_comparison_row(),))
    table = st.dataframes[0]
    assert "insufficient" in table[0]["Sample"]
    assert table[0]["ROI %"] == "—"
    assert any("at least one more strategy" in c for c in st.captions)


def test_comparison_with_two_strategies_does_not_show_insufficient_caption():
    st = FakeStreamlit()
    page._render_comparison(
        st, (_comparison_row("st01"), _comparison_row("st02"))
    )
    assert not any("at least one more strategy" in c for c in st.captions)


def test_comparison_never_blends_paper_and_live_into_one_row():
    st = FakeStreamlit()
    page._render_comparison(
        st,
        (
            _comparison_row("st01", execution_mode="paper"),
            _comparison_row("st01", execution_mode="live"),
        ),
    )
    table = st.dataframes[0]
    assert len(table) == 2
    modes = {row["Mode"] for row in table}
    assert modes == {"PAPER — simulated", "LIVE — real money"}


def test_comparison_row_click_returns_the_clicked_strategy_id():
    st = FakeStreamlit()
    st.dataframe_selection = {"selection": {"rows": [1]}}
    clicked = page._render_comparison(st, (_comparison_row("st01"), _comparison_row("st02")))
    assert clicked == "st02"


def test_comparison_no_click_returns_none():
    st = FakeStreamlit()
    clicked = page._render_comparison(st, (_comparison_row("st01"), _comparison_row("st02")))
    assert clicked is None


# ================================================================ Signals
def test_signals_empty_states():
    st = FakeStreamlit()
    page._render_signals(st, (), (), ())
    assert any("no signals" in i.lower() for i in st.infos)
    assert any("no notifications" in c.lower() for c in st.captions)
    assert any("no errors" in c.lower() for c in st.captions)


def test_signals_render_with_data():
    st = FakeStreamlit()
    signal = SignalRow(
        strategy_id="st01", execution_mode="paper", instrument="NIFTY", side="BUY",
        candle_open=99.0, candle_high=101.0, candle_low=98.5, candle_close=100.0,
        candle_start_at="2026-08-14T03:59:00+00:00", candle_end_at="2026-08-14T04:00:00+00:00",
        reference_price=100.0, evaluated_at="2026-08-14T04:00:00+00:00", reason="ema_cross",
        order_correlation_id="p_st01_1",
    )
    notification = NotificationRow(
        strategy_id="st01", execution_mode="paper", channel="telegram",
        event_type="order_filled", message="filled", delivered=True, failure_reason=None,
        created_at="2026-08-14T04:00:00+00:00",
    )
    error = EventErrorRow(
        strategy_id="st01", execution_mode="paper", severity="ERROR", component="engine",
        message="boom", occurred_at="2026-08-14T04:00:00+00:00",
    )
    page._render_signals(st, (signal,), (notification,), (error,))
    assert len(st.dataframes) == 3


# ======================================================== Date-range presets
def test_the_date_range_presets_offer_yesterday_between_today_and_the_windows():
    """The selector's own option list, in order — ``Yesterday`` sits next to
    ``Today`` rather than after the multi-day windows."""
    assert page._PRESETS == (
        "Today",
        "Yesterday",
        "Last 7 trading days",
        "Last 30 trading days",
        "Custom",
    )


def test_today_preset_resolves_to_a_single_day():
    st = FakeStreamlit()
    st.selectbox_returns = {"closed_trades_preset": "Today"}
    start, end = page._resolve_date_range(st, "closed_trades", date(2026, 9, 8))
    assert (start, end) == (date(2026, 9, 8), date(2026, 9, 8))


def test_yesterday_preset_resolves_to_the_previous_trading_day_only():
    """A single-day range, and it is the *previous* day — not today, and not a
    two-day window that would silently include today's trades."""
    st = FakeStreamlit()
    st.selectbox_returns = {"closed_trades_preset": "Yesterday"}
    # Tuesday 2026-09-08 -> Monday 2026-09-07.
    start, end = page._resolve_date_range(st, "closed_trades", date(2026, 9, 8))
    assert start == end == date(2026, 9, 7)


def test_yesterday_preset_is_offered_on_every_tab_that_takes_a_date_range():
    """Closed Trades, Performance and Strategy Comparison share one resolver,
    so the preset must reach all three — asserted through the widget each tab
    actually builds, not by reading the shared constant twice."""
    for key in ("closed_trades", "performance", "comparison"):
        st = FakeStreamlit()
        st.selectbox_returns = {f"{key}_preset": "Yesterday"}
        start, end = page._resolve_date_range(st, key, date(2026, 9, 8))
        assert start == end == date(2026, 9, 7), key
        label, options, kwargs = st.selectbox_calls[0]
        assert label == "Date range"
        assert "Yesterday" in options
        assert kwargs["key"] == f"{key}_preset"


def test_yesterday_on_a_monday_walks_back_over_the_weekend_to_friday():
    """The reason this is not calendar-yesterday: a literal previous day would
    make the preset show an empty Sunday every Monday."""
    assert page._previous_trading_day(date(2026, 9, 7)) == date(2026, 9, 4)


def test_yesterday_on_a_weekend_resolves_to_the_last_session_not_another_weekend_day():
    """Saturday's calendar-yesterday (Friday) happens to be a session, but
    Sunday's (Saturday) is not — both must land on Friday."""
    assert page._previous_trading_day(date(2026, 9, 12)) == date(2026, 9, 11)  # Sat -> Fri
    assert page._previous_trading_day(date(2026, 9, 13)) == date(2026, 9, 11)  # Sun -> Fri


def test_previous_trading_day_is_always_strictly_before_today():
    """The invariant that separates Yesterday from Today: never the same date,
    on any day of the week, including when today itself is not a session."""
    for offset in range(14):
        today = date(2026, 9, 7) + timedelta(days=offset)
        assert page._previous_trading_day(today) < today, today
