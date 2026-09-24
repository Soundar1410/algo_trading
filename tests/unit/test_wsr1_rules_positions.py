"""Phase 3: held positions — sizing (4.8), adds (4.9), exits (4.10).

Covers spec section 14's golden table, the V1 plan's section 6 stages and
scenarios (``WSR1_V1_GOLDEN_CASES.md``), every row of its section 8 exit
table, and the v1.2f answers H (EVENT_RISK disables T3) and I (expired rows).

The V1 plan's numbers are before costs, so these run with costs zeroed
(``PARAMS0``); ``test_costs_*`` checks the cost model separately.

:func:`_run` is a small simulator in spec 4.13's order: each week, fill last
week's order at this week's open, then review the position at this week's
close.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from _wsr1_rules_fixtures import (
    PARAMS,
    PARAMS0,
    D,
    Tape,
    book,
    ctx,
    fill,
    index_series,
    monday_after,
    open_position,
    quality,
    symbol_week,
    universe_row,
)

from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    OnExit,
    OrderAction,
    PendingOrder,
    Position,
    PositionReview,
    PositionState,
    QualityStatus,
    RulesParameters,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import (
    FillOutcome,
    apply_fill,
    decide_week,
    levels,
    review_position,
    sizing,
    spacing,
)

CASH = D("1000000")
START = 205  # the T1 fill bar in the simulations


# ---------------------------------------------------------------- helpers
@dataclass(frozen=True)
class Wk:
    """One simulated week: the open (where last week's order fills) and its bar."""

    open: float
    high: float
    low: float
    close: float
    k: float = 50.0
    d: float = 50.0
    ema200: float = 500.0
    ema10: float = 600.0


def _series(weeks: list[Wk], upto: int) -> Tape:
    """History of default bars, then ``weeks[: upto + 1]`` from bar START on."""
    shown = weeks[: upto + 1]
    return Tape(
        n=START + len(shown),
        close=[w.close for w in shown],
        high=[w.high for w in shown],
        low=[w.low for w in shown],
        k=[w.k for w in shown],
        d=[w.d for w in shown],
        ema200=[w.ema200 for w in shown],
        ema10=[w.ema10 for w in shown],
    )


@dataclass
class Run:
    position: Position
    reviews: list[PositionReview]
    fills: list[FillOutcome]

    def orders(self) -> list[tuple[int, OrderAction]]:
        return [(i, r.order.action) for i, r in enumerate(self.reviews) if r.order is not None]


def _run(
    weeks: list[Wk], *, atr_pct: float = 0.06, params: RulesParameters = PARAMS0, **inputs: Any
) -> Run:
    """T1 fills at ``weeks[0].open`` on the Monday of bar START; then each week."""
    position = open_position("X", atr_pct=atr_pct, p1=weeks[0].open, fill_week=START, params=params)
    reviews: list[PositionReview] = []
    fills: list[FillOutcome] = []
    pending: PendingOrder | None = None
    for i, wk in enumerate(weeks):
        bar = START + i
        if pending is not None:
            outcome = apply_fill(
                pending, position, session=monday_after(bar - 1), open_price=wk.open, params=params
            )
            fills.append(outcome)
            assert outcome.position is not None
            position = outcome.position
            pending = None
            if position.state is PositionState.CLOSED:
                break
        series = _series(weeks, i).series()
        review, position = review_position(
            position,
            symbol_week("X", series, **inputs),
            ctx(bar),
            params,
            CASH,
        )
        reviews.append(review)
        pending = review.order
    return Run(position, reviews, fills)


def _review(
    position: Position,
    tape: Tape,
    *,
    params: RulesParameters = PARAMS0,
    execution_date: date | None = None,
    **kw: Any,
) -> tuple[PositionReview, Position]:
    return review_position(
        position,
        symbol_week(position.symbol, tape.series(), **kw),
        ctx(tape.n - 1, execution_date),
        params,
        CASH,
    )


def _held(p1: float = 1000.0, *, fill_week: int = 200, atr_pct: float = 0.06) -> Position:
    return open_position("X", atr_pct=atr_pct, p1=p1, fill_week=fill_week)


def _sell_all(position: Position, price: float, week_index: int) -> Position:
    return fill(
        position,
        OrderAction.SELL_ALL,
        price=price,
        week_index=week_index,
        quantity=position.shares_held,
    )


# ================================================ spec 14 golden cases
def test_golden_atr_6pct_fill_1000() -> None:
    """ATR 6%, fill 1000: s 10%, A 1,00,000, T1 40 shares, L1 900, L2 800, Stop 700."""
    size = sizing(0.06, False, PARAMS0)
    assert size.spacing == D("0.10")
    assert size.allocation == D("100000.00")
    position = _held()
    assert position.shares_held == 40
    assert (position.l1, position.l2, position.stop) == (D("900.00"), D("800.00"), D("700.00"))


def test_golden_atr_7_2pct_close_1000() -> None:
    """ATR 7.2%: s 10.8%, A 92,593, T1 37,037 -> 37 shares; L1 892, L2 784, Stop 676."""
    size = sizing(0.072, False, PARAMS0)
    assert size.spacing == D("0.1080")
    assert size.allocation.quantize(D("1")) == D("92593")
    assert size.tranche_amounts[0].quantize(D("1")) == D("37037")
    position = _held(atr_pct=0.072)
    assert position.shares_held == 37
    assert (position.l1, position.l2, position.stop) == (D("892.00"), D("784.00"), D("676.00"))


def _fully_averaged() -> Position:
    position = fill(_held(), OrderAction.BUY_T2, price=930.0, week_index=202)
    return fill(position, OrderAction.BUY_T3, price=820.0, week_index=204)


def test_golden_t2_at_930_and_t3_at_820() -> None:
    """32 and 36 shares; 108 total; cost 99,280; average 919.26; loss at 700 -23,680."""
    position = _fully_averaged()
    assert [b.shares for b in position.buys] == [40, 32, 36]
    assert position.shares_held == 108
    assert position.buy_value == D("99280.00")
    assert position.average_cost.quantize(D("0.01")) == D("919.26")
    assert _sell_all(position, 700.0, 206).net_pnl == D("-23680.00")


def test_golden_stop_with_t1_only() -> None:
    assert _sell_all(_held(), 700.0, 206).net_pnl == D("-12000.00")


def test_golden_worked_trade() -> None:
    """T1 40 @ 1000, T2 32 @ 930, half 36 @ 1120, rest 36 @ 1180 -> +13,040."""
    position = fill(_held(), OrderAction.BUY_T2, price=930.0, week_index=202)
    position = fill(position, OrderAction.SELL_HALF, price=1120.0, week_index=210, quantity=36)
    assert position.state is PositionState.HALF_SOLD
    assert _sell_all(position, 1180.0, 215).net_pnl == D("13040.00")


def test_golden_event_risk_atr_9pct() -> None:
    """Event-risk, ATR 9%, B 1L: s 13.5%, A 37,037, T3 disabled."""
    size = sizing(0.09, True, PARAMS0)
    assert size.spacing == D("0.135")
    assert size.allocation == D("37037.04")
    assert size.max_tranches == 2


def test_golden_odd_shares_at_the_partial_sale() -> None:
    """73 held -> sell 36 (section 8: keep 37)."""
    # T1 of 40,000 at 547 buys floor(73.1) = 73 shares.
    seventy_three = open_position("Y", atr_pct=0.06, p1=547.0, fill_week=200)
    assert seventy_three.shares_held == 73
    review, _ = _review(seventy_three, Tape(n=205, close=560.0, k=95.0, d=92.0))
    assert review.order is not None
    assert review.order.action is OrderAction.SELL_HALF
    assert review.order.quantity == 36


def test_spacing_floor_and_the_levels_are_fixed_at_the_fill() -> None:
    assert spacing(0.05, PARAMS0) == D("0.10")  # 1.5 x 5% = 7.5% < floor
    assert levels(D("1000"), D("0.1125"), PARAMS0) == (D("887.50"), D("775.00"), D("662.50"))


def test_zero_shares_skips_the_order() -> None:
    """Spec 4.8: floor(amount / price) = 0 -> skipped and reported."""
    size = sizing(0.06, False, PARAMS0)
    order = PendingOrder(
        OrderAction.BUY_T1,
        "X",
        (2026, 1),
        date(2026, 1, 5),
        "e",
        amount=size.tranche_amounts[0],
        sizing=size,
        sector="S",
        group="X",
    )
    outcome = apply_fill(order, None, session=date(2026, 1, 5), open_price=50000.0, params=PARAMS0)
    assert outcome.position is None and outcome.skipped is not None
    assert outcome.cash_delta == 0


def test_costs_are_bps_on_value_plus_a_fixed_sell_charge() -> None:
    position = open_position("X", atr_pct=0.06, p1=1000.0, fill_week=200, params=PARAMS)
    assert position.buys[0].fees == D("48.00")  # 12 bps of 40,000
    closed = fill(
        position, OrderAction.SELL_ALL, price=1000.0, week_index=205, quantity=40, params=PARAMS
    )
    assert closed.sales[0].fees == D("59.00")  # 11 bps of 40,000 + 15
    assert closed.net_pnl == D("-107.00")


# ============================================ V1 plan section 6: stages
_STAGES = [
    Wk(1000, 1010, 960, 970),  # T1 fill week
    Wk(965, 925, 890, 900),  # low 890 <= L1 900: touch; no reversal
    Wk(905, 940, 905, 935),  # close 935 > prior high 925: reversal -> T2
    Wk(930, 850, 790, 800),  # T2 fills at 930; low 790 <= L2 after the T2 buy
    Wk(805, 860, 800, 855),  # close 855 > 850: reversal -> T3
    Wk(820, 830, 760, 780),  # T3 fills at 820
    Wk(775, 790, 685, 690),  # close 690 < 700: stop
    Wk(690, 700, 680, 690),  # exits at the open
]


def test_section6_stages_t1_t2_t3_and_the_stop() -> None:
    run = _run(_STAGES)
    assert run.orders() == [
        (2, OrderAction.BUY_T2),
        (4, OrderAction.BUY_T3),
        (6, OrderAction.SELL_ALL),
    ]
    buys = [(o.position.buys[-1].shares, o.position.buys[-1].value) for o in run.fills[:2]]  # type: ignore[union-attr]
    assert buys == [(32, D("29760.00")), (36, D("29520.00"))]
    assert run.position.state is PositionState.CLOSED
    # After all three: 108 shares, cost 99,280 — sold at the 690 open.
    assert run.position.shares_bought == 108
    assert run.position.net_pnl == D("-24760.00")  # the plan's "at 690"
    assert (D("919.26") / D("820") - 1).quantize(D("0.001")) == D("0.121")  # breakeven +12.1%


# ========================================= V1 plan section 6: scenarios
def test_scenario_falls_without_a_reversal_exits_with_t1_only() -> None:
    weeks = [
        Wk(1000, 1010, 960, 970),
        Wk(965, 975, 880, 890),
        Wk(885, 890, 820, 830),
        Wk(825, 835, 760, 770),
        Wk(765, 775, 680, 690),  # stop
        Wk(700, 710, 690, 700),  # exit at the open, 700
    ]
    run = _run(weeks)
    assert run.orders() == [(4, OrderAction.SELL_ALL)]
    assert run.position.net_pnl == D("-12000.00")  # 40 x 300


def test_scenario_hits_the_level_and_falls_further_first_reversal_buys_t2_only() -> None:
    weeks = [
        Wk(1000, 1010, 960, 970),
        Wk(965, 960, 880, 890),  # touch L1
        Wk(885, 790, 760, 770),  # below L2 too, but T2 not bought: no L2 touch yet
        Wk(775, 800, 765, 792),  # reversal below 800 -> T2 only
        Wk(795, 796, 785, 790),  # T2 fills; low <= 800 touches L2 (after the buy)
    ]
    run = _run(weeks)
    assert run.orders() == [(3, OrderAction.BUY_T2)]
    assert run.position.tranches_used == 2
    assert run.position.touch_week is not None  # L2 touched in the T2 fill week


def test_scenario_never_reaching_the_second_level_keeps_t3_reserved() -> None:
    weeks = [
        Wk(1000, 1010, 960, 970),
        Wk(965, 925, 890, 900),
        Wk(905, 940, 905, 935),  # T2
        Wk(930, 960, 925, 950),
        Wk(950, 995, 945, 990),
        Wk(990, 1030, 985, 1020),  # close >= P1: nothing to clear, no add above P1
    ]
    run = _run(weeks)
    assert run.orders() == [(2, OrderAction.BUY_T2)]
    assert run.position.next_level == D("800.00")
    assert run.position.committed == D("100000.00")  # T3's 30,000 stays reserved


def test_scenario_intraweek_gap_below_the_stop_counts_only_at_the_close() -> None:
    weeks = [
        Wk(1000, 1010, 960, 970),
        Wk(965, 970, 650, 700),  # low 650, close exactly 700: no action
        Wk(700, 705, 685, 690),  # close 690 < 700: sell all
        Wk(640, 650, 630, 645),  # Monday open 640
    ]
    run = _run(weeks)
    assert run.orders() == [(2, OrderAction.SELL_ALL)]
    assert run.position.net_pnl == D("-14400.00")
    assert _sell_all(_fully_averaged(), 640.0, 206).net_pnl == D("-30160.00")


def test_scenario_rises_after_t1_sells_half_at_90_and_releases_the_reserve() -> None:
    weeks = [
        Wk(1000, 1010, 990, 1005),
        Wk(1005, 1060, 1000, 1050),
        Wk(1050, 1110, 1045, 1100, k=95, d=91),  # partial
        Wk(1100, 1120, 1090, 1110),
    ]
    run = _run(weeks)
    assert run.orders() == [(2, OrderAction.SELL_HALF)]
    assert run.position.state is PositionState.HALF_SOLD
    assert run.position.shares_held == 20
    assert run.position.committed == D("20000.00")
    assert run.position.next_level is None


def test_scenario_t2_then_recovery_sells_36_and_cancels_t3() -> None:
    weeks = [
        Wk(1000, 1010, 960, 970),
        Wk(965, 925, 890, 900),
        Wk(905, 940, 905, 935),  # T2
        Wk(930, 990, 925, 985),
        Wk(985, 1090, 980, 1080, k=93, d=91),  # partial
        Wk(1080, 1090, 1070, 1085),
    ]
    run = _run(weeks)
    fill_t2 = run.fills[0].position
    assert fill_t2 is not None and fill_t2.shares_held == 72
    assert fill_t2.average_cost.quantize(D("0.01")) == D("968.89")
    assert fill_t2.stop == D("700.00")
    assert run.orders() == [(2, OrderAction.BUY_T2), (4, OrderAction.SELL_HALF)]
    assert run.reviews[4].order is not None and run.reviews[4].order.quantity == 36
    assert run.position.next_level is None  # T3 cancelled


def test_scenario_all_three_tranches_then_only_exits() -> None:
    position = _fully_averaged()
    assert position.next_level is None
    review, _ = _review(position, Tape(n=210, close=900.0, low=750.0, k=95.0, d=95.0))
    assert review.order is not None and review.order.quantity == 54


# ========================================= finding 2: close >= P1 clears
def test_a_close_at_or_above_p1_clears_the_touch() -> None:
    weeks = [
        Wk(1000, 1010, 960, 970),
        Wk(965, 925, 890, 900),  # touch L1
        Wk(905, 1010, 905, 1000),  # close = P1: touch cleared (and no add)
        Wk(995, 998, 950, 960),
        Wk(960, 1005, 955, 999),  # reversal, but no touch since the clear
    ]
    run = _run(weeks)
    assert run.orders() == []
    assert "touch cleared" in run.reviews[2].reason
    assert "not touched" in run.reviews[4].reason


# ================================================ V1 plan section 8
def _tape(n: int = 210, **kw: Any) -> Tape:
    return Tape(n=n, **kw)


#: One week that both touches L1 (low 880) and reverses (close 950 > prior high 940) —
#: fix 6 allows the same week. Multi-week touch memory is the simulator's job.
_TOUCH_AND_REVERSE: dict[str, Any] = {
    "close": {-2: 930.0, -1: 950.0},
    "high": {-2: 940.0},
    "low": {-1: 880.0},
}


def test_exit_90_is_judged_at_the_weekly_close() -> None:
    review, _ = _review(_held(), _tape(k=91.0, d=90.5))
    assert review.order is not None and review.order.action is OrderAction.SELL_HALF


def test_exit_above_90_midweek_but_not_at_the_close_is_no_sale() -> None:
    review, _ = _review(_held(), _tape(k=95.0, d=90.0))
    assert review.order is None


def test_exit_only_k_above_90_is_no_sale() -> None:
    review, _ = _review(_held(), _tape(k=96.0, d=85.0))
    assert review.order is None


def test_exit_sells_at_the_first_session_after_the_close() -> None:
    """Tuesday if Monday is a holiday: the order carries the runtime's execution date."""
    tuesday = monday_after(209) + timedelta(days=1)
    position = _held()
    review, _ = review_position(
        position,
        symbol_week("X", _tape(k=95.0, d=95.0).series()),
        ctx(209, execution_date=tuesday),
        PARAMS0,
        CASH,
    )
    assert review.order is not None and review.order.execute_on_or_after == tuesday


def _half_sold(partial_week: int = 205) -> Position:
    return fill(_held(), OrderAction.SELL_HALF, price=1100.0, week_index=partial_week, quantity=20)


def test_exit_no_adds_after_the_partial_sale() -> None:
    # A touch of L1 and a reversal: an OPEN position would add; HALF_SOLD never does.
    review, _ = _review(_half_sold(), _tape(**_TOUCH_AND_REVERSE))
    assert review.order is None


def test_exit_the_price_stop_does_not_apply_after_the_partial_sale() -> None:
    review, _ = _review(_half_sold(), _tape(close=650.0, ema10=600.0))
    assert review.order is None and "trailing" in review.reason


def test_exit_rising_strongly_hold_no_second_sale_then_52_week_trail_time() -> None:
    half = _half_sold(partial_week=150)
    hold, _ = _review(half, _tape(n=202, close=1500.0, ema10=1400.0, k=97.0, d=96.0))
    assert hold.order is None  # no second sale at 90
    sell, _ = _review(half, _tape(n=203, close=1500.0, ema10=1400.0))
    assert sell.order is not None and "52 weeks since the partial" in sell.reason


def test_exit_a_close_equal_to_the_10w_ema_holds() -> None:
    review, _ = _review(_half_sold(), _tape(close=1050.0, ema10=1050.0))
    assert review.order is None
    below, _ = _review(_half_sold(), _tape(close=1049.99, ema10=1050.0))
    assert below.order is not None and below.order.action is OrderAction.SELL_ALL


def test_exit_sell_half_even_below_average_cost() -> None:
    review, _ = _review(_held(), _tape(close=950.0, k=92.0, d=91.0))
    assert review.order is not None and review.order.action is OrderAction.SELL_HALF


def test_exit_the_52_week_time_exit_counts_iso_weeks_from_the_t1_fill() -> None:
    """v1.2f: T1 filled in week F -> decided at the close of F + 52."""
    position = _held(fill_week=150)
    before, _ = _review(position, _tape(n=202))  # F + 51
    assert before.order is None
    at, _ = _review(position, _tape(n=203))  # F + 52
    assert at.order is not None and "time exit" in at.reason
    # After a partial sale the time exit no longer applies (the trail does).
    half = _half_sold(partial_week=190)
    assert _review(half, _tape(n=203))[0].order is None


@pytest.mark.parametrize(
    ("position", "tape", "kw", "reason"),
    [
        (_held(), _tape(close=690.0), {}, "stop"),
        (_held(), _tape(), {"quality_row": quality("X", QualityStatus.FAIL)}, "thesis"),
        (_held(fill_week=150), _tape(n=203), {}, "time exit"),
        (_half_sold(), _tape(close=900.0, ema10=950.0), {}, "trail exit"),
        (_half_sold(partial_week=150), _tape(n=203), {}, "trail time"),
    ],
    ids=["stop", "thesis", "time", "trail", "trail-time"],
)
def test_exit_every_way_out_closes_the_position(
    position: Position, tape: Tape, kw: dict[str, Any], reason: str
) -> None:
    review, _ = _review(position, tape, **kw)
    assert review.order is not None and reason in review.reason
    assert review.order.quantity == position.shares_held
    closed = _sell_all(position, 1000.0, tape.n)
    assert closed.state is PositionState.CLOSED and closed.shares_held == 0


# ======================================= v1.2f H and I; universe exits
def test_h_event_risk_on_a_pass_entry_disables_t3_but_keeps_sizing() -> None:
    position = fill(_held(), OrderAction.BUY_T2, price=930.0, week_index=202)
    touch_and_reverse = _tape(close={-2: 820.0, -1: 850.0}, high={-2: 830.0}, low={-1: 780.0})
    before, _ = _review(position, touch_and_reverse)
    assert before.order is not None and before.order.action is OrderAction.BUY_T3
    review, updated = _review(
        position, touch_and_reverse, quality_row=quality("X", QualityStatus.EVENT_RISK)
    )
    assert review.order is None
    assert updated.t3_disabled and updated.sizing.allocation == D("100000.00")
    assert updated.committed == D("70000.00")


def test_i_an_expired_fail_still_exits() -> None:
    expired_fail = quality("X", QualityStatus.FAIL, valid_until=date(2020, 1, 1))
    review, _ = _review(_held(), _tape(), quality_row=expired_fail)
    assert review.order is not None and review.order.action is OrderAction.SELL_ALL


@pytest.mark.parametrize("status", [QualityStatus.PASS, QualityStatus.EVENT_RISK])
def test_i_an_expired_pass_or_event_risk_blocks_adds_but_does_not_exit(
    status: QualityStatus,
) -> None:
    touch_and_reverse = _tape(**_TOUCH_AND_REVERSE)
    expired = quality("X", status, valid_until=date(2020, 1, 1))
    review, _ = _review(_held(), touch_and_reverse, quality_row=expired)
    assert review.order is None
    assert "quality" in review.reason


def test_left_the_universe_with_on_exit_exit_is_a_thesis_exit() -> None:
    last_seen = universe_row("X", on_exit=OnExit.EXIT)
    review, _ = _review(_held(), _tape(), row=False, last_seen_row=last_seen)
    assert review.order is not None and "on_exit: exit" in review.reason


def test_left_the_universe_with_on_exit_hold_exits_normally_but_never_adds() -> None:
    touch_and_reverse = _tape(**_TOUCH_AND_REVERSE)
    last_seen = universe_row("X", on_exit=OnExit.HOLD)
    review, _ = _review(_held(), touch_and_reverse, row=False, last_seen_row=last_seen)
    assert review.order is None and "left the universe" in review.reason
    stop, _ = _review(_held(), _tape(close=650.0), row=False, last_seen_row=last_seen)
    assert stop.order is not None and "stop" in stop.reason


# ============================================ add gates and None


def test_an_add_needs_touch_reversal_and_every_gate() -> None:
    review, _ = _review(_held(), _tape(**_TOUCH_AND_REVERSE))
    assert review.order is not None and review.order.action is OrderAction.BUY_T2
    assert review.order.amount == D("30000.00")


def test_no_add_below_the_200w_ema() -> None:
    """Section 7 row 9's second half: adds NO while the close is below the EMA200."""
    review, _ = _review(_held(), _tape(**_TOUCH_AND_REVERSE, ema200=960.0))
    assert review.order is None and "200W EMA" in review.reason


def test_none_never_satisfies_the_add_ema200_gate() -> None:
    review, _ = _review(_held(), _tape(**_TOUCH_AND_REVERSE, ema200={-1: None}))
    assert review.order is None and "200W EMA" in review.reason


def test_none_never_satisfies_the_trail() -> None:
    review, _ = _review(_half_sold(), _tape(close=900.0, ema10={-1: None}))
    assert review.order is None and "EMA10 undefined" in review.flags


def test_none_never_satisfies_the_partial() -> None:
    review, _ = _review(_held(), _tape(k={-1: None}, d=95.0))
    assert review.order is None


def test_results_in_the_execution_week_blocks_an_add() -> None:
    review, _ = _review(
        _held(), _tape(**_TOUCH_AND_REVERSE), results_dates=(ctx(209).execution_date,)
    )
    assert review.order is None and "results" in review.reason


def test_the_reversal_must_come_after_the_previous_buys_fill_week() -> None:
    # T1 filled in the decision week itself: its own low and reversal cannot add.
    review, _ = _review(_held(fill_week=209), _tape(**_TOUCH_AND_REVERSE))
    assert review.order is None and "after the previous buy" in review.reason


def test_an_add_refused_for_cash_is_reported() -> None:
    position = _held()
    review, _ = review_position(
        position,
        symbol_week("X", _tape(**_TOUCH_AND_REVERSE).series()),
        ctx(209),
        PARAMS0,
        D("100"),
    )
    assert review.order is None and "cash" in review.reason


def test_an_unfilled_order_blocks_a_second_decision() -> None:
    position = _held()
    pending = PendingOrder(
        OrderAction.SELL_ALL,
        "X",
        (2025, 1),
        date(2025, 1, 6),
        "stop",
        position_id=position.position_id,
        quantity=40,
    )
    decision = decide_week(
        ctx(209),
        book(position, pending=(pending,)),
        {"X": symbol_week("X", _tape(close=600.0).series())},
        index_series(),
        PARAMS0,
    )
    assert decision.orders == ()
    assert "pending" in decision.reviews[0].flags


def test_no_bar_this_week_is_held_and_flagged() -> None:
    review, _ = review_position(
        _held(), symbol_week("X", _tape(n=209).series()), ctx(209), PARAMS0, CASH
    )
    assert review.order is None and "stale" in review.flags


def test_a_sale_needs_a_held_position_and_the_right_quantity() -> None:
    position = _held()
    with pytest.raises(ValueError, match="every share"):
        fill(position, OrderAction.SELL_ALL, price=1000.0, week_index=205, quantity=10)
    with pytest.raises(ValueError, match="more shares"):
        fill(position, OrderAction.SELL_HALF, price=1000.0, week_index=205, quantity=41)
    half = _half_sold()
    with pytest.raises(ValueError, match="once per trade"):
        fill(half, OrderAction.SELL_HALF, price=1000.0, week_index=206, quantity=5)
    with pytest.raises(ValueError, match="OPEN"):
        fill(half, OrderAction.BUY_T2, price=900.0, week_index=206)


def test_decimal_money_keeps_rupee_levels_exact() -> None:
    # 1000 x (1 - 0.108) is 891.9999... in float; the level is exactly 892.00.
    assert _held(atr_pct=0.072).l1 == Decimal("892.00")
