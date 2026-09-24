"""The rules core of ``wsr1_weekly_stochrsi`` (spec sections 4.3, 4.5-4.13).

Pure. It takes indicator series, the operator's input rows, the paper book and
a :class:`~.models.RulesParameters`, and returns decisions with reasons. No
I/O, no clock, no network, no database, no broker, nothing from
``common.engine`` or ``runtimes`` — spec section 13, enforced by
``tests/unit/test_wsr1_rules_guard.py``. A future live adapter reuses it
unchanged.

What is here
------------
* :func:`regime` — spec 4.3, with v1.2f's ``UNKNOWN``.
* :func:`trigger` — spec 4.5: armed, the latest oversold close ``j0``, the first
  cross since it, K < 50; undefined K/D in the window → no trigger (v1.2f).
* :func:`sizing` and :func:`apply_fill` — spec 4.8 and the fill-time state
  transitions (levels fixed from the T1 fill, floor shares, skip at 0).
* :func:`update_brakes` — spec 4.12's drawdown brakes, v1.2f semantics.
* :func:`review_position` — spec 4.10's exit order and 4.9's adds.
* :func:`decide_week` — spec 4.13 steps 2-4 in order.

**None never satisfies a condition.** Every indicator read goes through
:func:`_lt` / :func:`_gt` and friends, which answer ``False`` for ``None``. The
direction of each rule decides whether ``False`` is safe; the two places where
it would not be — the regime (``False`` for "Red" means Normal) and the trigger
window — are handled explicitly as ``UNKNOWN`` / undefined, per v1.2f.

Money is ``Decimal`` (see :mod:`.models`); indicator values are floats and are
only ever compared.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .indicators import EMA_LENGTHS, IndicatorSeries, relative_strength
from .iso_weeks import WeekKey, shift, week_of, weeks_between
from .models import (
    PAISA,
    Book,
    BrakeState,
    BuyFill,
    ClosedTrade,
    DailyBar,
    FunnelEntry,
    FunnelStage,
    OnExit,
    OrderAction,
    PendingOrder,
    Position,
    PositionReview,
    PositionState,
    QualityRow,
    QualityStatus,
    Regime,
    RulesParameters,
    SaleFill,
    Sizing,
    UniverseRow,
    WeekDecision,
    money,
)

#: Spec 4.8: A = B x 10% / s — the allocation at which s = 10% gives A = B.
_REFERENCE_SPACING = Decimal("0.10")
_CRORE = 10_000_000.0
_BPS = Decimal("10000")
#: Spec 4.1: liquidity is the average over the last 30 sessions.
LIQUIDITY_SESSIONS = 30


# ----------------------------------------------------------------- inputs
@dataclass(frozen=True, slots=True)
class WeekContext:
    """The decision week and when its orders execute.

    ``execution_date`` is the first session that starts after the decision run
    (spec 3). It is supplied by the runtime from the calendar; the rules never
    work it out, because that would need the clock.
    """

    week: WeekKey
    week_ending: date
    execution_date: date

    def __post_init__(self) -> None:
        if self.execution_date <= self.week_ending:
            raise ValueError("execution_date must fall after the decision week")

    @property
    def execution_week(self) -> tuple[date, date]:
        """Monday-Friday of the execution week (spec 4.6 item 6)."""
        monday = self.execution_date - timedelta(days=self.execution_date.weekday())
        return monday, monday + timedelta(days=4)


@dataclass(frozen=True, slots=True)
class SymbolWeek:
    """One symbol's inputs for one decision week.

    ``series`` must end at the decision week. ``universe_row`` is the current
    ``universe.csv`` row (``None`` = not in the file); ``last_seen_row`` is the
    row the runtime last saw, whose ``on_exit`` governs a held symbol that has
    left the file (spec 6.3 v1.2f). ``results_dates`` is ``None`` when the
    results calendar has no row for the symbol ("results date unknown").
    ``gap_blocked`` is spec 6.1's unacknowledged-gap flag, computed by the
    runtime (Phase 4).
    """

    symbol: str
    series: IndicatorSeries
    universe_row: UniverseRow | None
    quality: QualityRow | None = None
    results_dates: tuple[date, ...] | None = None
    traded_value_30d: float | None = None
    gap_blocked: bool = False
    last_seen_row: UniverseRow | None = None


# ---------------------------------------------------- None-safe comparisons
def _lt(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and a < b


def _gt(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and a > b


def _ge(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and a >= b


def _le(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and a <= b


def _last_index(series: IndicatorSeries, week: WeekKey) -> int | None:
    """The index of ``week``'s bar, only if it is the series' last bar."""
    if not series.bars or series.bars[-1].iso_key != week:
        return None
    return len(series.bars) - 1


def _ema(series: IndicatorSeries, length: int, i: int) -> float | None:
    return series.ema[length][i]


# ------------------------------------------------------------ liquidity
def traded_value(daily: Sequence[DailyBar], sessions: int = LIQUIDITY_SESSIONS) -> float | None:
    """Average daily traded value (close x volume) over the last ``sessions``.

    Spec 4.1. ``None`` when there are fewer sessions than that, which the
    liquidity filter treats as a failure.
    """
    if len(daily) < sessions:
        return None
    recent = sorted(daily, key=lambda bar: bar.session)[-sessions:]
    return sum(bar.traded_value for bar in recent) / sessions


# --------------------------------------------------------------- regime
def regime(index: IndicatorSeries, week: WeekKey, params: RulesParameters) -> Regime:
    """Spec 4.3: Red = close < EMA40 AND EMA40 < its value 4 bars earlier.

    ``UNKNOWN`` (v1.2f) when NIFTY has no bar for the week, or either EMA40
    value is undefined — never silently Normal.
    """
    i = _last_index(index, week)
    back = params.regime_slope_lookback_weeks
    if i is None or i < back:
        return Regime.UNKNOWN
    now, before = _ema(index, params.regime_ema, i), _ema(index, params.regime_ema, i - back)
    if now is None or before is None:
        return Regime.UNKNOWN
    if index.bars[i].close < now and now < before:
        return Regime.RED
    return Regime.NORMAL


# -------------------------------------------------------------- trigger
@dataclass(frozen=True, slots=True)
class TriggerVerdict:
    """Spec 4.5 at one bar. ``undefined`` marks v1.2f's fail-closed case."""

    armed: bool
    triggered: bool
    reason: str
    arm_index: int | None = None
    undefined: bool = False


def trigger(series: IndicatorSeries, i: int, params: RulesParameters) -> TriggerVerdict:
    """Spec 4.5 at bar ``i`` — weekly closes only.

    ARMED: K < 20 AND D < 20 on this close or any of the previous 7. ``j0`` is
    the latest such close. TRIGGER: armed, K > D now and K <= D last week, no
    such cross in any bar from ``j0`` to last week (a cross in the arm week
    itself counts), and K < 50. An undefined K or D in a bar the check reads —
    the 8-close window, or the bar before ``j0`` — means no trigger (v1.2f);
    a bar outside that set never blocks (v1.2g).
    """
    k, d = series.k, series.d
    low = i - (params.arm_window_weeks - 1)
    if low < 0:
        return TriggerVerdict(False, False, "not enough history for the arm window", undefined=True)

    def undefined(span: range) -> TriggerVerdict | None:
        if not any(k[j] is None or d[j] is None for j in span):
            return None
        flat = any(series.kd_blocked_by_flat_range(j) for j in span)
        why = "flat stoch range" if flat else "K/D undefined"
        return TriggerVerdict(False, False, f"{why} in the arm/cross window", undefined=True)

    # v1.2g: only bars the check reads can block — the 8-close window here,
    # and the bar before j0 (for the no-earlier-cross check) once j0 is known.
    blocked = undefined(range(low, i + 1))
    if blocked is not None:
        return blocked

    def value(values: tuple[float | None, ...], j: int) -> float:
        v = values[j]
        assert v is not None  # checked above for the whole span
        return v

    def crossed(m: int) -> bool:
        return value(k, m) > value(d, m) and value(k, m - 1) <= value(d, m - 1)

    oversold = [
        j
        for j in range(low, i + 1)
        if value(k, j) < params.arm_level and value(d, j) < params.arm_level
    ]
    if not oversold:
        return TriggerVerdict(False, False, "not armed: no K and D < 20 in the last 8 closes")
    j0 = oversold[-1]
    if j0 == 0:
        return TriggerVerdict(False, False, "not enough history before the oversold close", 0, True)
    blocked = undefined(range(j0 - 1, j0))
    if blocked is not None:
        return blocked
    if not crossed(i):
        if value(k, i) == value(d, i):
            why = "armed; K = D at the close (needs K > D)"
        elif value(k, i) < value(d, i):
            why = "armed; K below D at the close"
        else:
            why = "armed; K above D but was already above last week (no fresh cross)"
        return TriggerVerdict(True, False, why, arm_index=j0)
    if any(crossed(m) for m in range(j0, i)):
        return TriggerVerdict(
            True, False, "armed; not the first cross since the latest oversold close", arm_index=j0
        )
    if not value(k, i) < params.max_k_at_cross:
        return TriggerVerdict(True, False, f"armed; K {value(k, i):.2f} >= 50 at the cross", j0)
    return TriggerVerdict(True, True, "trigger", arm_index=j0)


# --------------------------------------------------------------- sizing
def spacing(atr_pct: float, params: RulesParameters) -> Decimal:
    """s = max(10%, 1.5 x ATR%) — ATR% of the trigger week (spec 4.8)."""
    floor = params.spacing_floor_pct / 100
    return max(floor, params.spacing_atr_mult * Decimal(str(atr_pct)))


def sizing(atr_pct: float, event_risk: bool, params: RulesParameters) -> Sizing:
    """Spec 4.8: A = B x 10% / s (x 0.5 for EVENT_RISK); tranches 40/30/30 of A."""
    s = spacing(atr_pct, params)
    allocation = params.base_allocation * _REFERENCE_SPACING / s
    if event_risk:
        allocation *= params.event_risk_alloc_mult
    allocation = money(allocation)
    amounts = tuple(money(allocation * pct / 100) for pct in params.tranches_pct)
    return Sizing(s, allocation, (amounts[0], amounts[1], amounts[2]), event_risk)


def levels(p1: Decimal, s: Decimal, params: RulesParameters) -> tuple[Decimal, Decimal, Decimal]:
    """L1, L2 and Stop from the T1 fill price, rounded to the paisa (spec 4.8)."""

    def at(multiple: int) -> Decimal:
        return (p1 * (1 - multiple * s)).quantize(PAISA, rounding=ROUND_HALF_UP)

    return at(1), at(2), at(params.stop_spacing_mult)


def _buy_fees(value: Decimal, params: RulesParameters) -> Decimal:
    return money(value * params.cost_bps_buy / _BPS)


def _sell_fees(value: Decimal, params: RulesParameters) -> Decimal:
    return money(value * params.cost_bps_sell / _BPS + params.fixed_cost_per_sell_rs)


def _reserve(amount: Decimal, params: RulesParameters) -> Decimal:
    """Cash a planned buy reserves: its amount plus buy costs (v1.2f)."""
    return amount + _buy_fees(amount, params)


# ---------------------------------------------------------------- fills
@dataclass(frozen=True, slots=True)
class FillOutcome:
    """One order filled (or skipped) at a session's open."""

    order: PendingOrder
    position: Position | None
    cash_delta: Decimal
    closed: ClosedTrade | None = None
    skipped: str | None = None


def apply_fill(
    order: PendingOrder,
    position: Position | None,
    *,
    session: date,
    open_price: float | Decimal,
    params: RulesParameters,
) -> FillOutcome:
    """Fill ``order`` at ``open_price`` on ``session`` — pure.

    Buys take ``floor(amount / price)`` shares; zero shares skips the order
    (spec 4.8). The T1 fill fixes P1, L1, L2 and Stop. An add consumes its
    touch. A ``SELL_HALF`` makes the position HALF_SOLD — no more adds, the
    remaining tranches cancelled, the trail replacing the stop — and a
    ``SELL_ALL`` closes it and yields the :class:`ClosedTrade` for re-entry.

    Raises:
        ValueError: the order does not fit the position's state.
    """
    price = open_price if isinstance(open_price, Decimal) else money(open_price)
    if price <= 0:
        raise ValueError(f"{order.symbol}: open price must be positive")
    action = order.action

    if action.is_buy:
        assert order.amount is not None
        shares = int(order.amount / price)
        if shares == 0:
            return FillOutcome(
                order, position, Decimal("0"), skipped="0 shares at this price; skipped"
            )
        fill = BuyFill(action.tranche, session, price, shares, _buy_fees(price * shares, params))
        cash_delta = -(fill.value + fill.fees)
        if action is OrderAction.BUY_T1:
            if position is not None:
                raise ValueError(f"{order.symbol}: BUY_T1 on an existing position")
            assert order.sizing is not None and order.sector is not None
            assert order.group is not None
            l1, l2, stop = levels(price, order.sizing.spacing, params)
            iso_year, iso_week = week_of(session)
            opened = Position(
                position_id=f"{order.symbol}-{iso_year}W{iso_week:02d}",
                symbol=order.symbol,
                sector=order.sector,
                group=order.group,
                sizing=order.sizing,
                p1=price,
                l1=l1,
                l2=l2,
                stop=stop,
                buys=(fill,),
            )
            return FillOutcome(order, opened, cash_delta)
        if position is None or position.state is not PositionState.OPEN:
            raise ValueError(f"{order.symbol}: {action.value} needs an OPEN position")
        if action.tranche != position.tranches_used + 1:
            raise ValueError(f"{order.symbol}: {action.value} out of order")
        added = replace(position, buys=(*position.buys, fill), touch_week=None)
        return FillOutcome(order, added, cash_delta)

    if position is None or position.state is PositionState.CLOSED:
        raise ValueError(f"{order.symbol}: {action.value} needs a held position")
    assert order.quantity is not None
    if order.quantity > position.shares_held:
        raise ValueError(f"{order.symbol}: selling more shares than held")
    sale = SaleFill(action, session, price, order.quantity)
    sale = replace(sale, fees=_sell_fees(sale.value, params))
    cash_delta = sale.value - sale.fees
    if action is OrderAction.SELL_HALF:
        if position.state is not PositionState.OPEN:
            raise ValueError(f"{order.symbol}: the partial sale happens once per trade")
        half = replace(
            position, sales=(*position.sales, sale), state=PositionState.HALF_SOLD, touch_week=None
        )
        return FillOutcome(order, half, cash_delta)
    if order.quantity != position.shares_held:
        raise ValueError(f"{order.symbol}: SELL_ALL must sell every share held")
    closed = replace(position, sales=(*position.sales, sale), state=PositionState.CLOSED)
    trade = ClosedTrade(closed.symbol, closed.position_id, sale.week, closed.net_pnl)
    return FillOutcome(order, closed, cash_delta, closed=trade)


# --------------------------------------------------------------- brakes
def update_brakes(
    previous: BrakeState,
    equity: Decimal,
    week: WeekKey,
    week_ending: date,
    params: RulesParameters,
) -> BrakeState:
    """Spec 4.12 step 2 of 4.13, with v1.2f's semantics.

    * Brake 2 (equity <= 80% of peak) holds until ``brake_2_cleared_on`` is set
      to a date on or after it fired and on or before this week; clearing
      **resets the peak** to this week's equity, or it would re-fire at once.
    * Brake 1 (equity <= 90% of peak) pauses entries for 4 decision weeks,
      counting this one. It fires again only after equity has closed above
      90% of peak at least once — so a long drawdown pauses entries once, and
      a deeper fall is brake 2's job.
    """
    peak = previous.peak
    fired_on = previous.brake2_fired_on
    cleared_on = params.brake_2_cleared_on
    if fired_on is not None and cleared_on is not None and fired_on <= cleared_on <= week_ending:
        fired_on = None
        peak = equity
    peak = equity if peak is None else max(peak, equity)

    ratio = equity / peak if peak > 0 else Decimal("0")
    if fired_on is None and ratio <= 1 - params.dd2_pct / 100:
        fired_on = week_ending

    threshold = 1 - params.dd1_pct / 100
    can_fire = previous.brake1_can_fire or ratio > threshold
    until = previous.brake1_until
    if ratio <= threshold and can_fire:
        until = shift(week, params.dd1_pause_weeks - 1)
        can_fire = False
    return BrakeState(
        peak=peak, brake1_until=until, brake1_can_fire=can_fire, brake2_fired_on=fired_on
    )


def entries_blocked_by_brakes(brakes: BrakeState, week: WeekKey) -> str | None:
    if brakes.brake2_active:
        return f"drawdown brake 2 active since {brakes.brake2_fired_on}"
    if brakes.brake1_until is not None and weeks_between(week, brakes.brake1_until) >= 0:
        return f"drawdown brake 1: entries paused through week {brakes.brake1_until}"
    return None


def mark_to_market(book: Book, closes: Mapping[str, float | Decimal]) -> Decimal:
    """Equity = cash + sum(shares x weekly close) (spec 4.12).

    Raises:
        ValueError: a held symbol has no close — equity must never be guessed.
    """
    equity = book.cash
    for position in book.positions:
        close = closes.get(position.symbol)
        if close is None:
            raise ValueError(f"no weekly close for held {position.symbol}")
        price = close if isinstance(close, Decimal) else money(close)
        equity += price * position.shares_held
    return equity


# ------------------------------------------------------------- quality
def _quality_allows(row: QualityRow | None, on: date) -> bool:
    """Entry and adds: PASS or EVENT_RISK, valid on the execution date (4.2)."""
    return (
        row is not None
        and row.is_valid_on(on)
        and row.status in (QualityStatus.PASS, QualityStatus.EVENT_RISK)
    )


def _results_in(dates: tuple[date, ...] | None, window: tuple[date, date]) -> bool | None:
    """True / False, or ``None`` when the calendar does not know the symbol."""
    if dates is None:
        return None
    start, end = window
    return any(start <= day <= end for day in dates)


# ------------------------------------------------------------ positions
def review_position(
    position: Position,
    inputs: SymbolWeek | None,
    ctx: WeekContext,
    params: RulesParameters,
    cash_available: Decimal,
) -> tuple[PositionReview, Position]:
    """Spec 4.10 (exits, in order) then 4.9 (adds) for one held position.

    Returns the review (with at most one order) and the position with its
    week's bookkeeping applied: the touch memory, and T3 disabled when the row
    has turned EVENT_RISK (v1.2f).
    """
    if inputs is None or (i := _last_index(inputs.series, ctx.week)) is None:
        return _review(position, "no bar this week: held, no decision", flags=("stale",)), position
    series = inputs.series
    bar = series.bars[i]
    close = money(bar.close)
    row = inputs.quality

    if (
        row is not None
        and row.status is QualityStatus.EVENT_RISK
        and not position.sizing.event_risk
        and not position.t3_disabled
        and position.tranches_used < 3  # v1.2g: only while T3 is unfilled
    ):
        position = replace(position, t3_disabled=True)

    def sell_all(reason: str) -> tuple[PositionReview, Position]:
        order = _order(OrderAction.SELL_ALL, position, ctx, reason, quantity=position.shares_held)
        return _review(position, reason, order), position

    # 1. Thesis exit — an expired FAIL still exits (v1.2f).
    if row is not None and row.status is QualityStatus.FAIL:
        return sell_all("thesis exit: quality gate FAIL")
    removed = inputs.universe_row is None
    last_seen = inputs.last_seen_row
    if removed and last_seen is not None and last_seen.on_exit is OnExit.EXIT:
        return sell_all("thesis exit: left the universe with on_exit: exit")

    if position.state is PositionState.HALF_SOLD:
        ema10 = _ema(series, params.trail_ema, i)
        if _lt(bar.close, ema10):
            return sell_all("trail exit: weekly close below the 10W EMA")
        partial = position.partial_week
        assert partial is not None
        if weeks_between(partial, ctx.week) >= params.trail_time_weeks:
            return sell_all("trail time exit: 52 weeks since the partial sale")
        flags = ("EMA10 undefined",) if ema10 is None else ()
        return _review(position, "hold: trailing", flags=flags), position

    # 2. Stop.
    if close < position.stop:
        return sell_all(f"stop: weekly close {close} below stop {position.stop}")
    # 3. Time exit.
    if weeks_between(position.t1_fill_week, ctx.week) >= params.time_exit_weeks:
        return sell_all("time exit: 52 weeks since the T1 fill with no partial sale")
    # 4. Partial.
    k, d = series.k[i], series.d[i]
    if _gt(k, params.overbought) and _gt(d, params.overbought):
        half = position.shares_held // 2
        if half == 0:
            reason = "partial due (K and D > 90) but half of 1 share rounds to 0"
            return _review(position, reason, flags=("partial rounds to 0",)), position
        order = _order(
            OrderAction.SELL_HALF,
            position,
            ctx,
            "partial: K and D > 90 at the close",
            quantity=half,
        )
        return _review(position, order.reason, order), position
    kd_flags = ("K/D undefined (flat range)",) if series.kd_blocked_by_flat_range(i) else ()

    # 5. Add (spec 4.9).
    return _review_add(position, inputs, i, ctx, params, cash_available, kd_flags)


def _review_add(
    position: Position,
    inputs: SymbolWeek,
    i: int,
    ctx: WeekContext,
    params: RulesParameters,
    cash_available: Decimal,
    flags: tuple[str, ...],
) -> tuple[PositionReview, Position]:
    series = inputs.series
    bar = series.bars[i]
    close = money(bar.close)
    level = position.next_level
    if level is None:
        return _review(position, "hold: no further add", flags=flags), position

    # Touch memory: the first week, from the previous buy's fill week on,
    # whose low reached the level. A close >= P1 clears it (4.9 item 4).
    touch = position.touch_week
    if touch is None and money(bar.low) <= level:
        touch = ctx.week
    if close >= position.p1:
        cleared = touch is not None
        position = replace(position, touch_week=None)
        reason = "hold: close >= P1" + ("; touch cleared" if cleared else "")
        return _review(position, reason, flags=flags), position
    position = replace(position, touch_week=touch)

    tranche = position.tranches_used + 1
    if touch is None:
        return _review(position, f"hold: L{tranche - 1} not touched", flags=flags), position
    refusals: list[str] = []
    if i < 1 or not close > money(series.bars[i - 1].high):
        refusals.append("no reversal (close not above the prior week's high)")
    if weeks_between(position.last_buy_week, ctx.week) <= 0:
        refusals.append("reversal must come after the previous buy's fill week")
    if not close > position.stop:
        refusals.append("close not above the stop")
    if not _gt(bar.close, _ema(series, params.add_ema, i)):
        refusals.append("close not above the 200W EMA")
    if not _quality_allows(inputs.quality, ctx.execution_date):
        refusals.append("quality row not PASS/EVENT_RISK or expired")
    if inputs.universe_row is None:
        refusals.append("left the universe (on_exit: hold): no further adds")
    results = _results_in(inputs.results_dates, ctx.execution_week)
    if results:
        refusals.append("results in the execution week")
    if results is None:
        flags = (*flags, "results date unknown")
    amount = position.sizing.tranche_amounts[tranche - 1]
    if not refusals and cash_available < _reserve(amount, params):
        refusals.append("not enough cash")
    if refusals:
        reason = f"add T{tranche} refused: " + "; ".join(refusals)
        return _review(position, reason, flags=flags), position
    order = _order(
        OrderAction.buy(tranche),
        position,
        ctx,
        f"add T{tranche}: touched L{tranche - 1} {level}, reversal week",
        amount=amount,
    )
    return _review(position, order.reason, order, flags), position


def _order(
    action: OrderAction,
    position: Position,
    ctx: WeekContext,
    reason: str,
    *,
    quantity: int | None = None,
    amount: Decimal | None = None,
) -> PendingOrder:
    return PendingOrder(
        action=action,
        symbol=position.symbol,
        decided_week=ctx.week,
        execute_on_or_after=ctx.execution_date,
        reason=reason,
        position_id=position.position_id,
        amount=amount,
        quantity=quantity,
    )


def _review(
    position: Position,
    reason: str,
    order: PendingOrder | None = None,
    flags: tuple[str, ...] = (),
) -> PositionReview:
    return PositionReview(position.position_id, position.symbol, reason, order, flags)


# -------------------------------------------------------------- entries
@dataclass(frozen=True, slots=True)
class _Candidate:
    symbol: str
    rs: float | None
    sizing: Sizing
    sector: str
    group: str
    nifty100: bool
    flags: tuple[str, ...]


def screen(
    inputs: SymbolWeek,
    index: IndicatorSeries,
    book: Book,
    ctx: WeekContext,
    params: RulesParameters,
) -> FunnelEntry | _Candidate:
    """Spec 4.5 and 4.6 for one symbol not held: a funnel entry if it stops
    here, or a candidate for ranking (4.7)."""
    symbol = inputs.symbol
    series = inputs.series
    i = _last_index(series, ctx.week)
    if i is None:
        return FunnelEntry(symbol, FunnelStage.SKIPPED, "no bar for the decision week")

    verdict = trigger(series, i, params)
    rs = relative_strength(series.bars, index.bars)[i]
    if verdict.undefined:
        return FunnelEntry(symbol, FunnelStage.UNDEFINED, verdict.reason, rs, ("flagged",))
    if not verdict.armed:
        return FunnelEntry(symbol, FunnelStage.NOT_ARMED, verdict.reason, rs)
    if not verdict.triggered:
        return FunnelEntry(symbol, FunnelStage.ARMED, verdict.reason, rs)

    bar = series.bars[i]
    flags: list[str] = []
    refusals: list[str] = []

    # 4.11: re-entry, cooling-off and the fresh arm (v1.2f).
    last = book.last_closed(symbol)
    loss_cooled = False
    if last is not None:
        assert verdict.arm_index is not None
        if last.is_loss:
            loss_cooled = True
            if weeks_between(last.exit_week, ctx.week) < params.cooling_off_weeks:
                refusals.append("cooling-off: 26 weeks after a losing exit")
        if weeks_between(last.exit_week, series.bars[verdict.arm_index].iso_key) <= 0:
            refusals.append("no fresh arm: the oversold close is not after the exit fill week")

    # 4.1 / 4.6 item 5: universe rules and quality.
    row = inputs.universe_row
    if row is None:
        refusals.append("not in the universe file")
    if i + 1 < params.min_history_weeks:
        refusals.append(f"history {i + 1} < {params.min_history_weeks} weekly bars")
    if not _ge(inputs.traded_value_30d, params.min_traded_value_cr * _CRORE):
        refusals.append("liquidity below 20 cr (or unknown)")
    if not _quality_allows(inputs.quality, ctx.execution_date):
        refusals.append("needs quality check")
    if inputs.gap_blocked:
        refusals.append("unacknowledged price gap")

    # 4.6 item 2 (4.11: EMA50 alone after a losing exit).
    ema50 = _ema(series, params.ema_trend, i)
    if loss_cooled:
        if not _gt(bar.close, ema50):
            refusals.append("after a losing exit the close must be above the 50W EMA")
    elif not (_gt(bar.close, ema50) or _gt(rs, 0.0)):
        refusals.append("close not above the 50W EMA and RS not > 0")
    # 4.6 items 3-4.
    if not _ge(bar.close, _mul(series.high_52w[i], params.min_pct_of_52w_high / 100)):
        refusals.append("close below 60% of the 52-week high")
    atr_pct = series.atr_pct[i]
    if not _le(atr_pct, params.max_atr_pct / 100):
        refusals.append("ATR% above 12%")
    # 4.6 item 6.
    results = _results_in(inputs.results_dates, ctx.execution_week)
    if results:
        refusals.append("results in the execution week")
    if results is None:
        flags.append("results date unknown")
    # 4.6 item 8: the most recent earlier trigger within 26 bars (v1.2f).
    repeat = _repeat_refusal(series, i, params)
    if repeat is not None:
        refusals.append(repeat)

    if refusals:
        return FunnelEntry(symbol, FunnelStage.FILTERED, "; ".join(refusals), rs, tuple(flags))
    assert atr_pct is not None and row is not None and inputs.quality is not None
    event_risk = inputs.quality.status is QualityStatus.EVENT_RISK
    return _Candidate(
        symbol=symbol,
        rs=rs,
        sizing=sizing(atr_pct, event_risk, params),
        sector=row.industry,
        group=row.effective_group,
        nifty100=row.nifty100,
        flags=tuple(flags),
    )


def _mul(value: float | None, factor: float) -> float | None:
    return None if value is None else value * factor


def _repeat_refusal(series: IndicatorSeries, i: int, params: RulesParameters) -> str | None:
    """Spec 4.6 item 8 (v1.2f): compare the MOST RECENT earlier trigger.

    Only the latest trigger in bars i-25 .. i-1 counts, traded or not. If
    its close is above this week's, this week's close must also be above the
    prior week's high.
    """
    # v1.2g: "within the last 26 bars" is i-25 .. i-1; a trigger exactly 26
    # bars back is outside, as the 26-week cooling-off counts.
    for j in range(i - 1, max(i - params.repeat_lookback_weeks, -1), -1):
        if not trigger(series, j, params).triggered:
            continue
        lower = series.bars[j].close > series.bars[i].close
        if lower and not series.bars[i].close > series.bars[i - 1].high:
            return (
                f"repeat signal below the previous trigger's close ({series.bars[j].iso_key}) "
                "without a close above the prior week's high"
            )
        return None
    return None


# ------------------------------------------------------------- the week
def decide_week(
    ctx: WeekContext,
    book: Book,
    symbols: Mapping[str, SymbolWeek],
    index: IndicatorSeries,
    params: RulesParameters,
) -> WeekDecision:
    """Spec 4.13 steps 2-4 for one decision week.

    Step 1 (fills) happened before this call, through :func:`apply_fill`.
    Step 2 marks the book at this week's closes and updates the brakes.
    Step 3 reviews every held position (exits, then adds). Step 4 screens,
    ranks and takes new entries with what step 3 left: a decided SELL_ALL frees
    its slot, sector, group and committed amount; a decided SELL_HALF counts the
    cost of the shares that remain; sale proceeds are not cash until filled;
    every planned buy reserves its amount plus buy costs (v1.2f). A BUY order
    still unfilled after step 1 holds its cash and, for a BUY_T1, its slot,
    sector, group and committed amount, exactly as if filled (v1.2g).
    """
    for length in (params.regime_ema, params.ema_trend, params.add_ema, params.trail_ema):
        if length not in EMA_LENGTHS:
            raise ValueError(f"EMA {length} is not computed by indicators.compute")

    # Step 2.
    closes: dict[str, float] = {}
    for position in book.positions:
        inputs = symbols.get(position.symbol)
        if inputs is not None and inputs.series.bars:
            closes[position.symbol] = inputs.series.bars[-1].close
    equity = mark_to_market(book, closes)
    brakes = update_brakes(book.brakes, equity, ctx.week, ctx.week_ending, params)
    current_regime = regime(index, ctx.week, params)

    # Step 3.
    cash_available = book.cash - params.buffer
    pending_symbols = {order.symbol for order in book.pending}
    # v1.2g: a BUY still unfilled after step 1 holds its cash (amount plus buy
    # costs) exactly as if filled; a pending BUY_T1 also holds a slot, sector,
    # group and committed amount (below, step 4).
    for pending in book.pending:
        if pending.action.is_buy:
            assert pending.amount is not None
            cash_available -= _reserve(pending.amount, params)
    pending_entries = [o for o in book.pending if o.action is OrderAction.BUY_T1]
    orders: list[PendingOrder] = []
    reviews: list[PositionReview] = []
    positions: list[Position] = []
    #: Positions still held after step 3's decisions, with what each commits.
    remaining: list[tuple[Position, Decimal]] = []
    for position in sorted(book.positions, key=lambda p: p.symbol):
        if position.symbol in pending_symbols:
            reviews.append(
                _review(position, "an earlier order is still unfilled", flags=("pending",))
            )
            positions.append(position)
            remaining.append((position, position.committed))
            continue
        review, updated = review_position(
            position, symbols.get(position.symbol), ctx, params, cash_available
        )
        reviews.append(review)
        positions.append(updated)
        order = review.order
        if order is not None:
            orders.append(order)
            if order.action.is_buy:
                assert order.amount is not None
                cash_available -= _reserve(order.amount, params)
        if order is not None and order.action is OrderAction.SELL_ALL:
            continue
        if order is not None and order.action is OrderAction.SELL_HALF:
            # v1.2f: committed becomes the cost of the shares that will remain.
            assert order.quantity is not None
            kept = updated.shares_held - order.quantity
            remaining.append((updated, money(updated.average_cost * kept)))
            continue
        remaining.append((updated, updated.committed))

    # Step 4.
    blocked = entries_blocked_by_brakes(brakes, ctx.week)
    if blocked is None and current_regime is Regime.UNKNOWN:
        blocked = "regime unknown (NIFTY 50 EMA40 undefined or no bar): no new entries"
    held = {p.symbol for p in book.positions}
    funnel: list[FunnelEntry] = []
    candidates: list[_Candidate] = []
    for symbol in sorted(symbols):
        if symbol in held:
            funnel.append(FunnelEntry(symbol, FunnelStage.SKIPPED, "already held"))
            continue
        if symbol in pending_symbols:
            funnel.append(FunnelEntry(symbol, FunnelStage.SKIPPED, "an entry order is unfilled"))
            continue
        result = screen(symbols[symbol], index, book, ctx, params)
        if isinstance(result, FunnelEntry):
            funnel.append(result)
        elif current_regime is Regime.RED and not result.nifty100:
            funnel.append(
                FunnelEntry(
                    symbol,
                    FunnelStage.FILTERED,
                    "Red regime: NIFTY 100 only",
                    result.rs,
                    result.flags,
                )
            )
        else:
            candidates.append(result)

    candidates.sort(key=lambda c: (c.rs is None, -(c.rs or 0.0), c.symbol))
    cap = params.red_max_entries if current_regime is Regime.RED else params.normal_max_entries
    committed = sum((amount for _, amount in remaining), Decimal("0"))
    sectors = Counter(p.sector for p, _ in remaining)
    groups = Counter(p.group for p, _ in remaining)
    open_count = len(remaining)
    for entry in pending_entries:
        assert entry.sizing is not None and entry.sector is not None and entry.group is not None
        usable = entry.sizing.tranche_amounts[: entry.sizing.max_tranches]
        committed += sum(usable, Decimal("0"))
        sectors[entry.sector] += 1
        groups[entry.group] += 1
        open_count += 1
    taken = 0
    for c in candidates:
        commitment = sum(c.sizing.tranche_amounts[: c.sizing.max_tranches], Decimal("0"))
        t1 = c.sizing.tranche_amounts[0]
        if blocked is not None:
            why = blocked
        elif taken >= cap:
            why = f"weekly entry cap reached ({current_regime.value}: {cap})"
        elif open_count >= params.max_positions:
            why = f"no free position slot ({params.max_positions} max)"
        elif sectors[c.sector] >= params.max_per_sector:
            why = f"sector full: {c.sector}"
        elif groups[c.group] >= params.max_per_group:
            why = f"promoter group full: {c.group}"
        elif committed + commitment > params.committed_cap:
            why = "committed-capital cap"
        elif cash_available < _reserve(t1, params):
            why = "not enough cash"
        else:
            why = None
        if why is not None:
            funnel.append(FunnelEntry(c.symbol, FunnelStage.NOT_TAKEN, why, c.rs, c.flags))
            continue
        order = PendingOrder(
            action=OrderAction.BUY_T1,
            symbol=c.symbol,
            decided_week=ctx.week,
            execute_on_or_after=ctx.execution_date,
            reason="entry: trigger, RS " + ("undefined" if c.rs is None else f"{c.rs:+.2f} pp"),
            amount=t1,
            sizing=c.sizing,
            sector=c.sector,
            group=c.group,
        )
        orders.append(order)
        funnel.append(FunnelEntry(c.symbol, FunnelStage.TAKEN, order.reason, c.rs, c.flags))
        taken += 1
        open_count += 1
        committed += commitment
        sectors[c.sector] += 1
        groups[c.group] += 1
        cash_available -= _reserve(t1, params)

    funnel.sort(key=lambda entry: entry.symbol)
    return WeekDecision(
        week=ctx.week,
        regime=current_regime,
        equity=equity,
        brakes=brakes,
        orders=tuple(orders),
        positions=tuple(positions),
        reviews=tuple(reviews),
        funnel=tuple(funnel),
        entries_blocked=blocked,
    )
