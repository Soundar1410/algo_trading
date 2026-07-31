"""MOMENTUM_LOW_OR_HIGHEST_CLOSE — combined candle-structure + best-close trail.

A position exits when *either* rule fires:

* candle-structure (:class:`~framework.exit.momentum_low.MomentumLowExit`,
  ``basis="side"``): close breaks the previous completed candle's extreme
  against the position.
* best-close trail (:class:`~framework.exit.highest_close.HighestCloseExit`,
  ``basis="side"``, ``trail_type="PERCENTAGE"``): close retraces
  ``trail_percentage`` percent from the best completed close since entry, with
  an optional activation threshold.

Both children are evaluated every call (so the trail's extreme keeps updating
even on a candle where only momentum fires) and the combined reason is
granular — :attr:`exit_reason` distinguishes a momentum-only exit, a trail-only
exit, and the case where both fired on the same candle — rather than collapsing
everything to a generic STRATEGY_EXIT, so logs/reports don't have to re-derive
which rule(s) triggered from raw candle data.

Driven by position **side** (BUY/SELL), never CE/PE — a bought PE is "long" in
premium terms just like a bought CE. Intended to be evaluated on the traded
instrument's own (premium) candle stream, not the underlying.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.models import ExitReason, OrderSide

from .base import BaseExit, register_exit_engine
from .highest_close import HighestCloseExit
from .momentum_low import MomentumLowExit


@register_exit_engine("momentum_low_or_highest_close")
class CombinedCandleExit(BaseExit):
    reason = "Momentum break or best-close trail"
    exit_reason = ExitReason.STRATEGY_EXIT

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        trail_percentage = float(self.params.get("trail_percentage", 8.0))
        activation = dict(self.params.get("activation") or {})
        # Also accept a flat minimum_favourable_move_percentage (spec's simplest
        # form) alongside the nested activation.* block.
        flat_min_move = self.params.get("minimum_favourable_move_percentage")
        if flat_min_move is not None and "minimum_favourable_move_percentage" not in activation:
            activation.setdefault("enabled", True)
            activation["minimum_favourable_move_percentage"] = flat_min_move

        self._momentum = MomentumLowExit({"basis": "side"})
        self._trail = HighestCloseExit(
            {
                "basis": "side",
                "trail_type": "PERCENTAGE",
                "trail_percentage": trail_percentage,
                "activation": activation,
            }
        )

        self.momentum_fired: bool = False
        self.trail_fired: bool = False
        self._highest_close: float | None = None
        self._lowest_close: float | None = None

    def reset(self) -> None:
        self._momentum.reset()
        self._trail.reset()
        self.momentum_fired = False
        self.trail_fired = False
        self._highest_close = None
        self._lowest_close = None

    @property
    def extreme_close(self) -> float | None:
        """Highest (long) / lowest (short) completed close since entry."""
        return self._trail._extreme

    @property
    def trail_activated(self) -> bool:
        """True once the best-close trail's activation gate has armed (or
        always True when no activation gate is configured)."""
        return (not self._trail.activation_enabled) or self._trail._activated

    @property
    def highest_close(self) -> float | None:
        return self._highest_close

    @property
    def lowest_close(self) -> float | None:
        return self._lowest_close

    def apply_to_trade(self, trade: Any) -> None:
        """Persist this position's final candle-exit state on ``trade``.

        Called for every close while the mode is configured, including hard-risk
        exits, so downstream reports can distinguish "mode active, rule did not
        trigger" from a strategy that did not use this exit mode at all.
        """
        is_long = trade.side is OrderSide.BUY
        best = self._highest_close if is_long else self._lowest_close
        retracement = None
        retracement_pct = None
        if best is not None and best > 0:
            retracement = (
                max(best - trade.exit_price, 0.0) if is_long else max(trade.exit_price - best, 0.0)
            )
            retracement_pct = retracement / best * 100
        trade.exit_mode = "MOMENTUM_LOW_OR_HIGHEST_CLOSE"
        trade.highest_completed_close = self._highest_close
        trade.lowest_completed_close = self._lowest_close
        trade.best_favourable_close = best
        trade.retracement_points = retracement
        trade.retracement_percentage = retracement_pct
        trade.candle_structure_triggered = self.momentum_fired
        trade.trail_triggered = self.trail_fired
        trade.trail_activated = self.trail_activated

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
        close = float(candle.close)
        self._highest_close = (
            close if self._highest_close is None else max(self._highest_close, close)
        )
        self._lowest_close = close if self._lowest_close is None else min(self._lowest_close, close)
        # Evaluate both every call so the trail's extreme keeps tracking even
        # when only momentum fires (and vice versa).
        self.momentum_fired = self._momentum.should_exit(
            position, candle, candle_history, indicators, strategy_config, timestamp=timestamp
        )
        self.trail_fired = self._trail.should_exit(
            position, candle, candle_history, indicators, strategy_config, timestamp=timestamp
        )

        is_long = position.side is OrderSide.BUY
        if self.momentum_fired and self.trail_fired:
            self.label = "MOMENTUM_AND_TRAIL"
            self.reason = "Momentum break and best-close trail both triggered"
            self.exit_reason = ExitReason.MOMENTUM_AND_TRAIL
        elif self.momentum_fired:
            self.label = "MOMENTUM_LOW" if is_long else "MOMENTUM_HIGH"
            self.reason = "Momentum break (candle-structure rule)"
            self.exit_reason = ExitReason.MOMENTUM_LOW if is_long else ExitReason.MOMENTUM_HIGH
        elif self.trail_fired:
            self.label = "HIGHEST_CLOSE_TRAIL" if is_long else "LOWEST_CLOSE_TRAIL"
            self.reason = "Best-close trail retracement"
            self.exit_reason = (
                ExitReason.HIGHEST_CLOSE_TRAIL if is_long else ExitReason.LOWEST_CLOSE_TRAIL
            )

        return self.momentum_fired or self.trail_fired
