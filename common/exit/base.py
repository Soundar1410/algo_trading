"""Exit engine interface and registry — pluggable, config-selected exits.

The framework separates **entry logic** (a strategy's ``on_candle`` deciding when
to open a position) from **exit logic** (deciding when to close it). Every exit
rule is a self-contained :class:`BaseExit` subclass that answers a single
question on each closed candle::

    exit_engine.should_exit(position, candle, candle_history, indicators, cfg) -> bool

A strategy never knows *how* the exit is implemented — it holds one exit engine
(usually a :class:`~framework.exit.composite.CompositeExit` built from config)
and closes the position whenever ``should_exit`` returns ``True``. New exit rules
are a new file + one :func:`register_exit_engine` decorator, with zero engine or
strategy changes, mirroring the strategy and risk-manager registries.

Contract
--------
* :meth:`should_exit` returns **only** ``True`` / ``False``.
* :meth:`reset` clears any per-position state (highest close, streak counters,
  premium high-water mark); it is called when a fresh position opens and at the
  start of each trading day. Stateless engines (e.g. momentum-close) inherit the
  default no-op.
* :attr:`label` is the engine's config/logging name (e.g. ``"MOMENTUM_CLOSE"``);
  :attr:`reason` is a short human phrase for logs/reports (e.g.
  ``"Momentum Broken"``); :attr:`exit_reason` is the
  :class:`~framework.core.models.ExitReason` recorded on the closed trade.

The ``timestamp`` of the current candle is passed as a keyword so time-based
engines (square-off) can act on it without changing the positional signature the
design doc specifies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from typing import Any

from common.models import ExitReason

# name -> BaseExit subclass
_REGISTRY: dict[str, type[BaseExit]] = {}


def register_exit_engine(name: str) -> Callable[[type[BaseExit]], type[BaseExit]]:
    """Class decorator that registers an exit engine under ``name`` (case-insensitive)."""

    def _wrap(cls: type[BaseExit]) -> type[BaseExit]:
        key = name.lower()
        if key in _REGISTRY and _REGISTRY[key] is not cls:
            raise ValueError(f"Exit engine name {name!r} is already registered.")
        _REGISTRY[key] = cls
        cls.label = name.upper()
        return cls

    return _wrap


def get_exit_engine(name: str, params: dict[str, Any] | None = None) -> BaseExit:
    """Instantiate a registered exit engine by name with its parameter dict."""
    key = name.lower()
    if key not in _REGISTRY:
        raise KeyError(
            f"Unknown exit engine {name!r}. Registered: {', '.join(sorted(_REGISTRY)) or '(none)'}."
        )
    return _REGISTRY[key](params or {})


def available_exit_engines() -> list[str]:
    return sorted(_REGISTRY)


class BaseExit(ABC):
    """One independent, config-selectable exit rule.

    Subclasses read their tuning from the ``params`` dict passed at construction
    (e.g. ``{"trail_points": 20}``) and implement :meth:`should_exit`.
    """

    label: str = "BASE"
    reason: str = "exit condition met"
    exit_reason: ExitReason = ExitReason.STRATEGY_EXIT

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = dict(params or {})

    def reset(self) -> None:  # noqa: B027 - the empty default is the contract
        """Clear per-position state. Default no-op for stateless engines.

        Deliberately **not** ``@abstractmethod``: half the registered engines are
        stateless (momentum-close, momentum-low, fixed-target, time-exit) and
        inherit this no-op. Marking it abstract would force each of them to write
        an empty override — and, worse, would change the ported contract.
        """

    @abstractmethod
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
        """Return True if the open ``position`` should be closed on this candle."""

    def describe(self) -> str:
        return f"{self.label}: {self.reason}"
