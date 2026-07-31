"""FIXED_TARGET exit — bank a fixed profit measured in option-premium points.

Evaluated on candle close against the position's latest premium
(``position.last_price``). Profit in points is direction-aware:

* BUY  (long option):  ``last_price - entry_price``
* SELL (written option): ``entry_price - last_price``

Exit once profit reaches ``points``.

Parameters:
    points: premium-point profit that triggers the exit (default 500).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.models import ExitReason, OrderSide

from .base import BaseExit, register_exit_engine


@register_exit_engine("fixed_target")
class FixedTargetExit(BaseExit):
    reason = "Fixed target reached"
    exit_reason = ExitReason.TARGET_PROFIT

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.points = float(self.params.get("points", 500))

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
        return bool(profit >= self.points)
