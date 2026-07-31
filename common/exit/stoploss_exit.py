"""STOPLOSS exit — a fixed stop measured in option-premium points.

Evaluated on candle close against the position's latest premium
(``position.last_price``). Loss in points is direction-aware:

* BUY  (long option):  ``entry_price - last_price``
* SELL (written option): ``last_price - entry_price``

Exit once the loss reaches ``points``.

This is a candle-close backstop; the strategy's per-tick risk manager (if any)
remains the immediate, intrabar stop. Both can run together.

Parameters:
    points: premium-point loss that triggers the exit (default 300).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.models import ExitReason, OrderSide

from .base import BaseExit, register_exit_engine


@register_exit_engine("stoploss")
class StopLossExit(BaseExit):
    reason = "Stop loss hit"
    exit_reason = ExitReason.STOP_LOSS

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.points = float(self.params.get("points", 300))

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
            loss = position.entry_price - position.last_price
        else:
            loss = position.last_price - position.entry_price
        return bool(loss >= self.points)
