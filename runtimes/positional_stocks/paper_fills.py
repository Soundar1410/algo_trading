"""The paper fill model (spec section 8) — step 1 of a weekly run (4.13).

Every order fills at the **official open of its execution session**, read from
the symbol's cached daily candle; costs are applied by the rules' own
:func:`~strategies.positional_stocks.wsr1_weekly_stochrsi.rules.apply_fill`
(spec 8's bps on value, plus the fixed charge per sell). Fills are recorded at
the next weekly run, which is the first moment the candle exists (spec 8).

* **Not traded that session.** The fill is the open of the symbol's first
  session on or after ``execute_on_or_after``, and is flagged when that is not
  the execution session itself.
* **Not traded all week.** No fill: the order stays PENDING and keeps holding
  its capacity (spec 4.12 v1.2g), and the next run tries again.
* **``at_week_open`` (spec 4.9 v1.2g)** is true only when the fill session is
  the calendar's first trading session of its ISO week — Monday, or Tuesday
  after a Monday holiday. A weekend special session is never the first.
* **Sells before buys** at the same open, in a deterministic order.

Pure apart from reading its arguments: no I/O, no clock.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import week_of
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    DailyBar,
    PendingOrder,
    Position,
    RulesParameters,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import FillOutcome, apply_fill
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar


@dataclass(frozen=True, slots=True)
class FillPlan:
    """Where and at what price one order fills."""

    session: date
    open_price: float
    at_week_open: bool
    #: Spec 8: the symbol did not trade on the execution session.
    not_traded_on_execution_session: bool


def plan_fill(
    order: PendingOrder,
    daily: Sequence[DailyBar],
    *,
    through: date,
    calendar: TradingCalendar,
) -> FillPlan | None:
    """The symbol's first session on or after the order's execution date and on
    or before ``through``; ``None`` when it has none yet."""
    sessions = [bar for bar in daily if order.execute_on_or_after <= bar.session <= through]
    if not sessions:
        return None
    bar = min(sessions, key=lambda b: b.session)
    return FillPlan(
        session=bar.session,
        open_price=bar.open,
        at_week_open=bar.session == calendar.first_session(week_of(bar.session)),
        not_traded_on_execution_session=bar.session != order.execute_on_or_after,
    )


@dataclass(frozen=True, slots=True)
class OrderFill:
    """One pending order's outcome this run."""

    order: PendingOrder
    plan: FillPlan | None
    #: ``None`` when the order did not fill (no session yet).
    outcome: FillOutcome | None

    @property
    def filled(self) -> bool:
        return self.outcome is not None and self.outcome.skipped is None

    @property
    def skipped(self) -> bool:
        return self.outcome is not None and self.outcome.skipped is not None


def _sort_key(order: PendingOrder) -> tuple[int, str, str]:
    return (0 if not order.action.is_buy else 1, order.symbol, order.action.value)


def fill_orders(
    pending: Sequence[PendingOrder],
    positions: Mapping[str, Position],
    daily: Mapping[str, Sequence[DailyBar]],
    *,
    through: date,
    calendar: TradingCalendar,
    params: RulesParameters,
) -> tuple[list[OrderFill], dict[str, Position]]:
    """Fill every pending order that has a session on or before ``through``.

    ``positions`` maps position id to position. Returns each order's outcome,
    and the positions after the fills — a BUY_T1 adds one, a SELL_ALL leaves
    its position CLOSED (still in the map, for the caller to record).
    """
    book = dict(positions)
    results: list[OrderFill] = []
    for order in sorted(pending, key=_sort_key):
        plan = plan_fill(order, daily.get(order.symbol, ()), through=through, calendar=calendar)
        if plan is None:
            results.append(OrderFill(order, None, None))
            continue
        position = book.get(order.position_id) if order.position_id else None
        outcome = apply_fill(
            order,
            position,
            session=plan.session,
            open_price=plan.open_price,
            params=params,
            at_week_open=plan.at_week_open,
        )
        if outcome.skipped is None and outcome.position is not None:
            book[outcome.position.position_id] = outcome.position
        results.append(OrderFill(order, plan, outcome))
    return results, book


def execution_date_after(week_ending: date, calendar: TradingCalendar) -> date:
    """The first session after a decision week: where its orders execute."""
    return calendar.next_session_after(week_ending)
