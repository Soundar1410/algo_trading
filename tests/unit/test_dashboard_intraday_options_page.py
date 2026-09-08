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
    SignalRow,
    compute_metrics,
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
def test_performance_flags_insufficient_sample():
    from datetime import date

    st = FakeStreamlit()
    page._render_performance(st, (_closed_trade(),), (), date(2026, 8, 14), date(2026, 8, 14))
    assert any("not yet" in w and "reliable" in w for w in st.warnings)


def test_performance_charts_render_for_populated_trades():
    st = FakeStreamlit()
    from datetime import date

    trades = tuple(_closed_trade(100.0) for _ in range(6))
    page._render_performance(st, trades, (), date(2026, 8, 14), date(2026, 8, 14))
    assert st.warnings == [] or all("reliable" not in w for w in st.warnings)
    assert len(st.charts) >= 2


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
