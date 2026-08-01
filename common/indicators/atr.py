"""Standalone Average True Range (Wilder-smoothed, stateful).

Ported from the reference repository's ``framework/indicators/atr.py``
(Phase 4 Part 2), behaviour unchanged.

:class:`~common.indicators.supertrend.SuperTrend` already computes an internal
ATR, but strategies that need ATR independently — ATR-based position sizing, a
custom risk manager, or the regime classifier this part also ports — can use
this directly instead of duplicating the smoothing logic.

Seeding, and why the cross-check is not exact
---------------------------------------------
This ATR seeds ``_atr = tr`` on the **first bar**. Wilder's own formulation, and
``pandas_ta_classic.atr``, seed with the simple average of the first ``period``
true ranges. The two therefore disagree early and converge exponentially, the
gap decaying as ``(1-1/period)**n``. Measured at ``2.85e-06`` maximum relative
difference over the last 150 bars of a 300-bar series; the cross-check asserts
``1e-5``. The difference is structural, not a defect in either — but it means an
ATR read in the first few bars of a session is not the same number a chart would
show, which matters more for a threshold than for a ratio.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import OHLC, StatefulIndicator


@dataclass
class ATRState:
    value: float


class ATR(StatefulIndicator):
    """Stateful Wilder ATR, fed one closed candle at a time."""

    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self.reset()

    def reset(self) -> None:
        self._prev_close: float | None = None
        self._atr: float | None = None
        self._count = 0

    @property
    def is_ready(self) -> bool:
        return self._atr is not None

    def update(self, candle: OHLC) -> ATRState:
        h, low, c = candle.high, candle.low, candle.close
        if self._prev_close is None:
            tr = h - low
        else:
            tr = max(h - low, abs(h - self._prev_close), abs(low - self._prev_close))

        if self._atr is None:
            self._atr = tr
        else:
            alpha = 1.0 / self.period
            self._atr = self._atr + alpha * (tr - self._atr)

        self._prev_close = c
        self._count += 1
        return self.state

    @property
    def state(self) -> ATRState:
        if self._atr is None:
            raise RuntimeError("ATR has no value yet; call update() first.")
        return ATRState(value=float(self._atr))
