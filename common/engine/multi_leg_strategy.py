"""Multi-leg strategy base class and registry.

Sibling to :mod:`common.engine.strategy`, not a reuse of it. ``BaseStrategy``'s
abstract surface — one ``option_selection``, one ``trade_side``, one
``risk_manager`` for the single position :class:`~common.engine.engine.
TradingEngine` ever holds — is single-leg-shaped by construction. A straddle (and
any future multi-leg strategy) has independently-sided legs and basket-level risk
that no single ``RiskManager`` instance represents, so forcing a multi-leg
strategy to satisfy ``BaseStrategy``'s properties would mean returning values for
questions :class:`~common.engine.multi_leg_engine.MultiLegEngine` never asks.

:class:`~common.engine.engine.TradingEngine`'s registry (``register_strategy``/
``get_strategy``) is untouched by this module — a multi-leg strategy registers
here instead, under its own name, and the two registries can never collide
because they hold different base classes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from common.indicators.base import OHLC
from common.models import Tick

from .multi_leg_models import Basket, BasketSignal, LegInstance

if TYPE_CHECKING:
    from .models import Trade

# name -> BaseMultiLegStrategy subclass
_REGISTRY: dict[str, type[BaseMultiLegStrategy]] = {}


def register_multi_leg_strategy(
    name: str,
) -> Callable[[type[BaseMultiLegStrategy]], type[BaseMultiLegStrategy]]:
    """Class decorator that registers a multi-leg strategy under ``name``."""

    def _wrap(cls: type[BaseMultiLegStrategy]) -> type[BaseMultiLegStrategy]:
        key = name.lower()
        if key in _REGISTRY and _REGISTRY[key] is not cls:
            raise ValueError(f"Multi-leg strategy name {name!r} is already registered.")
        _REGISTRY[key] = cls
        cls.name = key
        return cls

    return _wrap


def get_multi_leg_strategy(name: str, cfg: Any) -> BaseMultiLegStrategy:
    key = name.lower()
    if key not in _REGISTRY:
        raise KeyError(
            f"Unknown multi-leg strategy {name!r}. Registered: "
            f"{', '.join(sorted(_REGISTRY)) or '(none)'}."
        )
    return _REGISTRY[key](cfg)


def available_multi_leg_strategies() -> list[str]:
    return sorted(_REGISTRY)


class BaseMultiLegStrategy(ABC):
    """Turns closed underlying candles and leg ticks into basket decisions.

    Contract:
        * :meth:`on_candle` is called once per *closed* underlying candle and
          returns a :class:`~common.engine.multi_leg_models.BasketSignal` to
          act on, or ``None``. Always handed the current :class:`Basket` so
          the strategy never needs private durable state of its own — every
          basket/leg fact it needs (adjustment count, pending replacement,
          which legs are open, original basis) is already on ``basket``.
        * :meth:`reset` is called at the start of each trading day.
        * :meth:`on_leg_tick` is called on every fresh tick for an open leg,
          in addition to the engine's own hard-square-off check — this is
          where the exact 5-step risk order (adjustment, daily loss, combined
          stop, profit target) lives for a strategy like ``straddle_920``.
        * The strategy never mutates ``basket`` — it only reads it and
          returns a signal; the engine is the sole writer.
    """

    name: str = "base_multi_leg"

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self.params: dict[str, Any] = dict(getattr(cfg, "parameters", {}) or {})

    @abstractmethod
    def on_candle(
        self,
        candle: OHLC,
        timestamp: datetime,
        *,
        basket: Basket,
        vix: float | None,
    ) -> BasketSignal | None: ...

    @abstractmethod
    def reset(self) -> None: ...

    def on_leg_tick(self, leg: LegInstance, tick: Tick, basket: Basket) -> BasketSignal | None:
        """Called on every fresh tick for an OPEN leg. Default: no-op.

        Preserve the exact risk-evaluation order the spec requires (hard
        square-off is the engine's own, evaluated before this is ever
        called): leg-doubling adjustment first, then daily loss, then
        combined stop, then profit target — the first one that fires wins for
        this tick.
        """
        return None

    def on_leg_closed(self, trade: Trade, leg: LegInstance, basket: Basket) -> None:
        """Notification hook: a leg has closed, for any reason. Default: no-op.

        Informational only — the strategy must not need to remember this
        itself; every fact a later decision needs (adjustment count, pending
        replacement role) is already durable on ``basket``, which the engine
        updated before calling this.
        """
        return None

    def exit_state_snapshot(self) -> dict[str, Any]:
        """Restart-recoverable *supplementary* state, beyond the durable basket/leg
        tables migration 0009 already persists. Default ``{}`` — most multi-leg
        strategies need nothing here, because :class:`Basket`/``LegInstance`` are
        already the durable source of truth (see the module docstring)."""
        return {}

    def restore_exit_state(self, data: dict[str, Any]) -> None:
        """Reapply a previous :meth:`exit_state_snapshot`. Default no-op. Must
        never raise — see :class:`~common.engine.strategy.BaseStrategy.
        restore_exit_state` for the identical fail-open rationale."""
        return None

    def status(self) -> str:
        """Short, human-readable state for periodic heartbeat logs."""
        return "no status"

    def warmup_spec(self) -> Any:
        """Opt into historical warm-up. Default ``None`` = not required — a
        time/event-based strategy like ``straddle_920`` has no indicator
        history to warm (spec section 9.1)."""
        return None

    @property
    @abstractmethod
    def quantity_lots(self) -> int:
        """Lots per leg for this strategy's default entry."""
