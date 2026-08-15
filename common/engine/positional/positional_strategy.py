"""Positional multi-leg strategy base class and registry.

Sibling to :mod:`common.engine.multi_leg_strategy`, for the same reason that
module is a sibling of :mod:`common.engine.strategy`: a positional strategy's
questions are delta/quote/expiry-driven, evaluated across sessions, not
"what happened on this completed candle" — :class:`BasePositionalMultiLegStrategy`
is handed a :class:`~common.engine.positional.positional_models.Cycle` plus a
:class:`PositionalContext` on every evaluation, not a bare candle.

Its own registry, never colliding with either existing one (different base
class).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from common.greeks import GreekSnapshot
from common.market_data.chain_view import ChainView

from .positional_models import Cycle, CycleSignal

_REGISTRY: dict[str, type[BasePositionalMultiLegStrategy]] = {}


def register_positional_strategy(
    name: str,
) -> Callable[[type[BasePositionalMultiLegStrategy]], type[BasePositionalMultiLegStrategy]]:
    def _wrap(
        cls: type[BasePositionalMultiLegStrategy],
    ) -> type[BasePositionalMultiLegStrategy]:
        key = name.lower()
        if key in _REGISTRY and _REGISTRY[key] is not cls:
            raise ValueError(f"Positional strategy name {name!r} is already registered.")
        _REGISTRY[key] = cls
        cls.name = key
        return cls

    return _wrap


def get_positional_strategy(name: str, cfg: Any) -> BasePositionalMultiLegStrategy:
    key = name.lower()
    if key not in _REGISTRY:
        raise KeyError(
            f"Unknown positional strategy {name!r}. Registered: "
            f"{', '.join(sorted(_REGISTRY)) or '(none)'}."
        )
    return _REGISTRY[key](cfg)


def available_positional_strategies() -> list[str]:
    return sorted(_REGISTRY)


@dataclass(frozen=True)
class PositionalContext:
    """Everything a positional strategy's evaluation needs beyond the
    cycle itself — assembled once per evaluation by the engine so every
    candidate/decision in that evaluation shares one consistent snapshot
    (spec section 4.2)."""

    now: datetime
    trading_date: str
    spot: float | None
    spot_is_fresh: bool
    #: Fresh option-chain view for the resolved expiry, or ``None`` if the
    #: chain could not be fetched/parsed this evaluation — the strategy must
    #: treat ``None`` as "block risk-increasing decisions", never guess.
    chain: ChainView | None
    #: security_id -> current Greeks, for every currently open leg, resolved
    #: through the one shared GreeksService this evaluation used. Empty
    #: (never partially populated) when any open leg's Greeks could not be
    #: resolved.
    leg_greeks: dict[str, GreekSnapshot]
    can_enter: bool
    is_holiday: bool
    is_trading_day: bool


class BasePositionalMultiLegStrategy(ABC):
    """Turns one evaluation of the market plus the current cycle state into
    a cycle-level decision.

    Contract:
        * :meth:`evaluate` is called on a regular cadence (every underlying
          tick, or a periodic poll — the engine's choice) and returns a
          :class:`~common.engine.positional.positional_models.CycleSignal`
          to act on, or ``None``.
        * The strategy never mutates ``cycle`` — it only reads it and
          returns a signal; the engine is the sole writer, exactly as
          :class:`~common.engine.multi_leg_strategy.BaseMultiLegStrategy`
          requires of its own subclasses.
        * :meth:`reset_daily` is called once per verified new trading date —
          never at cycle creation, and never touches cycle-level state
          (spec section 9.2: "resets daily counters without resetting
          cycle-level state").
    """

    name: str = "base_positional_multi_leg"

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self.params: dict[str, Any] = dict(getattr(cfg, "parameters", {}) or {})

    @abstractmethod
    def evaluate(self, *, cycle: Cycle | None, context: PositionalContext) -> CycleSignal | None:
        """``cycle`` is ``None`` only when no cycle is currently open — the
        only state from which ``CycleAction.ENTER_CYCLE`` may be returned."""
        ...

    def reset_daily(self) -> None:
        """Called once per verified new trading date. Default no-op — every
        fact a daily reset needs (adjustments_today, day_blocked_reason) is
        already durable on the cycle, updated by the engine itself."""
        return None

    def status(self) -> str:
        return "no status"

    @property
    @abstractmethod
    def lots(self) -> int:
        """Lots per leg for this strategy's entry — resolved from contract
        metadata by the engine, never hardcoded (spec section 3.1)."""
        ...
