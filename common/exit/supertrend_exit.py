"""SUPERTREND exit — exit when the SuperTrend flips against the position.

Reads the strategy's live SuperTrend indicator (passed in the ``indicators``
dict under the key ``"supertrend"``) and exits when its trend opposes the open
leg:

* CE (long call): exit once the trend is DOWN.
* PE (long put):  exit once the trend is UP.

No state of its own — the trend lives in the indicator. Does nothing until the
indicator is ready or if no SuperTrend was supplied.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.indicators.supertrend import DOWNTREND, UPTREND
from common.models import ExitReason, OptionType

from .base import BaseExit, register_exit_engine


@register_exit_engine("supertrend")
class SuperTrendExit(BaseExit):
    reason = "SuperTrend flipped against position"
    exit_reason = ExitReason.STRATEGY_EXIT

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        # Which key in the indicators dict holds the SuperTrend (overridable).
        self._key = str(self.params.get("indicator_key", "supertrend"))

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
        st = (indicators or {}).get(self._key)
        if st is None or not getattr(st, "is_ready", False):
            return False
        trend = st.state.trend
        if position.contract.option_type is OptionType.CE:
            return bool(trend == DOWNTREND)
        return bool(trend == UPTREND)
