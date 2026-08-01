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

from collections.abc import Iterable

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
    "reset_session_local",
    "supertrend",
]


def reset_session_local(indicators: Iterable[StatefulIndicator]) -> list[StatefulIndicator]:
    """Reset every session-cumulative indicator; leave the rest untouched.

    The continuity rule from Phase 4 Part 3, as code rather than as prose in a
    docstring. Call it from
    :meth:`common.engine.strategy.BaseStrategy.on_candle_gap`.

    A hole in the tick stream affects the two indicator scopes differently, and
    the difference is not a matter of taste:

    * ``SESSION_LOCAL`` (VWAP) accumulates over the day, so missing volume is
      never recovered — every later value stays wrong by whatever the hole
      swallowed. Resetting costs the morning's context and buys correctness for
      the rest of the day, which is the better trade.
    * ``SESSION_SPANNING`` (EMA, RSI, ATR, ADX, SuperTrend) are exponentially
      forgetting: a missing bar decays out of them by itself. Resetting one
      would discard far more history than the hole did.

    Scope comes from each indicator's own ``warmup_requirement()``, so an
    indicator added later is classified by what it declares rather than by a list
    kept in step by hand. An indicator whose ``warmup_requirement()`` raises is
    treated as session-local and reset — the conservative direction, since the
    alternative is carrying state that may be corrupt.

    Returns the indicators it reset, so a caller can log or count them.
    """
    from common.warmup.requirements import IndicatorScope

    was_reset: list[StatefulIndicator] = []
    for indicator in indicators:
        try:
            scope = indicator.warmup_requirement().scope
        except Exception:
            scope = IndicatorScope.SESSION_LOCAL
        if scope is IndicatorScope.SESSION_LOCAL:
            indicator.reset()
            was_reset.append(indicator)
    return was_reset
