"""Pluggable, config-selected exit engines.

Importing this package registers every built-in exit engine (each module calls
:func:`register_exit_engine` at import time), so
:func:`~framework.exit.base.get_exit_engine` and
:func:`~framework.exit.composite.build_exit_engine` can resolve them by name.

Public API:
    BaseExit               — the exit-engine interface (returns True/False)
    register_exit_engine   — decorator to add a new engine
    get_exit_engine        — instantiate one engine by name
    available_exit_engines — list registered names
    CompositeExit          — OR several engines together
    build_exit_engine      — build a CompositeExit from an ExitConfig
"""

from __future__ import annotations

# Import each engine module for its registration side effect. The reference kept
# these below the `.base` import and silenced E402; isort sorts them above it
# instead, which is safe because every engine module imports `.base` itself, so
# the registry exists before any `@register_exit_engine` runs either way. Do not
# "fix" the order back — it is import-sorted, not accidental.
from . import (  # noqa: F401
    combined_candle_exit,
    consecutive_reversal,
    fixed_target,
    highest_close,
    momentum_close,
    momentum_low,
    stoploss_exit,
    supertrend_exit,
    time_exit,
    trailing_exit,
)
from .base import BaseExit, available_exit_engines, get_exit_engine, register_exit_engine
from .composite import CompositeExit, build_exit_engine

__all__ = [
    "BaseExit",
    "CompositeExit",
    "available_exit_engines",
    "build_exit_engine",
    "get_exit_engine",
    "register_exit_engine",
]
