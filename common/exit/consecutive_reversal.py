"""CONSECUTIVE_REVERSAL exit — exit after N candles closing against the position.

* CE (long call): ``reverse_candles`` consecutive **lower** closes -> exit.
* PE (long put):  ``reverse_candles`` consecutive **higher** closes -> exit.

The streak counter resets to zero on any candle that closes in the favourable
direction (or flat), so only an uninterrupted run triggers the exit. Stateful, so
:meth:`reset` clears the streak when a fresh position opens.

Parameters:
    reverse_candles: length of the adverse-close streak that triggers the exit
        (default 2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.models import ExitReason, OptionType

from .base import BaseExit, register_exit_engine


@register_exit_engine("consecutive_reversal")
class ConsecutiveReversalExit(BaseExit):
    reason = "Consecutive reversal candles"
    exit_reason = ExitReason.STRATEGY_EXIT

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.reverse_candles = max(1, int(self.params.get("reverse_candles", 2)))
        self._streak = 0
        self._prev_close: float | None = None

    def reset(self) -> None:
        self._streak = 0
        self._prev_close = None

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
        close = candle.close
        if self._prev_close is not None:
            is_ce = position.contract.option_type is OptionType.CE
            adverse = close < self._prev_close if is_ce else close > self._prev_close
            self._streak = self._streak + 1 if adverse else 0
        self._prev_close = close
        return self._streak >= self.reverse_candles
