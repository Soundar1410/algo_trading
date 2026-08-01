"""The ported indicators against an independent implementation (Phase 4 Part 2).

**Written here, not ported.** The reference has no dedicated indicator test file,
so for three of the five there was no regression test to bring across and for RSI
no coverage of any kind. This file is what stands in its place: agreement with
``pandas-ta-classic``, a library that computes the same quantities from different
code.

What this catches and what it does not
--------------------------------------
It catches a transcription error, an off-by-one in a smoothing loop, a swapped
high/low, a wrong alpha. It **cannot** catch a formula that both implementations
share and both get wrong — two implementations of the same misunderstanding
agree perfectly. That ceiling is real, and it is why the runbook records RSI as
the least-proven of the five rather than treating a green tick here as
equivalent to the reference coverage the others have.

Tolerances
----------
Every tolerance below is one order of magnitude above the *measured* maximum
relative difference, and each is tied to a named structural cause. None was
chosen by widening until a test passed:

========  ==============  =========  ==============================================
Indicator  Measured        Asserted   Cause
========  ==============  =========  ==============================================
RSI(14)    7.4e-16         1e-12      none — same Wilder formulation and seed
VWAP       7.5e-16         1e-12      none — same cumulative typical-price x volume
EMA(21)    2.98e-09        1e-8       first-close seed vs pandas-ta's
ATR(14)    2.85e-06        1e-5       first-TR seed vs SMA-of-first-`period` seed
ADX(14)    5.87e-05        1e-4       EWM from first DX vs Wilder's second seed pass
========  ==============  =========  ==============================================

Every inexact case is an exponentially-forgetting smoother, so the seeding
difference **decays** rather than accumulating. That is why the comparison is on
a tail rather than the whole series — and why ``BARS`` and ``TAIL`` are asserted
constants rather than incidental ones. A shorter series legitimately diverges
more; a test that quietly shortened the series and kept the tolerance would be
measuring nothing. ``test_the_fixture_is_long_enough_for_the_tolerances_to_mean_anything``
pins that relationship directly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from common.indicators import ADX, ATR, EMA, OHLC, RSI, VWAP
from common.indicators import vectorised as oracle

#: Fixed seed: the fixture must be identical on every run and every machine, or
#: a tolerance measured once means nothing later.
SEED = 7
#: Series length. Part of the assertion — see the module docstring.
BARS = 300
#: How many trailing bars are compared. The leading bars are where the seeding
#: differences live and are deliberately excluded.
TAIL = 150

EMA_PERIOD = 21
WILDER_PERIOD = 14

TOLERANCES = {
    "RSI": 1e-12,
    "VWAP": 1e-12,
    "EMA": 1e-8,
    "ATR": 1e-5,
    "ADX": 1e-4,
}


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    """A deterministic random walk with sane intrabar ranges and volume."""
    rng = np.random.default_rng(SEED)
    close = 20000 + np.cumsum(rng.normal(0, 25, BARS))
    high = close + rng.uniform(2, 30, BARS)
    low = close - rng.uniform(2, 30, BARS)
    index = pd.date_range("2026-08-03 09:15", periods=BARS, freq="1min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": rng.uniform(1000, 5000, BARS),
        },
        index=index,
    )


def _candles(frame: pd.DataFrame) -> list[OHLC]:
    return [
        OHLC(
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            open=float(row.open),
            volume=float(row.volume),
        )
        for row in frame.itertuples()
    ]


def _drive(indicator, frame: pd.DataFrame, attribute: str = "value") -> np.ndarray:
    """Feed the stateful indicator bar by bar and collect its published value."""
    out: list[float] = []
    for candle in _candles(frame):
        state = indicator.update(candle)
        out.append(float(getattr(state, attribute)))
    return np.asarray(out)


def _assert_agrees(name: str, mine: np.ndarray, theirs: np.ndarray) -> None:
    """Compare the trailing ``TAIL`` bars where both produced a value."""
    theirs = np.asarray(theirs, dtype=float)
    usable = ~np.isnan(theirs) & ~np.isnan(mine)
    assert usable.sum() >= TAIL, (
        f"{name}: only {usable.sum()} comparable bars, need at least {TAIL}"
    )
    idx = np.where(usable)[0][-TAIL:]
    rel = np.abs(mine[idx] - theirs[idx]) / np.maximum(np.abs(theirs[idx]), 1e-9)
    worst = float(rel.max())
    assert worst <= TOLERANCES[name], (
        f"{name}: max relative difference {worst:.3e} exceeds the stated "
        f"tolerance {TOLERANCES[name]:.0e}. Either the port changed or the "
        f"tolerance was never justified."
    )


# --------------------------------------------------------------- the exact two
def test_rsi_matches_the_oracle_exactly(frame):
    """Same Wilder formulation, same SMA seed — there is nothing to diverge."""
    _assert_agrees("RSI", _drive(RSI(WILDER_PERIOD), frame), oracle.rsi(frame, WILDER_PERIOD))


def test_vwap_matches_the_oracle_exactly(frame):
    """Both accumulate typical price times volume over one session."""
    _assert_agrees("VWAP", _drive(VWAP(), frame), oracle.vwap(frame))


# ------------------------------------------------------- the three that converge
def test_ema_converges_to_the_oracle(frame):
    _assert_agrees("EMA", _drive(EMA(EMA_PERIOD), frame), oracle.ema(frame, EMA_PERIOD))


def test_atr_converges_to_the_oracle(frame):
    _assert_agrees("ATR", _drive(ATR(WILDER_PERIOD), frame), oracle.atr(frame, WILDER_PERIOD))


def test_adx_converges_to_the_oracle(frame):
    """The reference disclaims TA-Lib parity for this one; measured agreement is
    nonetheless 5.87e-05 on this fixture, because the seeding gap decays."""
    theirs = oracle.adx(frame, WILDER_PERIOD)[f"ADX_{WILDER_PERIOD}"].to_numpy()
    _assert_agrees("ADX", _drive(ADX(WILDER_PERIOD), frame, "adx"), theirs)


# ------------------------------------------------------------- the fixture itself
def test_the_fixture_is_long_enough_for_the_tolerances_to_mean_anything():
    """The tolerances were measured at this length and this tail. Shortening
    either without re-measuring would silently weaken every assertion above,
    because the seeding differences these tolerances absorb decay with bars."""
    assert BARS == 300
    assert TAIL == 150
    assert BARS - TAIL >= 10 * WILDER_PERIOD, (
        "the excluded head must be long enough for Wilder seeding to wash out"
    )


def test_a_short_series_really_does_diverge_more(frame):
    """The negative control for the test above. If the tail length did not
    matter, pinning it would be cargo cult — this shows it does."""
    short = frame.iloc[:40]
    mine = _drive(ATR(WILDER_PERIOD), short)
    theirs = np.asarray(oracle.atr(short, WILDER_PERIOD), dtype=float)
    usable = ~np.isnan(theirs)
    early = np.abs(mine[usable] - theirs[usable]) / np.abs(theirs[usable])
    assert early.max() > TOLERANCES["ATR"], (
        "early bars agreed within the tail tolerance, so the tolerance is not "
        "actually absorbing a seeding difference and should be tightened"
    )


def test_the_oracle_rejects_a_frame_missing_a_column(frame):
    with pytest.raises(ValueError, match="missing required column"):
        oracle.atr(frame.drop(columns=["high"]), WILDER_PERIOD)


def test_the_oracle_vwap_requires_a_datetime_index(frame):
    """It anchors the session on the index; a positional index would silently
    produce a whole-frame cumulative value instead."""
    with pytest.raises(ValueError, match="DatetimeIndex"):
        oracle.vwap(frame.reset_index(drop=True))
