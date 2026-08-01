"""Relative Strength Index (Wilder-smoothed, stateful).

Ported from the reference repository's ``framework/indicators/rsi.py``
(Phase 4 Part 2), behaviour unchanged.

**This indicator arrived with no reference coverage of any kind.** Nothing in the
reference repository constructs ``RSI`` — not a test, not a strategy, not a
framework consumer. It is the only one of the five ported here whose behaviour
was never exercised by the system it came from. Its correctness in this
repository therefore rests on the ``pandas-ta-classic`` cross-check plus the
tests written alongside this port, and it should be treated as the least-proven
of the five until a strategy consumes it in Phase 9. Recorded in the runbook
rather than left to be discovered.

The cross-check is at least reassuring: this is the standard Wilder formulation
(SMA seed over the first ``period`` changes, then ``alpha = 1/period``), and it
agrees with ``pandas_ta_classic.rsi`` to ``7.4e-16`` — float precision, i.e.
exactly. That catches a transcription error. It cannot catch a formula both
implementations get wrong the same way, which is what the missing reference
coverage would have caught.

Before warm-up completes, ``update`` returns a neutral ``50.0`` rather than
raising, while :attr:`state` raises. That asymmetry is the reference's and is
kept: a caller polling ``update`` every bar wants a value it can ignore, and a
caller reaching for ``state`` has asserted the indicator is ready.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .base import OHLC, StatefulIndicator


@dataclass
class RSIState:
    value: float


class RSI(StatefulIndicator):
    """Stateful Wilder RSI, fed one closed candle at a time.

    Uses Wilder's smoothing (equivalent to an EWM with alpha=1/period),
    matching the convention already used by :class:`SuperTrend`'s ATR.
    """

    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self.reset()

    def reset(self) -> None:
        self._prev_close: float | None = None
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._count = 0
        self._seed_gains: deque[float] = deque(maxlen=self.period)
        self._seed_losses: deque[float] = deque(maxlen=self.period)

    @property
    def is_ready(self) -> bool:
        return self._avg_gain is not None

    def update(self, candle: OHLC) -> RSIState:
        c = candle.close
        if self._prev_close is None:
            self._prev_close = c
            self._count += 1
            return self.state if self.is_ready else RSIState(value=50.0)

        change = c - self._prev_close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        if self._avg_gain is None:
            self._seed_gains.append(gain)
            self._seed_losses.append(loss)
            if len(self._seed_gains) == self.period:
                self._avg_gain = sum(self._seed_gains) / self.period
                self._avg_loss = sum(self._seed_losses) / self.period
        else:
            alpha = 1.0 / self.period
            self._avg_gain = self._avg_gain + alpha * (gain - self._avg_gain)
            assert self._avg_loss is not None  # set together with _avg_gain
            self._avg_loss = self._avg_loss + alpha * (loss - self._avg_loss)

        self._prev_close = c
        self._count += 1
        return self.state if self.is_ready else RSIState(value=50.0)

    @property
    def state(self) -> RSIState:
        if self._avg_gain is None:
            raise RuntimeError("RSI has no value yet; call update() until warmed up.")
        if self._avg_loss == 0:
            return RSIState(value=100.0)
        assert self._avg_loss is not None
        rs = self._avg_gain / self._avg_loss
        return RSIState(value=100.0 - (100.0 / (1.0 + rs)))
