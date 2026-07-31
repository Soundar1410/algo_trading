"""MOMENTUM_LOW exit — exit when the close breaks the previous candle's extreme.

Two ``basis`` modes select what "long vs short" means:

* ``option_type`` (default): CE (long call) exits if
  ``current close < previous candle low``; PE (long put) exits if
  ``current close > previous candle high``. Written for *underlying*-candle
  strategies where CE/PE stands in for the position's directional bias.
* ``side``: driven by the actual :class:`~framework.core.models.Position` side
  instead of CE/PE — BUY (long) exits on ``close < prev.low``, SELL (short)
  exits on ``close > prev.high``. Required for *premium*-candle evaluation
  (e.g. a bought PE is still "long" in premium terms, unlike its CE/PE-relative
  underlying behaviour), and by :mod:`framework.exit.combined_candle_exit`.

A stricter cousin of MOMENTUM_CLOSE: it waits for the close to pierce the prior
candle's range, not merely its close. No state to reset.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.models import ExitReason, OptionType, OrderSide

from .base import BaseExit, register_exit_engine


@register_exit_engine("momentum_low")
class MomentumLowExit(BaseExit):
    reason = "Broke previous candle range"
    exit_reason = ExitReason.STRATEGY_EXIT

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.basis = str(self.params.get("basis", "option_type")).lower()
        if self.basis not in {"option_type", "side"}:
            raise ValueError(
                f"momentum_low.basis must be 'option_type' or 'side' (got {self.basis!r})"
            )

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
        if not candle_history:
            return False
        prev = candle_history[-1]
        if self.basis == "side":
            is_long = position.side is OrderSide.BUY
        else:
            is_long = position.contract.option_type is OptionType.CE
        if is_long:
            return bool(candle.close < prev.low)
        return bool(candle.close > prev.high)
