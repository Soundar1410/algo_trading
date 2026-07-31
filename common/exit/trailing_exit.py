"""TRAILING exit — a premium trailing stop measured in option-premium points.

Tracks the position's peak profit (premium points, direction-aware) since entry
and exits once profit retraces ``trail_points`` from that peak. The trail only
arms once profit is positive, so it never fires while the trade is still under
water (the STOPLOSS engine owns that side).

Stateful across candles within one position, so :meth:`reset` clears the peak
when a fresh position opens.

Parameters:
    trail_points: premium-point give-back from peak profit that triggers the exit
        (default 20).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.models import ExitReason, OrderSide

from .base import BaseExit, register_exit_engine


@register_exit_engine("trailing")
class TrailingExit(BaseExit):
    reason = "Trailing stop hit"
    exit_reason = ExitReason.TRAILING_STOP

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.trail_points = float(self.params.get("trail_points", 20))
        self._peak_profit = 0.0

    def reset(self) -> None:
        self._peak_profit = 0.0

    def should_exit(
        self,
        position: Any,
        candle: Any,
        candle_history: list[Any],
        indicators: dict[str, Any],
        strategy_config: Any,
        *,
        timestamp: datetime | None = None,
    ) -> bool:
        if position.side is OrderSide.BUY:
            profit = position.last_price - position.entry_price
        else:
            profit = position.entry_price - position.last_price
        if profit > self._peak_profit:
            self._peak_profit = profit
        # Only trail once we've been in profit; below water is the stop's job.
        if self._peak_profit <= 0:
            return False
        return bool((self._peak_profit - profit) >= self.trail_points)
