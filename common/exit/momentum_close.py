"""MOMENTUM_CLOSE exit — exit when consecutive candle closes turn adverse.

Two candle-stream modes are supported:

* ``underlying`` (default): CE exits on a lower underlying close; PE exits on a
  higher underlying close. This preserves the original single-leg behavior.
* ``premium``: BUY exits on a lower option-premium close; SELL exits on a
  higher option-premium close. Fixed-strike strategies use this mode because
  each CE/PE chart is the traded option's own premium series.

Pure function of the last two candle closes — no state to reset.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.models import ExitReason, OptionType, OrderSide

from .base import BaseExit, register_exit_engine


@register_exit_engine("momentum_close")
class MomentumCloseExit(BaseExit):
    reason = "Momentum Broken"
    exit_reason = ExitReason.STRATEGY_EXIT

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.price_stream = str(self.params.get("price_stream", "underlying")).lower()
        if self.price_stream not in {"underlying", "premium"}:
            raise ValueError(
                "momentum_close.price_stream must be 'underlying' or 'premium' "
                f"(got {self.price_stream!r})"
            )

    def should_exit_closes(
        self,
        current_close: float,
        previous_close: float | None,
        *,
        side: OrderSide | None = None,
        option_type: OptionType | None = None,
    ) -> bool:
        """Compare one closed candle with its immediate predecessor.

        This method lets option-chart strategies use the same registered exit
        engine without manufacturing a framework ``Position`` object.
        """
        if previous_close is None:
            return False
        if self.price_stream == "premium":
            if side is OrderSide.BUY:
                return current_close < previous_close
            if side is OrderSide.SELL:
                return current_close > previous_close
            return False
        if option_type is OptionType.CE:
            return current_close < previous_close
        if option_type is OptionType.PE:
            return current_close > previous_close
        return False

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
        prev_close = candle_history[-1].close if candle_history else None
        return self.should_exit_closes(
            candle.close,
            prev_close,
            side=getattr(position, "side", None),
            option_type=getattr(position.contract, "option_type", None),
        )
