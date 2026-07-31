"""HIGHEST_CLOSE / BEST_CLOSE_TRAIL exit — a candle-close trailing stop.

Tracks the extreme candle close since entry and exits once price retraces far
enough from it. Two independent axes of configuration:

* ``basis`` — what "long vs short" means:
    * ``option_type`` (default): CE (long call) tracks the **highest** close;
      PE (long put) tracks the **lowest** close. Written for underlying-candle
      strategies where CE/PE stands in for directional bias.
    * ``side``: driven by the actual :class:`~framework.core.models.Position`
      side instead — BUY tracks highest, SELL tracks lowest. Required for
      premium-candle evaluation (see :mod:`framework.exit.combined_candle_exit`).
* ``trail_type`` — how the retracement is measured:
    * ``POINTS`` (default, backward compatible): exit once the close gives back
      ``trail_points`` from the extreme.
    * ``PERCENTAGE``: exit once the close has retraced ``trail_percentage``
      percent from the extreme close. An optional ``activation`` gate can
      require the extreme to first reach ``minimum_favourable_move_percentage``
      percent beyond the entry price before the trail arms; once armed it stays
      armed for the life of the position.

Stateful across candles within one position, so :meth:`reset` clears the extreme
(and activation flag) when a fresh position opens.

Parameters:
    trail_points: retracement (points) from the extreme close that triggers the
        exit when ``trail_type: POINTS`` (default 20).
    trail_percentage: retracement (percent of the extreme close) that triggers
        the exit when ``trail_type: PERCENTAGE`` (default 8.0).
    activation.enabled / activation.minimum_favourable_move_percentage: optional
        gate — the trail cannot fire until the extreme has moved this many
        percent beyond the entry price in the favourable direction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.models import ExitReason, OptionType, OrderSide

from .base import BaseExit, register_exit_engine


@register_exit_engine("highest_close")
class HighestCloseExit(BaseExit):
    reason = "Trailed off extreme close"
    exit_reason = ExitReason.TRAILING_STOP

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.basis = str(self.params.get("basis", "option_type")).lower()
        if self.basis not in {"option_type", "side"}:
            raise ValueError(
                f"highest_close.basis must be 'option_type' or 'side' (got {self.basis!r})"
            )
        self.trail_type = str(self.params.get("trail_type", "POINTS")).upper()
        if self.trail_type not in {"POINTS", "PERCENTAGE"}:
            raise ValueError(
                "highest_close.trail_type must be 'POINTS' or 'PERCENTAGE' "
                f"(got {self.trail_type!r})"
            )
        self.trail_points = float(self.params.get("trail_points", 20))
        self.trail_percentage = float(self.params.get("trail_percentage", 8.0))
        activation = self.params.get("activation") or {}
        self.activation_enabled = bool(activation.get("enabled", False))
        self.min_favourable_move_percentage = float(
            activation.get("minimum_favourable_move_percentage", 0.0)
        )
        self._extreme: float | None = None
        self._activated: bool = False

    def reset(self) -> None:
        self._extreme = None
        self._activated = False

    def _is_long(self, position: Any) -> bool:
        if self.basis == "side":
            return position.side is OrderSide.BUY
        return position.contract.option_type is OptionType.CE

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
        is_long = self._is_long(position)
        if is_long:
            self._extreme = close if self._extreme is None else max(self._extreme, close)
        else:
            self._extreme = close if self._extreme is None else min(self._extreme, close)

        if self.trail_type == "POINTS":
            if is_long:
                return bool((self._extreme - close) >= self.trail_points)
            return bool((close - self._extreme) >= self.trail_points)

        # PERCENTAGE: optional activation gate, then a percentage retracement.
        if self.activation_enabled and not self._activated:
            entry = position.entry_price
            move_pct = (
                (self._extreme - entry) / entry * 100
                if is_long
                else (entry - self._extreme) / entry * 100
            )
            if move_pct < self.min_favourable_move_percentage:
                return False
            self._activated = True

        if is_long:
            retrace_pct = (self._extreme - close) / self._extreme * 100
        else:
            retrace_pct = (close - self._extreme) / self._extreme * 100
        return bool(retrace_pct >= self.trail_percentage)
