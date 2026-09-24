"""Phase 4a: the paper fill model (spec section 8).

Official open of the execution session; costs through the rules' own fill
function; a symbol not traded that session fills at its next session's open,
flagged; nothing through the week leaves the order pending. ``at_week_open``
comes from the committed holiday calendar (spec 4.9 v1.2g).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from runtimes.positional_stocks.paper_fills import execution_date_after, fill_orders, plan_fill
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    DailyBar,
    OrderAction,
    PendingOrder,
    PositionState,
    RulesParameters,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import sizing
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar

CALENDAR = TradingCalendar.from_config(Path(__file__).resolve().parents[2] / "config")
PARAMS = RulesParameters()  # spec 8's default costs: 12 bps buy, 11 bps + Rs 15 sell
D = Decimal

MON = date(2026, 9, 21)  # 2026-W39, an ordinary week
FRI = date(2026, 9, 25)


def _bar(day: date, open_: float, close: float | None = None) -> DailyBar:
    c = open_ if close is None else close
    return DailyBar(day, open_, max(open_, c), min(open_, c), c, 1000.0)


def _entry(symbol: str = "X", on: date = MON) -> PendingOrder:
    size = sizing(0.06, False, PARAMS)
    return PendingOrder(
        OrderAction.BUY_T1,
        symbol,
        (2026, 38),
        on,
        "entry",
        amount=size.tranche_amounts[0],
        sizing=size,
        sector="S",
        group=symbol,
    )


def test_an_order_fills_at_the_execution_sessions_open() -> None:
    plan = plan_fill(_entry(), [_bar(MON, 1000.0, 1010.0)], through=FRI, calendar=CALENDAR)
    assert plan is not None
    assert (plan.session, plan.open_price) == (MON, 1000.0)
    assert plan.at_week_open and not plan.not_traded_on_execution_session


def test_after_a_monday_holiday_tuesday_is_the_weeks_open() -> None:
    # Mon 26 Jan 2026 is Republic Day: orders decided on Fri 23 Jan execute Tue 27.
    tuesday = execution_date_after(date(2026, 1, 23), CALENDAR)
    assert tuesday == date(2026, 1, 27)
    plan = plan_fill(
        _entry(on=tuesday), [_bar(tuesday, 500.0)], through=date(2026, 1, 30), calendar=CALENDAR
    )
    assert plan is not None and plan.at_week_open and not plan.not_traded_on_execution_session


def test_not_traded_on_the_execution_session_fills_next_session_flagged() -> None:
    wednesday = MON + timedelta(days=2)
    plan = plan_fill(
        _entry(),
        [_bar(wednesday, 990.0), _bar(wednesday + timedelta(days=1), 995.0)],
        through=FRI,
        calendar=CALENDAR,
    )
    assert plan is not None
    assert (plan.session, plan.open_price) == (wednesday, 990.0)
    assert plan.not_traded_on_execution_session
    assert not plan.at_week_open  # the next level's touch window starts next week


def test_no_session_through_the_week_leaves_the_order_pending() -> None:
    assert plan_fill(_entry(), [], through=FRI, calendar=CALENDAR) is None
    later = [_bar(FRI + timedelta(days=3), 1000.0)]
    assert plan_fill(_entry(), later, through=FRI, calendar=CALENDAR) is None
    earlier = [_bar(MON - timedelta(days=3), 1000.0)]
    assert plan_fill(_entry(), earlier, through=FRI, calendar=CALENDAR) is None


def test_a_mid_week_fill_is_recorded_on_the_tranche() -> None:
    wednesday = MON + timedelta(days=2)
    results, book = fill_orders(
        [_entry()],
        {},
        {"X": [_bar(wednesday, 1000.0)]},
        through=FRI,
        calendar=CALENDAR,
        params=PARAMS,
    )
    (position,) = book.values()
    assert position.buys[0].session == wednesday
    assert position.buys[0].at_week_open is False
    assert results[0].plan is not None and results[0].plan.not_traded_on_execution_session


def test_costs_follow_section_8() -> None:
    results, book = fill_orders(
        [_entry()], {}, {"X": [_bar(MON, 1000.0)]}, through=FRI, calendar=CALENDAR, params=PARAMS
    )
    (position,) = book.values()
    outcome = results[0].outcome
    assert outcome is not None
    # 40 shares x 1000 = 40,000; 12 bps = 48.00.
    assert position.buys[0].fees == D("48.00")
    assert outcome.cash_delta == D("-40048.00")

    exit_order = PendingOrder(
        OrderAction.SELL_ALL,
        "X",
        (2026, 39),
        MON + timedelta(days=7),
        "stop",
        position_id=position.position_id,
        quantity=40,
    )
    sold, after = fill_orders(
        [exit_order],
        {position.position_id: position},
        {"X": [_bar(MON + timedelta(days=7), 900.0)]},
        through=FRI + timedelta(days=7),
        calendar=CALENDAR,
        params=PARAMS,
    )
    closed = after[position.position_id]
    assert closed.state is PositionState.CLOSED
    # 36,000 proceeds; 11 bps = 39.60, plus Rs 15.
    assert closed.sales[0].fees == D("54.60")
    assert sold[0].outcome is not None and sold[0].outcome.cash_delta == D("35945.40")
    assert sold[0].outcome.closed is not None
    assert sold[0].outcome.closed.net_pnl == D("-4102.60")


def test_sells_fill_before_buys_at_the_same_open() -> None:
    _, book = fill_orders(
        [_entry("A")], {}, {"A": [_bar(MON, 100.0)]}, through=FRI, calendar=CALENDAR, params=PARAMS
    )
    (held,) = book.values()
    next_monday = MON + timedelta(days=7)
    exit_a = PendingOrder(
        OrderAction.SELL_ALL,
        "A",
        (2026, 39),
        next_monday,
        "stop",
        position_id=held.position_id,
        quantity=held.shares_held,
    )
    results, _ = fill_orders(
        [_entry("B", next_monday), exit_a],
        {held.position_id: held},
        {"A": [_bar(next_monday, 90.0)], "B": [_bar(next_monday, 50.0)]},
        through=FRI + timedelta(days=7),
        calendar=CALENDAR,
        params=PARAMS,
    )
    assert [r.order.action for r in results] == [OrderAction.SELL_ALL, OrderAction.BUY_T1]


def test_zero_shares_at_the_open_skips_the_order() -> None:
    results, book = fill_orders(
        [_entry()], {}, {"X": [_bar(MON, 50000.0)]}, through=FRI, calendar=CALENDAR, params=PARAMS
    )
    assert results[0].skipped and book == {}
