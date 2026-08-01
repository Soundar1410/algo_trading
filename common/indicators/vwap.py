"""Volume Weighted Average Price (session-cumulative, stateful).

Ported from the reference repository's ``framework/indicators/vwap.py``
(Phase 4 Part 2), behaviour unchanged.

VWAP is reset at the start of each trading day (call :meth:`reset` in the
same place the engine resets other daily state) and accumulates over closed
candles using the typical price ``(high + low + close) / 3`` weighted by
volume.

**This is the one indicator the default warm-up requirement gets wrong**, which
is why it is the only one here that overrides
:meth:`~common.indicators.base.StatefulIndicator.warmup_requirement`. The
default in ``base.py`` declares ``SESSION_SPANNING`` with ``min_bars = period``,
correct for trend and moving-average indicators that self-correct from a cold
start. VWAP is neither: it is *session-cumulative*, so warming it with a prior
session's candles does not seed it — it corrupts it, permanently, for the whole
day. It also needs volume on the live stream, which most of the others do not.
Both facts are declared so the warm-up manager (Part 4) steers around it rather
than having to special-case it.

Agreement with ``pandas_ta_classic.vwap`` is **exact** (``7.5e-16``, float
precision) when the oracle is anchored to the same session, because both compute
the same cumulative sum of typical price times volume.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation only; the runtime import stays deferred, as ported
    from common.warmup.requirements import WarmupRequirement

from dataclasses import dataclass

from .base import OHLC, StatefulIndicator


@dataclass
class VWAPState:
    value: float


class VWAP(StatefulIndicator):
    """Stateful, session-cumulative VWAP fed one closed candle at a time."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._cum_pv = 0.0
        self._cum_vol = 0.0
        self._count = 0

    @property
    def is_ready(self) -> bool:
        return self._cum_vol > 0

    def warmup_requirement(self) -> WarmupRequirement:
        # VWAP is session-cumulative (must be warmed with today's candles only,
        # never prior sessions) AND volume-weighted (needs volume on the live
        # stream too). Both facts steer the WarmupManager away from corrupting it.
        from common.warmup.requirements import IndicatorScope, WarmupRequirement

        return WarmupRequirement(
            min_bars=1, scope=IndicatorScope.SESSION_LOCAL, requires_volume=True
        )

    def update(self, candle: OHLC) -> VWAPState:
        volume = candle.volume or 0.0
        typical_price = (candle.high + candle.low + candle.close) / 3.0
        self._cum_pv += typical_price * volume
        self._cum_vol += volume
        self._count += 1
        return self.state if self.is_ready else VWAPState(value=0.0)

    @property
    def state(self) -> VWAPState:
        if self._cum_vol <= 0:
            raise RuntimeError("VWAP has no volume yet; call update() with volume > 0 first.")
        return VWAPState(value=self._cum_pv / self._cum_vol)
