"""TIME_EXIT — square off the position at or after a configured clock time.

Exits once the current candle's timestamp reaches ``time`` (IST, ``HH:MM``).
This is a strategy-level, config-selectable square-off; the engine's hard
``session.square_off_time`` remains the ultimate backstop.

Parameters:
    time: ``HH:MM`` at/after which to exit (default "15:15").
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.models import ExitReason
from common.utils.timeutils import parse_hhmm

from .base import BaseExit, register_exit_engine


@register_exit_engine("time_exit")
class TimeExit(BaseExit):
    reason = "Time square-off"
    exit_reason = ExitReason.SQUARE_OFF

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self._time = parse_hhmm(str(self.params.get("time", "15:15")))

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
        if timestamp is None:
            return False
        return timestamp.time() >= self._time
