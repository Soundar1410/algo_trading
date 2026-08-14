"""Pure calculation tests: calendar/trading-day math, performance metrics,
incident classification. No database, no Streamlit — every rule here is
testable in isolation, which is the point of keeping these functions pure.
"""

from __future__ import annotations

from datetime import date, timedelta

from pytest import approx as pytest_approx

from dashboards.data.calendar_stats import (
    DailyOutcome,
    build_thirty_day_rollup,
    execution_day_stats,
    is_trading_day,
    n_trading_days_back,
    trading_days,
)
from dashboards.data.incidents import EventErrorRow, classify_incidents
from dashboards.data.intraday_options import (
    MIN_SAMPLE_SIZE,
    ClosedTradeRow,
    compute_metrics,
    drawdown_curve,
    equity_curve,
    pnl_by_day,
    pnl_by_month,
)
from dashboards.formatting import format_age, format_inr, format_ist, format_signed_inr, parse_utc


# ============================================================== calendar
def test_weekends_are_not_trading_days():
    assert is_trading_day(date(2026, 8, 14)) is True  # Friday
    assert is_trading_day(date(2026, 8, 15)) is False  # Saturday
    assert is_trading_day(date(2026, 8, 16)) is False  # Sunday
    assert is_trading_day(date(2026, 8, 17)) is True  # Monday


def test_trading_days_returns_only_weekdays_inclusive():
    days = trading_days(date(2026, 8, 14), date(2026, 8, 17))  # Fri..Mon
    assert days == (date(2026, 8, 14), date(2026, 8, 17))


def test_trading_days_empty_when_start_after_end():
    assert trading_days(date(2026, 8, 17), date(2026, 8, 14)) == ()


def test_n_trading_days_back_counts_end_as_day_one():
    # end (a trading day) is itself the 1st most recent trading day.
    assert n_trading_days_back(date(2026, 8, 17), 1) == date(2026, 8, 17)


def test_n_trading_days_back_skips_the_weekend():
    # Monday 2026-08-17 is trading day #1 counting backward; #2 is Friday
    # 2026-08-14 — the weekend in between is skipped.
    assert n_trading_days_back(date(2026, 8, 17), 2) == date(2026, 8, 14)


def test_n_trading_days_back_zero_returns_end_unchanged():
    assert n_trading_days_back(date(2026, 8, 17), 0) == date(2026, 8, 17)


def test_execution_day_stats_distinguishes_ran_from_executed():
    outcomes = (
        DailyOutcome(trading_date=date(2026, 8, 10), ran=True, trade_count=2, net_pnl=100.0),
        DailyOutcome(trading_date=date(2026, 8, 11), ran=True, trade_count=0, net_pnl=0.0),
        # 2026-08-12 not present at all -> neither executed nor "ran"
    )
    stats = execution_day_stats(
        outcomes, window_start=date(2026, 8, 10), window_end=date(2026, 8, 12)
    )
    assert stats.eligible_trading_days == 3
    assert stats.executed_days == 1
    assert stats.skipped_days == 1
    assert stats.execution_pct == pytest_approx(1 / 3 * 100.0)

def test_thirty_day_rollup_none_when_never_started():
    assert build_thirty_day_rollup((), inception_date=None, today=date(2026, 8, 14)) is None


def test_thirty_day_rollup_flags_incomplete_when_fewer_than_thirty_calendar_days_elapsed():
    outcomes = (
        DailyOutcome(trading_date=date(2026, 8, 14), ran=True, trade_count=1, net_pnl=50.0),
    )
    rollup = build_thirty_day_rollup(
        outcomes, inception_date=date(2026, 8, 10), today=date(2026, 8, 14)
    )
    assert rollup is not None
    assert rollup.data_complete is False
    assert "30 calendar days" in rollup.completeness_note


def test_thirty_day_rollup_flags_gaps_even_after_thirty_calendar_days():
    inception = date(2026, 7, 1)
    today = date(2026, 8, 14)  # 45 calendar days later
    # Only one day of activity recorded in the whole window -> real gaps.
    outcomes = (
        DailyOutcome(trading_date=date(2026, 8, 10), ran=True, trade_count=1, net_pnl=10.0),
    )
    rollup = build_thirty_day_rollup(outcomes, inception_date=inception, today=today)
    assert rollup is not None
    assert rollup.data_complete is False
    assert "no recorded" in rollup.completeness_note


def test_thirty_day_rollup_complete_when_every_eligible_day_accounted_for():
    inception = date(2026, 7, 1)
    today = date(2026, 8, 14)
    window_start = max(inception, today - timedelta(days=29))
    days = trading_days(window_start, today)
    outcomes = tuple(
        DailyOutcome(trading_date=d, ran=True, trade_count=1, net_pnl=10.0) for d in days
    )
    rollup = build_thirty_day_rollup(outcomes, inception_date=inception, today=today)
    assert rollup is not None
    assert rollup.data_complete is True
    assert rollup.trading_days_elapsed == len(days)
    assert rollup.days_with_trades == len(days)
    assert rollup.cumulative_net_pnl == len(days) * 10.0


# ============================================================ performance
def _trade(net_pnl: float, exit_time: str = "2026-08-10T10:00:00+00:00", charges: float = 0.0):
    return ClosedTradeRow(
        strategy_id="s1",
        execution_mode="paper",
        instrument="NIFTY",
        security_id="13",
        trading_date="2026-08-10",
        side="BUY",
        quantity=75,
        entry_price=100.0,
        exit_price=110.0,
        points=10.0,
        gross_pnl=net_pnl + charges,
        charges=charges,
        net_pnl=net_pnl,
        entry_time="2026-08-10T09:20:00+00:00",
        exit_time=exit_time,
    )


def test_compute_metrics_on_no_trades_is_all_zero_not_none_sample():
    metrics = compute_metrics(())
    assert metrics.sample_size == 0
    assert metrics.win_rate is None
    assert metrics.profit_factor is None
    assert metrics.reliable is False


def test_compute_metrics_win_rate_and_profit_factor():
    trades = (_trade(100.0), _trade(-50.0), _trade(50.0))
    metrics = compute_metrics(trades)
    assert metrics.sample_size == 3
    assert metrics.wins == 2
    assert metrics.losses == 1
    assert metrics.win_rate == pytest_approx(2 / 3 * 100.0)
    assert metrics.profit_factor == pytest_approx(150.0 / 50.0)
    assert metrics.net_profit == 100.0


def test_compute_metrics_infinite_profit_factor_when_no_losses():
    trades = (_trade(100.0), _trade(50.0))
    metrics = compute_metrics(trades)
    assert metrics.profit_factor == float("inf")


def test_compute_metrics_below_minimum_sample_is_flagged_unreliable():
    trades = tuple(_trade(10.0) for _ in range(MIN_SAMPLE_SIZE - 1))
    metrics = compute_metrics(trades)
    assert metrics.sample_size == MIN_SAMPLE_SIZE - 1
    assert metrics.reliable is False


def test_compute_metrics_at_minimum_sample_is_reliable():
    trades = tuple(_trade(10.0) for _ in range(MIN_SAMPLE_SIZE))
    metrics = compute_metrics(trades)
    assert metrics.reliable is True


def test_equity_curve_is_cumulative_ordered_by_exit_time():
    trades = (
        _trade(100.0, exit_time="2026-08-11T10:00:00+00:00"),
        _trade(-30.0, exit_time="2026-08-10T10:00:00+00:00"),
    )
    curve = equity_curve(trades)
    assert [v for _, v in curve] == [-30.0, 70.0]


def test_drawdown_curve_never_exceeds_zero():
    trades = (
        _trade(100.0, exit_time="2026-08-10T10:00:00+00:00"),
        _trade(-150.0, exit_time="2026-08-11T10:00:00+00:00"),
    )
    equity = equity_curve(trades)
    drawdown = drawdown_curve(equity)
    values = [v for _, v in drawdown]
    assert values[0] == 0.0
    assert values[1] == -150.0
    assert min(values) == -150.0


def test_pnl_by_day_and_month_group_correctly():
    trades = (
        _trade(10.0, exit_time="2026-08-10T10:00:00+00:00"),
        _trade(20.0, exit_time="2026-08-10T14:00:00+00:00"),
        _trade(5.0, exit_time="2026-09-01T10:00:00+00:00"),
    )
    by_day = dict(pnl_by_day(trades))
    by_month = dict(pnl_by_month(trades))
    assert by_day["2026-08-10"] == 30.0
    assert by_day["2026-09-01"] == 5.0
    assert by_month["2026-08"] == 30.0
    assert by_month["2026-09"] == 5.0


# =============================================================== incidents
def test_a_recovered_feed_error_is_not_shown_as_active():
    """The exact bug the spec names: an old feed outage, already recovered,
    must not read as a currently-active incident."""
    errors = (
        EventErrorRow(
            strategy_id=None,
            execution_mode=None,
            severity="ERROR",
            component="feed",
            message="disconnected",
            occurred_at="2026-08-14T04:00:00+00:00",
        ),
    )
    classified = classify_incidents(
        errors,
        broker_healthy=True,
        database_healthy=True,
        feed_currently_ok=True,  # a clean restart happened since
        strategy_health_states={},
    )
    assert len(classified) == 1
    assert classified[0].active is False


def test_the_latest_feed_error_is_active_when_the_feed_is_still_down():
    errors = (
        EventErrorRow(None, None, "ERROR", "feed", "disconnected", "2026-08-14T08:00:00+00:00"),
    )
    classified = classify_incidents(
        errors,
        broker_healthy=True,
        database_healthy=True,
        feed_currently_ok=False,
        strategy_health_states={},
    )
    assert classified[0].active is True


def test_only_the_latest_error_per_group_can_be_active():
    errors = (
        EventErrorRow(None, None, "ERROR", "feed", "disconnected (2)", "2026-08-14T08:00:00+00:00"),
        EventErrorRow(None, None, "ERROR", "feed", "disconnected (1)", "2026-08-14T04:00:00+00:00"),
    )
    classified = classify_incidents(
        errors, broker_healthy=True, database_healthy=True, feed_currently_ok=False,
        strategy_health_states={},
    )
    by_message = {c.message: c.active for c in classified}
    assert by_message["disconnected (2)"] is True
    assert by_message["disconnected (1)"] is False


def test_a_strategy_error_resolves_once_the_strategy_is_running_again():
    errors = (
        EventErrorRow("st01", "paper", "ERROR", "engine", "crashed", "2026-08-14T04:00:00+00:00"),
    )
    classified = classify_incidents(
        errors, broker_healthy=True, database_healthy=True, feed_currently_ok=True,
        strategy_health_states={"st01": "RUNNING_PAPER"},
    )
    assert classified[0].active is False


# ============================================================== formatting
def test_format_inr_uses_indian_digit_grouping():
    assert format_inr(1234567.891, decimals=2) == "₹12,34,567.89"
    assert format_inr(1000.0, decimals=0) == "₹1,000"
    assert format_inr(-500.0) == "₹-500.00"


def test_format_inr_missing_for_none():
    assert format_inr(None) == "—"


def test_format_signed_inr_shows_explicit_sign():
    assert format_signed_inr(100.0).startswith("+")
    assert format_signed_inr(-100.0).startswith("-")


def test_format_age_scales_with_magnitude():
    assert format_age(45) == "45s"
    assert format_age(125) == "2m 05s"
    assert format_age(3 * 3600 + 61) == "3h 01m"
    assert format_age(None) == "—"


def test_format_ist_converts_utc_to_ist_with_offset():
    # 2026-08-14T04:00:00+00:00 UTC == 09:30 IST (UTC+5:30).
    assert format_ist("2026-08-14T04:00:00+00:00") == "2026-08-14 09:30:00 IST"


def test_parse_utc_rejects_garbage_without_raising():
    assert parse_utc("not-a-timestamp") is None
    assert parse_utc(None) is None
