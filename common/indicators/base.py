"""Indicator base contract.

Indicators in this project come in two flavours that must agree:

* a **vectorised** form (batch over a DataFrame) used to seed state from
  historical candles, and
* a **stateful/incremental** form fed one closed candle at a time during live
  trading.

:class:`StatefulIndicator` defines the incremental contract. Keeping it
minimal makes it easy to add new indicators without touching the engine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # annotation only; the runtime import stays deferred, as ported
    from common.warmup.requirements import WarmupRequirement

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class OHLC:
    """A single completed candle's price data (metadata optional).

    ``start`` is the exchange/chart bar-open timestamp.  It is deliberately
    distinct from the later wall-clock time at which the first tick of the next
    bucket lets the engine discover that this bar has closed.
    """

    high: float
    low: float
    close: float
    open: float | None = None
    volume: float | None = None
    start: datetime | None = None
    #: True when this bar's interval contained a hole in the tick stream, so its
    #: OHLC is stitched across missing data. Phase 4 Part 3. The engine does not
    #: feed such a bar to indicators at all — see
    #: :meth:`common.engine.strategy.BaseStrategy.on_candle_gap` — so an
    #: indicator seeing this set means someone bypassed that rule.
    spans_gap: bool = False


class StatefulIndicator(ABC):
    """An indicator that updates one closed candle at a time."""

    @abstractmethod
    def update(self, candle: OHLC) -> Any:
        """Incorporate a newly *closed* candle into the indicator state.

        Returns whatever snapshot the concrete indicator publishes (SuperTrend
        returns a :class:`~common.indicators.supertrend.SuperTrendState`). The
        reference declared ``None`` here while every implementation returned its
        state object; the annotation is widened rather than the implementations
        narrowed, so no ported indicator's behaviour changes.
        """

    @abstractmethod
    def reset(self) -> None:
        """Clear all state (called at the start of each trading day)."""

    @property
    @abstractmethod
    def is_ready(self) -> bool:
        """True once enough candles have been seen to produce a valid value."""

    def warmup_requirement(self) -> WarmupRequirement:
        """Declare this indicator's history need for :mod:`framework.warmup`.

        Default: session-spanning, ``min_bars`` = the indicator's ``period`` (or 1
        if it has none), no volume needed — correct for trend/MA/range indicators
        (SuperTrend, EMA, ATR). Session-cumulative indicators (VWAP) override this
        to declare :attr:`~framework.warmup.requirements.IndicatorScope.SESSION_LOCAL`.
        """
        from common.warmup.requirements import WarmupRequirement

        return WarmupRequirement(min_bars=int(getattr(self, "period", 1)))
