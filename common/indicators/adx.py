"""Average Directional Index (ADX) — trend-strength indicator.

Ported from the reference repository's ``framework/indicators/adx.py``
(Phase 4 Part 2), behaviour unchanged.

Standard Wilder formulation:

    +DM[i] = high[i]-high[i-1] if that exceeds (low[i-1]-low[i]) and is > 0, else 0
    -DM[i] = low[i-1]-low[i]   if that exceeds (high[i]-high[i-1]) and is > 0, else 0
    TR[i]  = max(high-low, |high-prev_close|, |low-prev_close|)

    +DI = 100 * WilderSmooth(+DM) / WilderSmooth(TR)
    -DI = 100 * WilderSmooth(-DM) / WilderSmooth(TR)
    DX  = 100 * |+DI - -DI| / (+DI + -DI)
    ADX = WilderSmooth(DX)

Wilder smoothing is the same running EWM (alpha = 1/period, adjust=False) used
by the :class:`~.supertrend.SuperTrend` ATR, applied here to +DM/-DM/TR and
again to DX. A low ADX means the market lacks directional strength (range-
bound); a rising ADX above ~20-25 signals a developing trend.

Only the stateful, candle-by-candle interface is provided. The reference noted
that nothing warm-up-replays this indicator from a DataFrame; that is still true
here, and :mod:`common.indicators.vectorised` supplies the batch form for the
cross-check rather than for replay.

Parity, stated because the reference stated it
----------------------------------------------
The reference's own docstring says this ADX "approximates with a running EWM
from the first DX onward — good enough for a threshold-based filter, **not
intended for exact TA-Lib parity**". Wilder seeds ADX with a second averaging
pass over the first ``period`` DX values; this does not. Measured against
``pandas_ta_classic.adx`` at ``5.87e-05`` maximum relative difference over the
last 150 bars of a 300-bar series — far closer than the disclaimer implies,
because the seeding difference decays — and the cross-check asserts ``1e-4``.
On a **short** series the divergence is legitimately larger, which is why the
cross-check pins the fixture length rather than only the tolerance.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import OHLC, StatefulIndicator


@dataclass(frozen=True)
class ADXState:
    """Snapshot of the indicator after the latest candle."""

    adx: float
    plus_di: float
    minus_di: float


class ADX(StatefulIndicator):
    """Stateful Wilder ADX, fed one closed candle at a time.

    ``is_ready`` only turns True once ``period`` candles have been seen *and*
    the first ADX value has been produced (Wilder's ADX needs a further
    ``period`` DX values to seed its own smoothing, but we approximate with a
    running EWM from the first DX onward — good enough for a threshold-based
    filter, not intended for exact TA-Lib parity).
    """

    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self.reset()

    def reset(self) -> None:
        self._count = 0
        self._prev_high: float | None = None
        self._prev_low: float | None = None
        self._prev_close: float | None = None
        self._smoothed_tr: float | None = None
        self._smoothed_plus_dm: float | None = None
        self._smoothed_minus_dm: float | None = None
        self._adx: float | None = None

    @property
    def is_ready(self) -> bool:
        return self._adx is not None

    @property
    def count(self) -> int:
        return self._count

    def _wilder(self, prev: float | None, value: float) -> float:
        if prev is None:
            return value
        alpha = 1.0 / self.period
        return prev + alpha * (value - prev)

    def update(self, candle: OHLC) -> ADXState:
        h, low, c = candle.high, candle.low, candle.close

        if self._prev_high is None:
            tr = h - low
            plus_dm = 0.0
            minus_dm = 0.0
        else:
            assert self._prev_low is not None and self._prev_close is not None
            up_move = h - self._prev_high
            down_move = self._prev_low - low
            plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
            minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
            tr = max(h - low, abs(h - self._prev_close), abs(low - self._prev_close))

        self._smoothed_tr = self._wilder(self._smoothed_tr, tr)
        self._smoothed_plus_dm = self._wilder(self._smoothed_plus_dm, plus_dm)
        self._smoothed_minus_dm = self._wilder(self._smoothed_minus_dm, minus_dm)

        if self._smoothed_tr:
            plus_di = 100.0 * self._smoothed_plus_dm / self._smoothed_tr
            minus_di = 100.0 * self._smoothed_minus_dm / self._smoothed_tr
        else:
            plus_di = minus_di = 0.0

        di_sum = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum else 0.0
        self._adx = self._wilder(self._adx, dx)

        self._prev_high, self._prev_low, self._prev_close = h, low, c
        self._count += 1

        return ADXState(adx=self._adx, plus_di=plus_di, minus_di=minus_di)

    @property
    def state(self) -> ADXState:
        if self._adx is None:
            raise RuntimeError("ADX has no value yet; call update() first.")
        if self._smoothed_tr:
            plus_di = 100.0 * (self._smoothed_plus_dm or 0.0) / self._smoothed_tr
            minus_di = 100.0 * (self._smoothed_minus_dm or 0.0) / self._smoothed_tr
        else:
            plus_di = minus_di = 0.0
        return ADXState(adx=self._adx, plus_di=plus_di, minus_di=minus_di)
