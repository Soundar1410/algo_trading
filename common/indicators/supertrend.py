"""SuperTrend indicator.

SuperTrend is an ATR-band trend filter.

Algorithm (standard / TradingView-compatible):

    hl2          = (high + low) / 2
    atr          = Wilder-smoothed ATR over `period`
    upper_basic  = hl2 + multiplier * atr
    lower_basic  = hl2 + (- multiplier * atr)

    final_upper[i] = upper_basic[i]
        if upper_basic[i] < final_upper[i-1] or close[i-1] > final_upper[i-1]
        else final_upper[i-1]
    final_lower[i] = lower_basic[i]
        if lower_basic[i] > final_lower[i-1] or close[i-1] < final_lower[i-1]
        else final_lower[i-1]

    Direction flips to DOWN when close crosses below final_lower (was up),
    and to UP when close crosses above final_upper (was down). The SuperTrend
    line is the active band.

Two interfaces are provided:

* :func:`supertrend` — vectorised over a pandas DataFrame; used to seed state
  from historical candles.
* :class:`SuperTrend` — stateful, fed one closed candle at a time for live use.

Both must produce identical results for the same candle series.

Trend encoding: ``+1`` = uptrend (bullish), ``-1`` = downtrend (bearish).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation only; the runtime import stays deferred, as ported
    from common.warmup.requirements import WarmupRequirement

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .base import OHLC, StatefulIndicator

UPTREND = 1
DOWNTREND = -1


def _wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Wilder-smoothed Average True Range.

    With ``period == 1`` this reduces to the True Range of each bar.
    """
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    # Wilder's smoothing == EWM with alpha = 1/period (adjust=False).
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def supertrend(
    df: pd.DataFrame,
    period: int = 1,
    multiplier: float = 1.0,
    *,
    high: str = "high",
    low: str = "low",
    close: str = "close",
) -> pd.DataFrame:
    """Compute SuperTrend over a DataFrame of OHLC candles.

    Args:
        df: candles with at least the high/low/close columns.
        period: ATR period.
        multiplier: ATR band multiplier.
        high/low/close: column names.

    Returns:
        A DataFrame (indexed like ``df``) with columns:
        ``supertrend`` (the active band value) and ``trend`` (+1 / -1).
    """
    if df.empty:
        return pd.DataFrame(columns=["supertrend", "trend"], index=df.index)

    h, low_s, c = df[high], df[low], df[close]
    hl2 = (h + low_s) / 2.0
    atr = _wilder_atr(h, low_s, c, period)

    upper_basic = hl2 + multiplier * atr
    lower_basic = hl2 - multiplier * atr

    n = len(df)
    final_upper = np.zeros(n)
    final_lower = np.zeros(n)
    trend = np.zeros(n, dtype=int)
    st_line = np.zeros(n)

    ub = upper_basic.to_numpy()
    lb = lower_basic.to_numpy()
    cl = c.to_numpy()

    for i in range(n):
        if i == 0:
            final_upper[i] = ub[i]
            final_lower[i] = lb[i]
            # Seed direction from where close sits relative to the bands.
            trend[i] = UPTREND if cl[i] >= lb[i] else DOWNTREND
            st_line[i] = final_lower[i] if trend[i] == UPTREND else final_upper[i]
            continue

        final_upper[i] = (
            ub[i]
            if (ub[i] < final_upper[i - 1] or cl[i - 1] > final_upper[i - 1])
            else final_upper[i - 1]
        )
        final_lower[i] = (
            lb[i]
            if (lb[i] > final_lower[i - 1] or cl[i - 1] < final_lower[i - 1])
            else final_lower[i - 1]
        )

        if trend[i - 1] == UPTREND:
            trend[i] = DOWNTREND if cl[i] < final_lower[i] else UPTREND
        else:
            trend[i] = UPTREND if cl[i] > final_upper[i] else DOWNTREND

        st_line[i] = final_lower[i] if trend[i] == UPTREND else final_upper[i]

    return pd.DataFrame({"supertrend": st_line, "trend": trend}, index=df.index)


@dataclass
class SuperTrendState:
    """Snapshot of the indicator after the latest candle."""

    trend: int  # +1 uptrend / -1 downtrend
    line: float  # active SuperTrend band value
    flipped: bool  # True if trend changed on the latest candle


class SuperTrend(StatefulIndicator):
    """Stateful SuperTrend for live, candle-by-candle updates.

    Feed each *closed* candle via :meth:`update`; read :attr:`state` for the
    current trend and whether it just flipped. Call :meth:`reset` at the start
    of each trading day for fresh state.
    """

    def __init__(self, period: int = 1, multiplier: float = 1.0) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self.multiplier = float(multiplier)
        self.reset()

    def reset(self) -> None:
        self._count = 0
        self._prev_close: float | None = None
        self._atr: float | None = None
        self._final_upper: float | None = None
        self._final_lower: float | None = None
        self._trend: int | None = None
        self._line: float | None = None
        self._flipped = False

    @property
    def is_ready(self) -> bool:
        # A trend value exists after the first candle. With period>1 the ATR is
        # still warming up via EWM, but a (provisional) trend is defined.
        #
        # NOTE: "ready" here means "has a value", NOT "is correct". The first
        # candle seeds the trend outright (see update(): _count == 0), so a
        # cold-started SuperTrend is ready and wrong at the same time. Anything
        # gating TRADES must consult warmup_requirement().continuity_required,
        # never this. See md_docs/Warmup_Fail_Closed_Gate.md.
        return self._trend is not None

    def warmup_requirement(self) -> WarmupRequirement:
        """SuperTrend cannot be approximated from live candles alone.

        The default (``min_bars = period``) describes only the ATR's need, which
        for ``period=1`` reads as "one candle warms me". That is the false
        comfort that let 2026-07-17's cold-started charts trade: the trend is
        *latched* path-dependent state, changing only when price crosses a band,
        so a cold seed has no guaranteed or bounded convergence to the
        historically continuous trend and can hold the wrong direction
        indefinitely -- then read the first crossing as a fresh flip.

        No bar count can encode "continuous with history", so this declares
        ``continuity_required`` and lets the engine fail closed instead.
        """
        from common.warmup.requirements import WarmupRequirement

        return WarmupRequirement(min_bars=self.period, continuity_required=True)

    @property
    def count(self) -> int:
        """Number of closed candles fed so far (for warm-up reporting)."""
        return self._count

    def _update_atr(self, tr: float) -> float:
        if self._atr is None:
            self._atr = tr
        else:
            alpha = 1.0 / self.period
            self._atr = self._atr + alpha * (tr - self._atr)
        return self._atr

    def update(self, candle: OHLC) -> SuperTrendState:
        h, low, c = candle.high, candle.low, candle.close
        if self._prev_close is None:
            tr = h - low
        else:
            tr = max(h - low, abs(h - self._prev_close), abs(low - self._prev_close))
        atr = self._update_atr(tr)

        hl2 = (h + low) / 2.0
        upper_basic = hl2 + self.multiplier * atr
        lower_basic = hl2 - self.multiplier * atr

        prev_trend = self._trend
        if self._count == 0:
            final_upper = upper_basic
            final_lower = lower_basic
            trend = UPTREND if c >= lower_basic else DOWNTREND
        else:
            # Reaching here means a previous update() completed, which always
            # sets all three (see the assignments at the end of this method). The
            # reference suppressed this with `# type: ignore`; stating the
            # invariant instead means a genuine violation surfaces as a named
            # error rather than a TypeError deep inside the comparison.
            fu_prev = self._final_upper
            fl_prev = self._final_lower
            prev_close = self._prev_close
            if fu_prev is None or fl_prev is None or prev_close is None:  # pragma: no cover
                raise RuntimeError(
                    "SuperTrend state is inconsistent: count > 0 with no previous bar."
                )
            final_upper = (
                upper_basic if (upper_basic < fu_prev or prev_close > fu_prev) else fu_prev
            )
            final_lower = (
                lower_basic if (lower_basic > fl_prev or prev_close < fl_prev) else fl_prev
            )
            if prev_trend == UPTREND:
                trend = DOWNTREND if c < final_lower else UPTREND
            else:
                trend = UPTREND if c > final_upper else DOWNTREND

        self._final_upper = final_upper
        self._final_lower = final_lower
        self._trend = trend
        self._line = final_lower if trend == UPTREND else final_upper
        self._flipped = prev_trend is not None and prev_trend != trend
        self._prev_close = c
        self._count += 1

        return self.state

    @property
    def state(self) -> SuperTrendState:
        # ``_line`` is checked alongside ``_trend`` for the same reason the
        # ``update()`` branch above states its invariant explicitly rather than
        # carrying the reference's ``# type: ignore``: both are set together at
        # the end of every ``update()``, so one being None without the other is
        # a broken invariant, not a caller error. The reference tested ``_trend``
        # alone, which let a None ``_line`` fall through to ``float(None)`` and
        # surface as a TypeError from inside the constructor call below.
        # RuntimeError is deliberate and matches the message this property
        # already raises: the failure is "this indicator has no value", which is
        # a state error, not a type error — and it keeps both reachable failures
        # of this property on one exception type so callers need one handler.
        if self._trend is None or self._line is None:
            raise RuntimeError("SuperTrend has no value yet; call update() first.")
        return SuperTrendState(trend=self._trend, line=float(self._line), flipped=self._flipped)
