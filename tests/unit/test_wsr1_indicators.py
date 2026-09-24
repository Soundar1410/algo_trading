"""Phase 2. Spec 4.4's indicator formulas, against hand-computed values.

Every expected number below is worked out by hand in the comment next to it,
on series short enough to check with a pencil. The small lengths (2 or 3
instead of 14) change nothing about the formulas — each function takes its
length as an argument — and are what make the arithmetic checkable.

Undefined values are ``None``, never ``NaN`` or ``0``; several tests pin that
directly, because a ``NaN`` K silently compares as "not armed".

TradingView parity on real data is ``test_wsr1_indicator_parity.py``; the
pandas-ta-classic cross-check is ``test_wsr1_indicators_oracle.py``.
"""

from __future__ import annotations

import ast
import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from strategies.positional_stocks.wsr1_weekly_stochrsi import indicators
from strategies.positional_stocks.wsr1_weekly_stochrsi.indicators import (
    atr,
    compute,
    ema,
    highest_high,
    performance,
    ratio,
    relative_strength,
    rma,
    rsi,
    sma,
    stoch,
    true_range,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import WeeklyBar
from strategies.positional_stocks.wsr1_weekly_stochrsi.weekly_bars import iso_key

#: Float tolerance for values whose hand result is a repeating fraction.
EXACT = 1e-12

_FIRST_FRIDAY = date(2020, 1, 3)


def _bar(i: int, *, close: float, high: float | None = None, low: float | None = None) -> WeeklyBar:
    """Weekly bar number ``i`` (consecutive ISO weeks from 2020-W01)."""
    friday = _FIRST_FRIDAY + timedelta(weeks=i)
    iso_year, iso_week = iso_key(friday)
    high = close if high is None else high
    low = close if low is None else low
    return WeeklyBar(
        week_ending=friday,
        iso_year=iso_year,
        iso_week=iso_week,
        open=low,
        high=high,
        low=low,
        close=close,
        expected_last_session=friday,
        sessions=5,
    )


def _bars(closes: list[float]) -> list[WeeklyBar]:
    return [_bar(i, close=c, high=c + 1.0, low=c - 1.0) for i, c in enumerate(closes)]


def _approx(values: list[float | None]) -> list[object]:
    return [None if v is None else pytest.approx(v, abs=EXACT) for v in values]


# ---------------------------------------------------------------- constants
def test_the_spec_lengths_are_the_defaults() -> None:
    assert indicators.RSI_LENGTH == 14
    assert indicators.STOCH_LENGTH == 14
    assert indicators.K_SMOOTHING == 3
    assert indicators.D_SMOOTHING == 3
    assert indicators.ATR_LENGTH == 14
    assert indicators.EMA_LENGTHS == (10, 40, 50, 200)
    assert indicators.HIGH_LOOKBACK_BARS == 52
    assert indicators.PERFORMANCE_LOOKBACK_WEEKS == 26


# ---------------------------------------------------------------------- SMA
def test_sma() -> None:
    # (1+2+3)/3 = 2, (2+3+4)/3 = 3, (3+4+5)/3 = 4
    assert sma([1.0, 2.0, 3.0, 4.0, 5.0], 3) == [None, None, 2.0, 3.0, 4.0]


def test_sma_is_undefined_while_its_window_holds_an_undefined_value() -> None:
    # Windows ending at 2 and 3 contain the None; (3+4+5)/3 = 4, (4+5+6)/3 = 5.
    assert sma([1.0, None, 3.0, 4.0, 5.0, 6.0], 3) == [None, None, None, None, 4.0, 5.0]


def test_a_length_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        sma([1.0], 0)
    with pytest.raises(ValueError, match="at least 1"):
        ema([1.0], 0)


# ---------------------------------------------------------------------- RMA
def test_rma_is_sma_seeded_wilder_smoothing() -> None:
    # length 2, alpha 1/2: seed (2+4)/2 = 3; (3+6)/2 = 4.5; (4.5+8)/2 = 6.25
    assert rma([2.0, 4.0, 6.0, 8.0], 2) == [None, 3.0, 4.5, 6.25]


# ---------------------------------------------------------------------- EMA
def test_ema_is_seeded_with_the_sma_of_the_first_n_closes() -> None:
    # length 3, alpha 2/4 = 0.5: seed (2+4+6)/3 = 4;
    # 0.5*8 + 0.5*4 = 6; 0.5*4 + 0.5*6 = 5
    assert ema([2.0, 4.0, 6.0, 8.0, 4.0], 3) == [None, None, 4.0, 6.0, 5.0]


def test_ema_is_not_seeded_with_the_first_value() -> None:
    # A first-value seed would give 0.5*4 + 0.5*2 = 3 at index 1 and then
    # 4.5 at index 2; spec 4.4 v1.2c settled on the SMA seed (ETERNAL's EMA200).
    values = ema([2.0, 4.0, 6.0], 3)
    assert values[:2] == [None, None]
    assert values[2] == 4.0


def test_ema_is_undefined_before_bar_n() -> None:
    closes = [float(i) for i in range(1, 251)]
    ema200 = ema(closes, 200)
    assert all(v is None for v in ema200[:199])
    # Seed = mean(1..200) = 100.5; then alpha = 2/201 per bar.
    assert ema200[199] == 100.5
    assert ema200[200] == pytest.approx((2 / 201) * 201.0 + (199 / 201) * 100.5, abs=EXACT)
    assert all(v is None for v in ema(closes[:199], 200))


# ---------------------------------------------------------------------- RSI
def test_rsi_hand_computed() -> None:
    # length 2. closes 10, 12, 11, 14, 14 -> changes +2, -1, +3, 0
    #   gains  2, 0, 3, 0     losses 0, 1, 0, 0
    #   bar 2: seed avg gain (2+0)/2 = 1, avg loss (0+1)/2 = 0.5, RS 2
    #          RSI = 100 - 100/3 = 200/3
    #   bar 3: gain (1+3)/2 = 2, loss (0.5+0)/2 = 0.25, RS 8 -> 800/9
    #   bar 4: gain (2+0)/2 = 1, loss 0.125, RS 8 -> 800/9
    assert rsi([10.0, 12.0, 11.0, 14.0, 14.0], 2) == _approx(
        [None, None, 200 / 3, 800 / 9, 800 / 9]
    )


def test_rsi_first_value_is_at_bar_length() -> None:
    # 14 changes are needed for the seed, and bar 0 has no change.
    closes = [float(c) for c in range(1, 16)]
    values = rsi(closes)
    assert values[:14] == [None] * 14
    assert values[14] == 100.0
    assert rsi(closes[:14]) == [None] * 14


def test_rsi_keeps_pines_tie_breaks() -> None:
    # ta.rsi: down == 0 -> 100 (checked first); up == 0 -> 0.
    assert rsi([1.0, 2.0, 3.0, 4.0], 2) == [None, None, 100.0, 100.0]
    assert rsi([4.0, 3.0, 2.0, 1.0], 2) == [None, None, 0.0, 0.0]
    assert rsi([5.0, 5.0, 5.0], 2) == [None, None, 100.0]


# -------------------------------------------------------------------- stoch
def test_stoch_hand_computed_including_the_flat_case() -> None:
    # length 3:
    #   bar 2: window has an undefined value -> warm-up, not flat
    #   bar 3: [10, 20, 30]  -> 100*(30-10)/(30-10) = 100
    #   bar 4: [20, 30, 30]  -> 100*(30-20)/(30-20) = 100
    #   bar 5: [30, 30, 30]  -> max == min: UNDEFINED and flagged, no division
    #   bar 6: [30, 30, 15]  -> 100*(15-15)/(30-15) = 0
    values, flat = stoch([None, 10.0, 20.0, 30.0, 30.0, 30.0, 15.0], 3)
    assert values == [None, None, None, 100.0, 100.0, None, 0.0]
    assert flat == [False, False, False, False, False, True, False]


def test_stoch_mid_range() -> None:
    # bar 3: [50, 100, 75] -> 100*(75-50)/(100-50) = 50
    values, _ = stoch([0.0, 50.0, 100.0, 75.0], 3)
    assert values == [None, None, 100.0, 50.0]


def test_k_and_d_are_sma3_of_stoch_and_of_k() -> None:
    stoch_values: list[float | None] = [0.0, 30.0, 60.0, 90.0, 60.0]
    k = sma(stoch_values, 3)
    # (0+30+60)/3 = 30, (30+60+90)/3 = 60, (60+90+60)/3 = 70
    assert k == [None, None, 30.0, 60.0, 70.0]
    # (30+60+70)/3 = 160/3
    assert sma(k, 3) == _approx([None, None, None, None, 160 / 3])


def test_a_flat_rsi_range_leaves_k_and_d_undefined_and_flagged() -> None:
    # 40 rising closes: every change is a gain, so RSI is 100 from bar 14 on
    # and every stoch window from bar 27 is flat. Then a drop and a recovery.
    closes = [100.0 + i for i in range(40)] + [130.0, 131.0, 132.0, 133.0, 134.0]
    series = compute(_bars(closes))

    assert series.rsi[14] == 100.0
    assert series.stoch_flat[26] is False  # window still reaches warm-up
    assert all(series.stoch_flat[27:40])
    assert all(series.stoch[i] is None for i in range(27, 40))
    assert series.k[39] is None and series.d[39] is None
    assert series.kd_blocked_by_flat_range(39)

    # Bar 40 breaks the range: its RSI is the window's minimum -> stoch 0.
    assert series.stoch_flat[40] is False
    assert series.stoch[40] == 0.0
    # K at 40 still reads flat bars 38, 39; D at 42 still reads flat K inputs.
    assert series.k[40] is None and series.kd_blocked_by_flat_range(40)
    assert series.k[42] is not None and series.d[42] is None
    assert series.kd_blocked_by_flat_range(42)
    # D at 44 reads stoch 40..44, all defined.
    assert series.d[44] is not None
    assert not series.kd_blocked_by_flat_range(44)


def test_plain_warm_up_is_not_reported_as_a_flat_range() -> None:
    series = compute(_bars([100.0 + math.sin(i) * 5 for i in range(20)]))
    assert all(v is None for v in series.k)
    assert not any(series.kd_blocked_by_flat_range(i) for i in range(20))


# ---------------------------------------------------------------------- ATR
_ATR_BARS = [
    _bar(0, close=9.0, high=10.0, low=8.0),  # TR = 10 - 8 = 2 (no previous close)
    _bar(1, close=11.0, high=12.0, low=9.0),  # max(3, |12-9|=3, |9-9|=0) = 3
    _bar(2, close=15.0, high=16.0, low=14.0),  # gap up: max(2, |16-11|=5, |14-11|=3) = 5
    _bar(3, close=12.0, high=13.0, low=10.0),  # gap down: max(3, |13-15|=2, |10-15|=5) = 5
]


def test_true_range_uses_the_previous_close_across_gaps() -> None:
    assert true_range(_ATR_BARS) == [2.0, 3.0, 5.0, 5.0]


def test_atr_is_sma_seeded_wilder_of_true_range() -> None:
    # length 2: seed (2+3)/2 = 2.5; (2.5+5)/2 = 3.75; (3.75+5)/2 = 4.375
    values = atr(_ATR_BARS, 2)
    assert values == [None, 2.5, 3.75, 4.375]
    # ATR% = ATR / close of the same week: 2.5/11, 3.75/15 = 0.25, 4.375/12
    closes = [bar.close for bar in _ATR_BARS]
    assert ratio(values, closes) == _approx([None, 2.5 / 11, 0.25, 4.375 / 12])


# ------------------------------------------------------------- 52-week high
def test_highest_high_includes_the_current_bar() -> None:
    bars = [_bar(i, close=h, high=h) for i, h in enumerate([10.0, 12.0, 16.0, 13.0, 11.0, 17.0])]
    # length 3: max(10,12,16)=16, max(12,16,13)=16, max(16,13,11)=16, max(13,11,17)=17
    assert highest_high(bars, 3) == [None, None, 16.0, 16.0, 16.0, 17.0]


def test_the_52_week_high_needs_52_bars() -> None:
    bars = _bars([100.0 + i for i in range(52)])
    values = highest_high(bars)
    assert values[50] is None
    assert values[51] == 152.0  # the current bar's high (151 + 1)


# ------------------------------------------------------ performance and RS
def test_performance_over_the_lookback() -> None:
    # 2 weeks: 125/100 - 1 = 0.25; 99/110 - 1 = -0.1
    bars = [_bar(i, close=c) for i, c in enumerate([100.0, 110.0, 125.0, 99.0])]
    assert performance(bars, 2) == _approx([None, None, 0.25, -0.1])


def test_six_month_performance_is_26_iso_weeks() -> None:
    bars = _bars([100.0] * 26 + [150.0])
    values = performance(bars)
    assert values[25] is None
    assert values[26] == 0.5


def test_performance_counts_iso_weeks_not_bars() -> None:
    # Spec 4.4 v1.2e. Week 2 is missing (a suspension). The bar in week 3
    # compares with week 1 (130/110 - 1), not with the bar two positions back
    # (week 0), which is what a bar count would do.
    bars = [_bar(i, close=c) for i, c in [(0, 100.0), (1, 110.0), (3, 132.0), (4, 150.0)]]
    values = performance(bars, 2)
    assert values[2] == pytest.approx(132.0 / 110.0 - 1.0, abs=EXACT)
    # Week 4 compares with week 2, which has no bar: undefined.
    assert values[3] is None


def test_performance_crosses_a_53_week_iso_year() -> None:
    # 2026 has an ISO week 53 (28 Dec 2026 - 3 Jan 2027). Two weeks before
    # 2027-W01 is 2026-W52, with W53 in between.
    fridays = [date(2026, 12, 18), date(2026, 12, 25), date(2027, 1, 1), date(2027, 1, 8)]
    closes = [100.0, 104.0, 108.0, 120.0]
    bars = [
        WeeklyBar(
            week_ending=friday,
            iso_year=iso_key(friday)[0],
            iso_week=iso_key(friday)[1],
            open=close,
            high=close,
            low=close,
            close=close,
            expected_last_session=friday,
            sessions=5,
        )
        for friday, close in zip(fridays, closes, strict=True)
    ]
    assert [bar.iso_key for bar in bars] == [(2026, 51), (2026, 52), (2026, 53), (2027, 1)]
    # 2027-W01 vs 2026-W52: 120/104 - 1
    assert performance(bars, 2)[3] == pytest.approx(120.0 / 104.0 - 1.0, abs=EXACT)


def test_relative_strength_is_percentage_points_matched_by_iso_week() -> None:
    stock = [_bar(i, close=c) for i, c in enumerate([100.0, 110.0, 125.0])]
    # The index starts a week earlier; matching is by ISO week, not position.
    # Index performance at stock bar 2: 220/200 - 1 = 0.10.
    index = [_bar(i - 1, close=c) for i, c in enumerate([190.0, 200.0, 210.0, 220.0])]
    # (0.25 - 0.10) * 100 = 15 percentage points
    assert relative_strength(stock, index, 2) == _approx([None, None, 15.0])


def test_relative_strength_is_undefined_for_a_week_the_index_lacks() -> None:
    stock = [_bar(i, close=c) for i, c in enumerate([100.0, 110.0, 125.0])]
    index = [_bar(i, close=c) for i, c in enumerate([200.0, 210.0])]
    assert relative_strength(stock, index, 2) == [None, None, None]


def test_relative_strength_compares_the_same_calendar_window() -> None:
    # The stock missed week 2; the index did not. Both 2-week performances at
    # week 3 are measured from week 1: stock 121/110 - 1 = 0.10, index
    # 240/200 - 1 = 0.20 -> -10 pp. A bar count would have measured the stock
    # from week 0 instead.
    stock = [_bar(i, close=c) for i, c in [(0, 100.0), (1, 110.0), (3, 121.0)]]
    index = [_bar(i, close=c) for i, c in [(0, 190.0), (1, 200.0), (2, 220.0), (3, 240.0)]]
    assert relative_strength(stock, index, 2) == _approx([None, None, -10.0])


# ------------------------------------------------------------------ compute
def _wave(n: int) -> list[float]:
    return [100.0 + 10.0 * math.sin(i / 3.0) + 0.1 * i for i in range(n)]


def test_compute_first_defined_bar_of_every_series() -> None:
    series = compute(_bars(_wave(260)))

    def first(values: tuple[float | None, ...]) -> int:
        return next(i for i, v in enumerate(values) if v is not None)

    assert first(series.rsi) == 14
    assert first(series.stoch) == 27  # 14 RSI values: bars 14..27
    assert first(series.k) == 29
    assert first(series.d) == 31
    assert {n: first(v) for n, v in series.ema.items()} == {10: 9, 40: 39, 50: 49, 200: 199}
    assert first(series.atr) == 13
    assert first(series.atr_pct) == 13
    assert first(series.high_52w) == 51
    assert first(series.performance_6m) == 26
    assert not any(series.stoch_flat)


def test_compute_matches_the_building_blocks() -> None:
    bars = _bars(_wave(120))
    closes = [bar.close for bar in bars]
    series = compute(bars)
    stoch_values, _ = stoch(rsi(closes))
    assert list(series.k) == sma(stoch_values, 3)
    assert list(series.d) == sma(sma(stoch_values, 3), 3)
    assert list(series.ema[50]) == ema(closes, 50)
    assert list(series.atr) == atr(bars)


@pytest.mark.parametrize("order", ["reversed", "duplicate"])
def test_compute_refuses_bars_out_of_iso_week_order(order: str) -> None:
    bars = _bars([100.0, 101.0, 102.0])
    bad = list(reversed(bars)) if order == "reversed" else [bars[0], bars[0], bars[1]]
    with pytest.raises(ValueError, match="strictly ascending"):
        compute(bad)


def test_compute_on_an_empty_series() -> None:
    series = compute([])
    assert series.k == () and series.ema[200] == ()


# ------------------------------------------------------------------- purity
def test_indicators_module_is_pure() -> None:
    """No I/O, no clock, no network, no pandas, no ``common.engine`` (spec 13)."""
    path = Path(indicators.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or ""))
    assert imported == {
        "__future__",
        "collections.abc",
        "dataclasses",
        "itertools",
        ".iso_weeks",
        ".models",
    }
    source = path.read_text(encoding="utf-8")
    for call in ("open(", "now(", "time.", "datetime"):
        assert call not in source.split('"""', 2)[2], call
