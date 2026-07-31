"""Per-position risk manager interface and registry.

Ported from the reference repository's ``framework/risk/base.py`` (Phase 3
Part 2b-i). A strategy's risk policy (initial stop loss, profit lock, trailing
stop, target profit) is pluggable: :class:`RiskManager` is an ABC and concrete
implementations register themselves by name, mirroring the exit registry Part 2a
ported and the strategy registry in :mod:`common.engine.strategy`.

**No concrete risk manager is ported.** The reference's implementations
(``sl_lock_trail``, ``premium_sl`` and friends) are strategy tuning, and the
engine never names one — it only calls the interface. They arrive with Phase 9,
alongside the strategies that select them. Until then the only implementations
are test doubles, which is exactly how the reference's own engine tests worked.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from common.models import ExitReason

# Config values that mean "this rule is switched off" (case-insensitive strings
# plus a bare YAML null). Any of these, in any numeric risk field, disables the
# corresponding rule instead of being parsed as a number.
_DISABLED_TOKENS = {"", "none", "null", "off", "disabled", "na", "-"}


def opt_float(value: Any) -> float | None:
    """Parse a risk threshold that may be switched off.

    Returns ``None`` when ``value`` is a YAML null or one of the textual
    "disabled" tokens (``none`` / ``null`` / ``off`` / ...), so a caller can treat
    a rule as *not configured*. Otherwise returns ``float(value)``.

    Central to the "set it to ``none`` to disable it" convention shared by every
    risk manager, so ``stop_loss``/``profit``/``trailing``/``target`` fields all
    behave identically across strategies.
    """
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in _DISABLED_TOKENS:
        return None
    return float(value)


# name -> RiskManager subclass
_REGISTRY: dict[str, type[RiskManager]] = {}


def register_risk_manager(name: str) -> Callable[[type[RiskManager]], type[RiskManager]]:
    """Class decorator that registers a risk manager under ``name``."""

    def _wrap(cls: type[RiskManager]) -> type[RiskManager]:
        key = name.lower()
        if key in _REGISTRY and _REGISTRY[key] is not cls:
            raise ValueError(f"Risk manager name {name!r} is already registered.")
        _REGISTRY[key] = cls
        cls.name = key
        return cls

    return _wrap


def get_risk_manager(name: str, cfg: Any) -> RiskManager:
    """Instantiate a registered risk manager by name with the given config."""
    key = name.lower()
    if key not in _REGISTRY:
        raise KeyError(
            f"Unknown risk manager {name!r}. "
            f"Registered: {', '.join(sorted(_REGISTRY)) or '(none)'}."
        )
    return _REGISTRY[key](cfg)


def available_risk_managers() -> list[str]:
    return sorted(_REGISTRY)


class RiskManager(ABC):
    """Tracks the exit level(s) for an open position and decides exits.

    Contract:
        * :meth:`reset` clears all state (no open position; called at the start of
          each trading day).
        * :meth:`new_position` arms the manager for a freshly opened position
          (``lots`` scales per-lot thresholds).
        * :meth:`on_pnl` is fed the latest unrealised P&L (rupees) on every tick
          and returns an :class:`~common.models.ExitReason` if the position should
          be closed now, else ``None``.
        * :attr:`state` exposes the current risk picture for logging.
    """

    name: str = "base"

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg

    @abstractmethod
    def reset(self) -> None: ...

    @abstractmethod
    def new_position(self, lots: int = 1) -> None: ...

    @abstractmethod
    def on_pnl(self, pnl: float) -> ExitReason | None: ...

    @property
    @abstractmethod
    def state(self) -> Any: ...
