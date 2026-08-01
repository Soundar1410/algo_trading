"""The reference repository's own indicator tests, ported (Phase 4 Part 2).

**All 14 of them.** That number is the point of this file's existence as a
separate module, and it is not a large number. The reference has no dedicated
indicator test file — no ``test_indicators.py``, no per-indicator suite, no
``conftest.py`` anywhere in its tree — so these five indicators arrived with
almost no regression coverage to bring across. What is here came from four
different reference files, and every one of them exercises the indicators
*through a consumer* rather than directly:

===================================================  =====  ==========================
Reference file                                       Tests  Covers
===================================================  =====  ==========================
``tests/test_ema_crossover_confirmation.py``             4  ``ConfirmedCrossover``
``tests/test_regime.py`` (classifier block)              6  ADX + ATR via the classifier
``tests/test_warmup.py`` (requirements block)            3  EMA, VWAP declarations
``tests/test_warmup_fail_closed_gate.py`` (1 test)       1  EMA continuity
===================================================  =====  ==========================

Only the import paths changed. Assertions, test names, parametrisation and the
comments explaining *why* each case exists are the reference's own.

**RSI appears nowhere here**, because it appears nowhere in the reference:
nothing there constructs it. Its coverage is entirely in
``test_indicators_behaviour.py`` and ``test_indicator_oracle.py``, both written
in this repository.

Everything else covering these indicators lives in those two files and was
likewise never validated against the reference. Phase 3 Part 2a could claim its
exit policies "pass the reference's own regression suite unmodified, names and
assertion count identical to source". **No equivalent claim is available for the
indicators, and none is made.**
"""

from __future__ import annotations

import pytest

from common.engine.regime import AdxAtrClassifier, NullClassifier, RegimeLabel
from common.indicators import EMA, OHLC, VWAP, SuperTrend
from common.indicators.ema import ConfirmedCrossover
from common.warmup.requirements import IndicatorScope, StrategyWarmupSpec

# ============================================================================
# From tests/test_ema_crossover_confirmation.py
# """Regression coverage for both EMA strategies' shared crossover gate."""
# ============================================================================


def test_july_22_ema_9_21_micro_cross_produces_no_signal() -> None:
    gate = ConfirmedCrossover(minimum_separation=0.25, confirmation_candles=2)

    # 11:45 establishes bearish context. The +0.04 print at 11:50 is inside
    # the deadband and the following bar returns to the same bearish direction.
    assert gate.update(-0.59) is None
    assert gate.update(+0.04) is None
    assert gate.update(-0.56) is None
    assert gate.confirmed_side == -1


def test_july_22_ema_5_9_replay_rejects_one_bar_whipsaws() -> None:
    gate = ConfirmedCrossover(minimum_separation=0.25, confirmation_candles=2)

    # Spreads copied from the 2026-07-22 production log.  The 13:00 bullish
    # print immediately failed at 13:05 and must never become an order.  The
    # later bullish and bearish directions each persist for two bars and do.
    spreads = [
        +2.19,  # seed existing bullish context
        -0.31,
        -3.40,  # confirmed bearish
        -3.34,
        -0.17,
        +1.12,
        -0.73,  # one-bar bullish whipsaw rejected
        -0.85,
        +0.92,
        +1.47,  # confirmed bullish
        -0.17,
        -1.53,
        -2.33,  # confirmed bearish
    ]
    signals = [signal for spread in spreads if (signal := gate.update(spread)) is not None]

    assert signals == [-1, 1, -1]


def test_deadband_cancels_an_incomplete_candidate() -> None:
    gate = ConfirmedCrossover(minimum_separation=0.25, confirmation_candles=2)
    assert gate.update(-1.0) is None
    assert gate.update(+1.0) is None
    assert gate.candidate_count == 1

    assert gate.update(+0.10) is None
    assert gate.candidate_side is None
    assert gate.candidate_count == 0
    assert gate.confirmed_side == -1


@pytest.mark.parametrize(
    ("minimum_separation", "confirmation_candles"),
    [(-0.01, 2), (float("nan"), 2), (0.25, 0)],
)
def test_invalid_confirmation_settings_fail_at_startup(
    minimum_separation: float, confirmation_candles: int
) -> None:
    with pytest.raises(ValueError):
        ConfirmedCrossover(minimum_separation, confirmation_candles)


# ============================================================================
# From tests/test_regime.py — the classifier block (lines 49-117).
# These are the ONLY reference tests that drive ADX and ATR.
# ============================================================================


def _feed(clf, candles):
    for c in candles:
        clf.observe(c)


def test_classifier_unclassified_before_warmup():
    clf = AdxAtrClassifier()
    clf.observe(OHLC(high=101, low=99, close=100))
    assert not clf.is_ready
    assert clf.classify() is RegimeLabel.UNCLASSIFIED


def test_classifier_detects_strong_uptrend():
    clf = AdxAtrClassifier({"atr_avg_window": 10})
    # A steady, low-noise staircase up -> high ADX, +DI > -DI, calm ATR ratio.
    base = 20000.0
    candles = []
    for i in range(40):
        low = base + i * 20
        high = low + 10
        close = high
        candles.append(OHLC(high=high, low=low, close=close, open=low))
    _feed(clf, candles)
    assert clf.is_ready
    assert clf.classify() is RegimeLabel.TRENDING_UP


def test_classifier_detects_strong_downtrend():
    clf = AdxAtrClassifier({"atr_avg_window": 10})
    base = 20000.0
    candles = []
    for i in range(40):
        high = base - i * 20
        low = high - 10
        close = low
        candles.append(OHLC(high=high, low=low, close=close, open=high))
    _feed(clf, candles)
    assert clf.classify() is RegimeLabel.TRENDING_DOWN


def test_classifier_detects_volatile_on_range_expansion():
    clf = AdxAtrClassifier({"atr_avg_window": 20, "vol_high": 1.3})
    # Calm, rangebound candles to establish a low ATR baseline...
    calm = [OHLC(high=20010, low=19990, close=20000, open=20000) for _ in range(30)]
    _feed(clf, calm)
    # ...then a sudden wide-range candle: ATR jumps well above its average.
    clf.observe(OHLC(high=20400, low=19600, close=20000, open=20000))
    assert clf.classify() is RegimeLabel.VOLATILE


def test_classifier_sideways_when_calm_and_directionless():
    clf = AdxAtrClassifier({"atr_avg_window": 10})
    # Oscillating within a tight band -> low ADX, mid vol ratio.
    candles = []
    for i in range(40):
        if i % 2 == 0:
            candles.append(OHLC(high=20015, low=19995, close=20010, open=20000))
        else:
            candles.append(OHLC(high=20005, low=19985, close=19990, open=20000))
    _feed(clf, candles)
    assert clf.classify() in (RegimeLabel.SIDEWAYS, RegimeLabel.LOW_VOLATILITY)


def test_null_classifier_always_unclassified():
    clf = NullClassifier()
    clf.observe(OHLC(high=1, low=0, close=0.5))
    assert clf.is_ready
    assert clf.classify() is RegimeLabel.UNCLASSIFIED


# ============================================================================
# From tests/test_warmup.py — the requirements block (lines 49-71).
# ============================================================================


def test_indicator_declarations():
    assert SuperTrend(period=1).warmup_requirement().scope is IndicatorScope.SESSION_SPANNING
    assert EMA(period=21).warmup_requirement().min_bars == 21
    v = VWAP().warmup_requirement()
    assert v.scope is IndicatorScope.SESSION_LOCAL and v.requires_volume is True


def test_spec_aggregation_session_spanning_only():
    spec = StrategyWarmupSpec.from_indicators([SuperTrend(1, 1), SuperTrend(1, 1)])
    assert not spec.is_empty
    assert spec.has_session_local is False and spec.requires_volume is False


def test_spec_aggregation_flags_session_local_and_takes_max_bars():
    spec = StrategyWarmupSpec.from_indicators([EMA(9), EMA(21), VWAP()])
    assert spec.min_bars == 21  # max over session-spanning indicators
    assert spec.has_session_local is True  # VWAP present
    # requires_volume reflects the *session-spanning* set (EMAs) — False here
    assert spec.requires_volume is False


# ============================================================================
# From tests/test_warmup_fail_closed_gate.py (lines 104-109).
# ============================================================================


def test_converging_indicators_do_not_declare_continuity():
    """EMA/ATR self-correct from a cold start, so they must NOT be gated —
    otherwise ema_cross would be blocked for no reason."""
    from common.indicators.ema import EMA

    assert EMA(9).warmup_requirement().continuity_required is False
