"""Per-indicator behaviour (Phase 4 Part 2). **Written here, not ported.**

Everything in this file was written in this repository and was never validated
against the reference's own behaviour — the reference has no dedicated indicator
test file. See ``test_indicators_ported.py`` for the 14 that did come across.

This file covers what the ported 14 cannot: construction validation, the
``reset()`` contract, ``is_ready`` transitions, the documented ``RuntimeError``
before a first value exists, and each indicator's own edge. Numerical agreement
with an independent implementation is ``test_indicator_oracle.py``'s job.
"""

from __future__ import annotations

import pytest

from common.indicators import ADX, ATR, EMA, OHLC, RSI, VWAP


def _c(close: float, high: float | None = None, low: float | None = None, volume: float = 0.0):
    """One candle. Defaults make a flat bar at ``close``."""
    return OHLC(
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        open=close,
        volume=volume,
    )


# --------------------------------------------------------------- construction
@pytest.mark.parametrize("cls", [EMA, RSI, ATR, ADX])
@pytest.mark.parametrize("period", [0, -1])
def test_a_non_positive_period_is_refused(cls, period):
    with pytest.raises(ValueError, match="period must be >= 1"):
        cls(period)


def test_vwap_takes_no_period():
    """It is session-cumulative — there is no window to configure."""
    VWAP()  # must not raise


# ------------------------------------------------------- before a first value
@pytest.mark.parametrize(
    ("indicator", "match"),
    [
        (EMA(5), "EMA has no value yet"),
        (RSI(5), "RSI has no value yet"),
        (ATR(5), "ATR has no value yet"),
        (ADX(5), "ADX has no value yet"),
        (VWAP(), "VWAP has no volume yet"),
    ],
)
def test_state_raises_before_the_indicator_has_one(indicator, match):
    """A caller reaching for `.state` has asserted readiness; give it a real
    error rather than a plausible-looking zero."""
    assert indicator.is_ready is False
    with pytest.raises(RuntimeError, match=match):
        _ = indicator.state


# -------------------------------------------------------------------- is_ready
def test_ema_is_ready_only_after_period_candles():
    ema = EMA(3)
    for expected in (False, False, True):
        ema.update(_c(100))
        assert ema.is_ready is expected


def test_rsi_is_ready_only_once_the_seed_window_is_full():
    """The first candle only establishes prev_close, so readiness needs
    ``period`` *changes*, i.e. period + 1 candles."""
    rsi = RSI(3)
    for close in (100, 101, 102):
        rsi.update(_c(close))
        assert rsi.is_ready is False
    rsi.update(_c(103))
    assert rsi.is_ready is True


def test_atr_is_ready_on_its_very_first_candle():
    """It seeds `_atr = tr`, so unlike RSI there is no warm-up window."""
    atr = ATR(14)
    atr.update(_c(100, high=105, low=95))
    assert atr.is_ready is True


def test_vwap_is_not_ready_until_volume_arrives():
    vwap = VWAP()
    vwap.update(_c(100, volume=0.0))
    assert vwap.is_ready is False
    vwap.update(_c(100, volume=1.0))
    assert vwap.is_ready is True


# ----------------------------------------------------------------------- reset
@pytest.mark.parametrize(
    "indicator",
    [EMA(3), RSI(3), ATR(3), ADX(3), VWAP()],
    ids=["EMA", "RSI", "ATR", "ADX", "VWAP"],
)
def test_reset_returns_an_indicator_to_its_unready_state(indicator):
    """Called at the start of each trading day — a leak across the boundary
    would seed today's signals with yesterday's prices."""
    for close in range(100, 120):
        indicator.update(_c(close, high=close + 2, low=close - 2, volume=100.0))
    assert indicator.is_ready is True

    indicator.reset()
    assert indicator.is_ready is False
    with pytest.raises(RuntimeError):
        _ = indicator.state


def test_reset_gives_a_reused_indicator_the_same_values_as_a_fresh_one():
    """Stronger than `is_ready is False`: proves no residue survives at all."""
    series = [100, 104, 102, 108, 107, 111, 109, 115]
    reused = EMA(3)
    for close in (500, 400, 300):  # unrelated history
        reused.update(_c(close))
    reused.reset()

    fresh = EMA(3)
    for close in series:
        reused.update(_c(close))
        fresh.update(_c(close))
    assert reused.state.value == fresh.state.value


# ------------------------------------------------------------------- EMA edges
def test_a_flat_series_leaves_the_ema_at_that_price():
    ema = EMA(5)
    for _ in range(30):
        ema.update(_c(250.0))
    assert ema.state.value == pytest.approx(250.0)


def test_the_ema_seeds_on_the_first_close():
    """The documented seeding choice, and the reason the oracle cross-check for
    EMA is not exact."""
    ema = EMA(10)
    ema.update(_c(123.75))
    assert ema.state.value == pytest.approx(123.75)


# ------------------------------------------------------------------- RSI edges
def test_an_unbroken_rally_pins_rsi_at_100():
    """Zero average loss: the formula divides by it, so this is the branch that
    must return 100 rather than raise ZeroDivisionError."""
    rsi = RSI(5)
    for close in range(100, 130):
        rsi.update(_c(float(close)))
    assert rsi.state.value == 100.0


def test_an_unbroken_selloff_drives_rsi_to_zero():
    rsi = RSI(5)
    for close in range(130, 100, -1):
        rsi.update(_c(float(close)))
    assert rsi.state.value == pytest.approx(0.0)


def test_rsi_reports_a_neutral_50_before_it_is_ready():
    """`update` returns 50 while warming; `state` raises. The asymmetry is the
    reference's and is deliberate — see the module docstring."""
    rsi = RSI(5)
    assert rsi.update(_c(100)).value == 50.0
    assert rsi.update(_c(105)).value == 50.0
    assert rsi.is_ready is False


def test_a_flat_series_gives_rsi_no_gains_and_no_losses():
    """Both averages are zero, so the zero-loss branch wins and reports 100.
    Worth pinning: it is a defensible convention rather than an obvious one."""
    rsi = RSI(3)
    for _ in range(10):
        rsi.update(_c(100.0))
    assert rsi.state.value == 100.0


# ------------------------------------------------------------------- ATR edges
def test_the_first_true_range_is_simply_high_minus_low():
    """No previous close exists, so the two gap terms are undefined."""
    atr = ATR(14)
    assert atr.update(_c(100, high=110, low=90)).value == pytest.approx(20.0)


def test_a_gap_up_counts_against_the_previous_close_not_the_bar():
    """True range's whole purpose: a 5-point bar that gapped 50 points is a
    50-point move, and an ATR that saw only the bar would under-state risk.

    Asserted against both candidates rather than against a threshold, so the
    test says which implementation it is rejecting:

        gap-aware TR = max(152-147, |152-100|, |147-100|) = 52
        bar-only  TR = 152-147                            =  5

    Both are then smoothed once from a seed of 0 (the flat first bar), so the
    expected values are 52/14 and 5/14.
    """
    atr = ATR(14)
    atr.update(_c(100, high=100, low=100))
    state = atr.update(_c(150, high=152, low=147))

    assert state.value == pytest.approx(52 / 14)
    assert state.value != pytest.approx(5 / 14), "true range ignored the gap"


def test_atr_of_a_perfectly_flat_market_is_zero():
    atr = ATR(5)
    for _ in range(20):
        atr.update(_c(100.0))
    assert atr.state.value == pytest.approx(0.0)


# ------------------------------------------------------------------- ADX edges
def test_a_clean_uptrend_puts_plus_di_above_minus_di():
    adx = ADX(14)
    for i in range(40):
        low = 20000 + i * 20
        adx.update(_c(low + 10, high=low + 10, low=low))
    state = adx.state
    assert state.plus_di > state.minus_di
    assert state.adx > 0


def test_a_clean_downtrend_puts_minus_di_above_plus_di():
    adx = ADX(14)
    for i in range(40):
        high = 20000 - i * 20
        adx.update(_c(high - 10, high=high, low=high - 10))
    state = adx.state
    assert state.minus_di > state.plus_di


def test_adx_state_agrees_with_what_update_returned():
    """`update` builds a state inline and `state` recomputes it. Two code paths
    for one value is exactly how they drift apart."""
    adx = ADX(7)
    returned = None
    for i in range(30):
        low = 20000 + i * 13
        returned = adx.update(_c(low + 6, high=low + 9, low=low))
    assert returned is not None
    assert returned.adx == pytest.approx(adx.state.adx)
    assert returned.plus_di == pytest.approx(adx.state.plus_di)
    assert returned.minus_di == pytest.approx(adx.state.minus_di)


def test_adx_counts_the_candles_it_has_seen():
    adx = ADX(5)
    for _ in range(7):
        adx.update(_c(100, high=101, low=99))
    assert adx.count == 7


# ------------------------------------------------------------------ VWAP edges
def test_vwap_is_the_volume_weighted_typical_price():
    """Hand-computed, so a refactor that changed the weighting would fail here
    rather than merely disagreeing with the oracle."""
    vwap = VWAP()
    vwap.update(_c(100, high=110, low=90, volume=100.0))  # typical 100
    vwap.update(_c(200, high=210, low=190, volume=300.0))  # typical 200
    # (100*100 + 200*300) / 400 = 175
    assert vwap.state.value == pytest.approx(175.0)


def test_a_zero_volume_candle_moves_vwap_not_at_all():
    vwap = VWAP()
    vwap.update(_c(100, high=100, low=100, volume=50.0))
    before = vwap.state.value
    vwap.update(_c(999, high=999, low=999, volume=0.0))
    assert vwap.state.value == pytest.approx(before)


def test_vwap_treats_a_missing_volume_as_zero():
    """`OHLC.volume` is optional, and an option leg's tape may not carry it."""
    vwap = VWAP()
    vwap.update(OHLC(high=100, low=100, close=100, volume=None))
    assert vwap.is_ready is False


def test_resetting_vwap_starts_a_new_session_cleanly():
    """The one indicator where carrying state across a day is corruption rather
    than merely a stale seed — which is why it declares SESSION_LOCAL."""
    vwap = VWAP()
    vwap.update(_c(100, volume=1000.0))
    vwap.reset()
    vwap.update(_c(200, volume=1.0))
    assert vwap.state.value == pytest.approx(200.0)
