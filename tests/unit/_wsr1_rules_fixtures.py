"""Builders for the ``wsr1_weekly_stochrsi`` rules tests.

The rules read an :class:`IndicatorSeries`; these helpers build one directly,
column by column, so each golden case states exactly the K, D, EMA and ATR
values it depends on instead of reverse-engineering prices that would produce
them. Formula correctness is ``test_wsr1_indicators*.py``'s job.

Defaults describe an unremarkable, entry-eligible stock at its last bar: 210
weekly bars (history >= 200), close 1000 with a 10-point range, K = D = 50
(neither oversold nor crossing), EMA50 900 (close above it), EMA200 800, EMA10
950, ATR 6%, 52-week high 1100.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from strategies.positional_stocks.wsr1_weekly_stochrsi.indicators import (
    EMA_LENGTHS,
    IndicatorSeries,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    Book,
    OnExit,
    OrderAction,
    PendingOrder,
    Position,
    QualityRow,
    QualityStatus,
    RulesParameters,
    UniverseRow,
    WeeklyBar,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import (
    SymbolWeek,
    WeekContext,
    apply_fill,
    sizing,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.weekly_bars import iso_key

D = Decimal
#: Spec 4.12's paper book with costs zeroed: the V1 plan's worked numbers are
#: all "before costs".
ZERO = D("0")
PARAMS0 = RulesParameters(cost_bps_buy=ZERO, cost_bps_sell=ZERO, fixed_cost_per_sell_rs=ZERO)
PARAMS = RulesParameters()

FIRST_FRIDAY = date(2021, 1, 8)
N = 210

Column = float | None | Sequence[float | None] | Mapping[int, float | None]


def friday(i: int) -> date:
    return FIRST_FRIDAY + timedelta(weeks=i)


def week(i: int) -> tuple[int, int]:
    return iso_key(friday(i))


def monday_after(i: int) -> date:
    return friday(i) + timedelta(days=3)


def ctx(i: int = N - 1, execution_date: date | None = None) -> WeekContext:
    return WeekContext(week(i), friday(i), execution_date or monday_after(i))


def _column(value: Column, n: int, default: float | None) -> list[float | None]:
    """A scalar, a full list, a tail list (aligned to the end), or {index: value}."""
    if value is None and default is not None:
        return [default] * n
    if isinstance(value, Mapping):
        out: list[float | None] = [default] * n
        for index, v in value.items():
            out[index if index >= 0 else n + index] = v
        return out
    if isinstance(value, Sequence):
        values = list(value)
        if len(values) > n:
            raise ValueError("column longer than the series")
        return [default] * (n - len(values)) + values
    return [value] * n


@dataclass
class Tape:
    """Per-bar columns. Sequences shorter than ``n`` are aligned to the end."""

    n: int = N
    close: Column = 1000.0
    high: Column = None
    low: Column = None
    k: Column = 50.0
    d: Column = 50.0
    ema10: Column = 950.0
    ema40: Column = 900.0
    ema50: Column = 900.0
    ema200: Column = 800.0
    atr_pct: Column = 0.06
    high_52w: Column = 1100.0
    flat: Sequence[int] = ()

    def series(self) -> IndicatorSeries:
        n = self.n
        closes = [float(c) for c in _column(self.close, n, 1000.0)]  # type: ignore[arg-type]
        highs = _column(self.high, n, None) if self.high is not None else [None] * n
        lows = _column(self.low, n, None) if self.low is not None else [None] * n
        bars = []
        for i, c in enumerate(closes):
            hi = highs[i] if highs[i] is not None else c + 10.0
            lo = lows[i] if lows[i] is not None else c - 10.0
            assert hi is not None and lo is not None
            bars.append(
                WeeklyBar(
                    week_ending=friday(i),
                    iso_year=week(i)[0],
                    iso_week=week(i)[1],
                    open=lo,
                    high=max(hi, c),
                    low=min(lo, c),
                    close=c,
                    expected_last_session=friday(i),
                    sessions=5,
                )
            )
        emas = {10: self.ema10, 40: self.ema40, 50: self.ema50, 200: self.ema200}
        assert set(emas) == set(EMA_LENGTHS)
        none: tuple[float | None, ...] = (None,) * n
        return IndicatorSeries(
            bars=tuple(bars),
            rsi=none,
            stoch=none,
            stoch_flat=tuple(i in {f if f >= 0 else n + f for f in self.flat} for i in range(n)),
            k=tuple(_column(self.k, n, 50.0)),
            d=tuple(_column(self.d, n, 50.0)),
            ema={length: tuple(_column(v, n, None)) for length, v in emas.items()},
            atr=none,
            atr_pct=tuple(_column(self.atr_pct, n, 0.06)),
            high_52w=tuple(_column(self.high_52w, n, 1100.0)),
            performance_6m=none,
        )


def kd_tape(pairs: Sequence[tuple[float | None, float | None]], **columns: Any) -> Tape:
    """A :class:`Tape` whose K/D tail is ``pairs`` (oldest first, ending at the last bar)."""
    return Tape(k=[p[0] for p in pairs], d=[p[1] for p in pairs], **columns)


def index_series(
    n: int = N, *, close: Column = 20000.0, ema40: Column = 19000.0
) -> IndicatorSeries:
    """NIFTY 50: Normal by default (close above a flat EMA40)."""
    return Tape(n=n, close=close, ema40=ema40).series()


def universe_row(
    symbol: str,
    industry: str = "Industry-" + "X",
    *,
    nifty100: bool = True,
    group: str | None = None,
    on_exit: OnExit = OnExit.HOLD,
) -> UniverseRow:
    return UniverseRow(symbol, "INE000000000", symbol, industry, nifty100, group, on_exit, None)


def quality(
    symbol: str, status: QualityStatus = QualityStatus.PASS, valid_until: date = date(2099, 1, 1)
) -> QualityRow:
    return QualityRow(symbol, status, None, valid_until)


def symbol_week(
    symbol: str,
    series: IndicatorSeries,
    *,
    industry: str | None = None,
    row: UniverseRow | bool | None = True,
    quality_row: QualityRow | bool | None = True,
    results_dates: tuple[date, ...] | None = (),
    traded_value: float | None = 3e8,
    gap_blocked: bool = False,
    last_seen_row: UniverseRow | None = None,
) -> SymbolWeek:
    universe = (
        universe_row(symbol, industry or f"Industry-{symbol}") if row is True else row or None
    )
    q = quality(symbol) if quality_row is True else quality_row or None
    return SymbolWeek(
        symbol=symbol,
        series=series,
        universe_row=universe,
        quality=q,
        results_dates=results_dates,
        traded_value_30d=traded_value,
        gap_blocked=gap_blocked,
        last_seen_row=last_seen_row,
    )


def open_position(
    symbol: str,
    *,
    atr_pct: float,
    p1: float,
    fill_week: int,
    sector: str = "Finance",
    event_risk: bool = False,
    params: RulesParameters = PARAMS0,
) -> Position:
    """A position opened by a real BUY_T1 fill on the Monday after ``fill_week - 1``."""
    size = sizing(atr_pct, event_risk, params)
    order = PendingOrder(
        OrderAction.BUY_T1,
        symbol,
        week(fill_week - 1),
        monday_after(fill_week - 1),
        "entry",
        amount=size.tranche_amounts[0],
        sizing=size,
        sector=sector,
        group=symbol,
    )
    outcome = apply_fill(
        order, None, session=monday_after(fill_week - 1), open_price=p1, params=params
    )
    assert outcome.position is not None
    return outcome.position


def fill(
    position: Position,
    action: OrderAction,
    *,
    price: float,
    week_index: int,
    quantity: int | None = None,
    params: RulesParameters = PARAMS0,
) -> Position:
    """Apply an add or a sale to ``position`` at ``price`` on the Monday of week ``week_index``."""
    amount = position.sizing.tranche_amounts[action.tranche - 1] if action.is_buy else None
    order = PendingOrder(
        action,
        position.symbol,
        week(week_index - 1),
        monday_after(week_index - 1),
        "test",
        position_id=position.position_id,
        amount=amount,
        quantity=quantity,
    )
    outcome = apply_fill(
        order, position, session=monday_after(week_index - 1), open_price=price, params=params
    )
    assert outcome.position is not None and outcome.skipped is None
    return outcome.position


_START_CASH = D("1000000")


def book(*positions: Position, cash: Decimal = _START_CASH, **kw: Any) -> Book:
    return Book(cash=cash, positions=tuple(positions), **kw)
