"""Phase 2. Cross-check ``indicators.py`` against pandas-ta-classic, after warm-up.

pandas-ta-classic is an **oracle here, never a dependency of the strategy**:
``indicators.py`` imports no pandas at all, and
``test_indicator_oracle_boundary.py`` keeps the library out of every shipped
package except ``common/indicators/vectorised.py``.

Why "after warm-up" and not from the first bar: the two differ in where they
seed, and TradingView (spec section 7) is the authority, not the library.

* RSI — the library's ``diff`` leaves bar 0 undefined and its RMA then averages
  ``iloc[0:14]``, i.e. only 13 real changes, one bar earlier than ``ta.rsi``.
* ATR — its true range is undefined on bar 0, where ``ta.tr(true)`` uses
  high - low, so its seed averages a different 14 bars.

Both differences decay geometrically, by 13/14 per bar. At bar 150 the ATR gap is
still about 1e-6 relative on a 4%-volatility walk; by bar 250 it is about 1e-8,
far below the tolerance used here. **EMA** seeds identically — the SMA of the
first n closes — so it is compared exactly from its first defined bar.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import pandas as pd
import pandas_ta_classic as ta
import pytest

from strategies.positional_stocks.wsr1_weekly_stochrsi.indicators import compute
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import WeeklyBar
from strategies.positional_stocks.wsr1_weekly_stochrsi.weekly_bars import iso_key

BARS = 400
WARM_UP = 250


def _random_walk(seed: int) -> list[WeeklyBar]:
    rng = random.Random(seed)
    close = 100.0
    bars: list[WeeklyBar] = []
    for i in range(BARS):
        open_ = close
        close = open_ * (1.0 + rng.gauss(0.0, 0.04))
        high = max(open_, close) * (1.0 + abs(rng.gauss(0.0, 0.02)))
        low = min(open_, close) * (1.0 - abs(rng.gauss(0.0, 0.02)))
        friday = date(2010, 1, 1) + timedelta(weeks=i)
        iso_year, iso_week = iso_key(friday)
        bars.append(
            WeeklyBar(
                week_ending=friday,
                iso_year=iso_year,
                iso_week=iso_week,
                open=open_,
                high=high,
                low=low,
                close=close,
                expected_last_session=friday,
                sessions=5,
            )
        )
    return bars


def _assert_close(
    ours: tuple[float | None, ...], oracle: pd.Series, start: int, rel: float
) -> None:
    compared = 0
    for i in range(start, len(ours)):
        value = ours[i]
        expected = float(oracle.iloc[i])
        assert value is not None and not math.isnan(expected), i
        assert value == pytest.approx(expected, rel=rel, abs=1e-9), i
        compared += 1
    assert compared > 0


@pytest.mark.parametrize("seed", [7, 11, 2026])
def test_indicators_agree_with_pandas_ta_classic(seed: int) -> None:
    bars = _random_walk(seed)
    series = compute(bars)
    close = pd.Series([bar.close for bar in bars])
    high = pd.Series([bar.high for bar in bars])
    low = pd.Series([bar.low for bar in bars])

    _assert_close(series.rsi, ta.rsi(close, length=14), WARM_UP, rel=1e-6)

    stochrsi = ta.stochrsi(close, length=14, rsi_length=14, k=3, d=3)
    _assert_close(series.k, stochrsi["STOCHRSIk_14_14_3_3"], WARM_UP, rel=1e-6)
    _assert_close(series.d, stochrsi["STOCHRSId_14_14_3_3"], WARM_UP, rel=1e-6)

    for length in (10, 40, 50, 200):
        # Same seed as ours: exact from the first defined bar.
        _assert_close(series.ema[length], ta.ema(close, length=length), length - 1, rel=1e-12)

    _assert_close(series.atr, ta.atr(high, low, close, length=14), WARM_UP, rel=1e-6)
