"""Indicators ported from the reference repository (Phase 3 Part 2a).

Only what the exit registry needs: the :class:`OHLC` candle record every exit
engine reads, and SuperTrend, which ``supertrend_exit`` consults and the ported
``test_exit_engines.py`` drives directly.

The reference package also ships EMA, RSI, VWAP, ATR and ADX. They are **not**
ported here: nothing in this repository consumes them yet, and Phase 4 owns the
indicator layer proper (including the `pandas-ta-classic` adapter this project
already depends on). Porting five more indicators now would add ~440 lines with
no caller and no test.
"""

from __future__ import annotations

from .base import OHLC, StatefulIndicator
from .supertrend import DOWNTREND, UPTREND, SuperTrend, SuperTrendState, supertrend

__all__ = [
    "DOWNTREND",
    "OHLC",
    "UPTREND",
    "StatefulIndicator",
    "SuperTrend",
    "SuperTrendState",
    "supertrend",
]
