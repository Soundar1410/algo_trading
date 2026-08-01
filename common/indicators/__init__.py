"""Indicators ported from the reference repository.

Phase 3 Part 2a brought across only what the exit registry needed: the
:class:`OHLC` candle record every exit engine reads, and SuperTrend, which
``supertrend_exit`` consults. It recorded that the reference's other five —
EMA, RSI, VWAP, ATR and ADX — were deliberately left out because nothing
consumed them and Phase 4 owned the indicator layer.

**Phase 4 Part 2 is that layer.** All five are now here, each a
:class:`~common.indicators.base.StatefulIndicator` with a ``<Name>``/
``<Name>State`` pair, matching the SuperTrend port's shape.

What the port could and could not bring with it
-----------------------------------------------
The reference has **no dedicated indicator test file** — no
``test_indicators.py``, no per-indicator suite, no ``conftest.py`` anywhere in
its tree. Only 14 reference tests were portable, and they arrive via the two
consumers that exercise these indicators rather than via the indicators
themselves:

* :class:`~common.engine.regime.AdxAtrClassifier`, ported in the same part, is
  what gives ADX and ATR a consumer and 6 regression tests. Phase 3's D21 left
  it out precisely because these two indicators did not exist yet.
* :class:`ConfirmedCrossover` ships inside ``ema.py`` and carries 4 tests that
  replay real production spreads. It has no consumer here until Phase 9.

**RSI arrived with no reference coverage at all** — nothing in the reference
constructs it. See :mod:`common.indicators.rsi`.

Everything else covering these indicators was written in this repository and was
never validated against the reference's own behaviour. No claim of the form
Part 2a could make — "passes the reference's own regression suite unmodified" —
is available here, and none is made.

:mod:`common.indicators.vectorised` wraps ``pandas-ta-classic`` as an
independent cross-check and as Part 4's warm-up replay source. It is **never on
the live incremental path**, and a test enforces that.
"""

from __future__ import annotations

from .adx import ADX, ADXState
from .atr import ATR, ATRState
from .base import OHLC, StatefulIndicator
from .ema import EMA, ConfirmedCrossover, EMAState
from .rsi import RSI, RSIState
from .supertrend import DOWNTREND, UPTREND, SuperTrend, SuperTrendState, supertrend
from .vwap import VWAP, VWAPState

__all__ = [
    "ADX",
    "ATR",
    "DOWNTREND",
    "EMA",
    "OHLC",
    "RSI",
    "UPTREND",
    "VWAP",
    "ADXState",
    "ATRState",
    "ConfirmedCrossover",
    "EMAState",
    "RSIState",
    "StatefulIndicator",
    "SuperTrend",
    "SuperTrendState",
    "VWAPState",
    "supertrend",
]
