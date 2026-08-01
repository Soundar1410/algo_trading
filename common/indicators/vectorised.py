"""Batch indicator values from ``pandas-ta-classic`` — the oracle, not the path.

Phase 4 Part 2. This module is the architecture document's "pandas-ta-classic
adapter and fixtures" bullet, and it is important to be exact about what that
does and does not mean here.

**No value the engine trades on is produced by this module.** The live
incremental path is the ported reference maths in the sibling modules
(:mod:`~common.indicators.ema`, ``rsi``, ``vwap``, ``atr``, ``adx``,
``supertrend``). This adapter exists for two callers:

1. the cross-check tests, which assert the hand-rolled stateful indicators agree
   with an independent implementation; and
2. Part 4's warm-up replay, which needs a batch form to seed state from history.

Routing live values through the library instead would change numbers the ported
regression tests were written against, which the project rules forbid. That is a
deviation from how the arch-doc bullet might be read, and it is recorded as one.

The restriction is enforced structurally rather than by convention:
``tests/unit/test_indicator_oracle_boundary.py`` asserts that
``pandas_ta_classic`` is imported by this module and by tests, and by nothing
else in the tree. A claim in a docstring is not a mitigation — Phase 3 Part
2b-ii-B-1 found one in ``hub.py`` that had survived review while no code
performed it.

Why an independent implementation is worth having
-------------------------------------------------
The reference repository has **no dedicated indicator test file**, so for three
of the five ported indicators there was no regression test to bring across, and
for RSI there was no coverage of any kind. An agreement test against a different
implementation catches a transcription error, an off-by-one in a smoothing loop,
or a swapped high/low. It cannot catch a formula both implementations share and
both get wrong. That ceiling is real and is stated in the runbook.

Measured agreement, last 150 bars of a 300-bar series
-----------------------------------------------------
=========  ==============  ==========  ==============================================
Indicator  Max rel. diff   Tolerance   Cause of the difference
=========  ==============  ==========  ==============================================
RSI(14)    7.4e-16         1e-12       none — same Wilder formulation, same seed
VWAP       7.5e-16         1e-12       none — same cumulative typical-price x volume
EMA(21)    2.98e-09        1e-8        first-close seed vs pandas-ta's
ATR(14)    2.85e-06        1e-5        first-TR seed vs SMA-of-first-`period` seed
ADX(14)    5.87e-05        1e-4        EWM from first DX vs Wilder's second seed pass
=========  ==============  ==========  ==============================================

Every inexact case is an exponentially-forgetting smoother, so the seeding
difference decays rather than accumulating. That is why the tolerances hold on a
*tail* and why a shorter series legitimately diverges more — the cross-check
pins the fixture length as part of the assertion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

#: Column names every helper here expects on the input frame.
REQUIRED_COLUMNS = ("high", "low", "close")


def _require(df: pd.DataFrame, *columns: str) -> None:
    missing = [name for name in columns if name not in df.columns]
    if missing:
        raise ValueError(f"frame is missing required column(s) {missing}; got {list(df.columns)}")


def ema(df: pd.DataFrame, period: int) -> pd.Series:
    """Batch EMA over ``close``."""
    import pandas_ta_classic as ta

    _require(df, "close")
    return ta.ema(df["close"], length=period)


def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Batch Wilder RSI over ``close``."""
    import pandas_ta_classic as ta

    _require(df, "close")
    return ta.rsi(df["close"], length=period)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Batch Wilder ATR."""
    import pandas_ta_classic as ta

    _require(df, *REQUIRED_COLUMNS)
    return ta.atr(df["high"], df["low"], df["close"], length=period)


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Batch Wilder ADX. Columns: ``ADX_<n>``, ``DMP_<n>``, ``DMN_<n>``."""
    import pandas_ta_classic as ta

    _require(df, *REQUIRED_COLUMNS)
    return ta.adx(df["high"], df["low"], df["close"], length=period)


def vwap(df: pd.DataFrame) -> pd.Series:
    """Batch session-anchored VWAP.

    ``pandas_ta_classic.vwap`` anchors on the frame's :class:`~pandas.DatetimeIndex`
    and resets each day, so the index must be tz-aware and within one session for
    the result to correspond to :class:`common.indicators.vwap.VWAP`, which is
    reset by the engine at the start of each trading day.

    **The timezone warning is suppressed deliberately, and this is why.**
    Internally the library calls ``.to_period("D")`` to find the day boundary,
    which pandas warns "will drop timezone information". Verified rather than
    assumed: ``to_period`` keeps the **local wall date**, so an
    ``Asia/Kolkata``-indexed frame anchors on the IST trading date — including
    for a 23:50 IST bar, which stays on its IST day rather than rolling to the
    UTC one. That is the behaviour this project needs, so the warning is noise
    here. It would **not** be noise for a naive index, which is why the
    ``DatetimeIndex`` check above is a hard error rather than a coercion:
    without a timezone there is no local date to anchor on.
    """
    import warnings

    import pandas as pd
    import pandas_ta_classic as ta

    _require(df, *REQUIRED_COLUMNS, "volume")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError(
            f"vwap needs a DatetimeIndex to anchor the session; got {type(df.index).__name__}"
        )
    if df.index.tz is None:
        raise ValueError(
            "vwap needs a tz-aware index: the session anchor is the local "
            "trading date, and a naive index has no local date to anchor on."
        )
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Converting to PeriodArray/Index representation will drop timezone",
            category=UserWarning,
        )
        return ta.vwap(df["high"], df["low"], df["close"], df["volume"])
