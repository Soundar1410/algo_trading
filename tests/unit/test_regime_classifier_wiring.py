"""The `adx_atr` classifier's wiring, and D21's closure (Phase 4 Part 2).

``test_indicators_ported.py`` carries the reference's own 6 classifier tests,
which cover what it *decides*. This file covers what the reference had no reason
to test and this repository does: that the classifier is reachable at all, and
that closing D21 did not switch anything on.

Written because the claims were otherwise only in a docstring. Phase 3 Part
2b-ii-B-1 found exactly that shape in ``hub.py`` — an entry-block asserted as
fact in prose while no code performed it — and it survived review. "Available but
not enabled" is a claim of the same kind, so it is a test here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from common.engine.regime import (
    AdxAtrClassifier,
    NullClassifier,
    RegimeLabel,
    available_regime_classifiers,
    build_regime_tagger,
    get_regime_classifier,
)
from common.indicators import OHLC

REPO_ROOT = Path(__file__).resolve().parents[2]


class _Cfg:
    """The subset of EngineConfig that build_regime_tagger reads."""

    def __init__(self, *, enabled: bool = False, parameters: dict | None = None) -> None:
        self.regime_enabled = enabled
        self.parameters = parameters or {}


def _trending_candles(n: int = 40) -> list[OHLC]:
    return [
        OHLC(high=20000 + i * 20 + 10, low=20000 + i * 20, close=20000 + i * 20 + 10)
        for i in range(n)
    ]


# ------------------------------------------------------------------- registry
def test_the_classifier_is_registered():
    """D21's substance: before Part 2 only `null` existed."""
    assert "adx_atr" in available_regime_classifiers()
    assert available_regime_classifiers() == ["adx_atr", "null"]


def test_registration_needs_no_second_import():
    """D21 predicted 'a new file plus one decorator'. A decorator in its own
    module registers only if something imports that module, so the class lives
    in `regime.py` instead. This is that decision, asserted."""
    code = (
        "from common.engine.regime import available_regime_classifiers\n"
        "print(','.join(available_regime_classifiers()))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT
    )
    assert result.returncode == 0, result.stderr
    assert "adx_atr" in result.stdout.strip().split(","), (
        "importing common.engine.regime alone did not register adx_atr, so a "
        "caller could resolve 'adx_atr' only by knowing to import elsewhere"
    )


def test_resolving_it_by_name_gives_the_right_class():
    assert isinstance(get_regime_classifier("adx_atr"), AdxAtrClassifier)


def test_an_unknown_classifier_name_still_raises():
    with pytest.raises(KeyError, match="Unknown regime classifier"):
        get_regime_classifier("macd_thing")


# ---------------------------------------------------------------- not enabled
def test_the_default_is_still_the_null_classifier():
    """Closing D21 made adx_atr *available*. It must not have made it active:
    the regime axis is observational, and turning it on is a config decision."""
    tagger = build_regime_tagger(_Cfg(enabled=False))
    assert isinstance(tagger._classifier, NullClassifier)


def test_enabling_regime_without_naming_one_still_gets_null():
    """`regime_classifier` defaults to 'null' even when the axis is enabled."""
    tagger = build_regime_tagger(_Cfg(enabled=True))
    assert isinstance(tagger._classifier, NullClassifier)


def test_asking_for_it_by_config_gets_it():
    tagger = build_regime_tagger(_Cfg(enabled=True, parameters={"regime_classifier": "adx_atr"}))
    assert isinstance(tagger._classifier, AdxAtrClassifier)


def test_a_disabled_axis_ignores_a_named_classifier():
    """`enabled: false` wins over `regime_classifier: adx_atr` — otherwise the
    flag that is supposed to switch the axis off would not."""
    tagger = build_regime_tagger(_Cfg(enabled=False, parameters={"regime_classifier": "adx_atr"}))
    assert isinstance(tagger._classifier, NullClassifier)


# ------------------------------------------------------------------ behaviour
def test_diagnostics_are_empty_until_it_is_ready():
    clf = AdxAtrClassifier()
    clf.observe(OHLC(high=101, low=99, close=100))
    assert clf.diagnostics() == {}


def test_diagnostics_and_classify_read_the_same_snapshot():
    """`_snapshot` exists so the label and the numbers behind it cannot drift.
    A label of TRENDING_UP with diagnostics showing adx below the threshold
    would make every recalibration from stored trades wrong."""
    clf = AdxAtrClassifier({"atr_avg_window": 10})
    for candle in _trending_candles():
        clf.observe(candle)

    label = clf.classify()
    diagnostics = clf.diagnostics()
    assert label is RegimeLabel.TRENDING_UP
    assert diagnostics["adx"] >= 25.0, "the label says trending; the diagnostics must agree"
    assert diagnostics["plus_di"] >= diagnostics["minus_di"]


def test_reset_clears_the_classifier_between_days():
    clf = AdxAtrClassifier({"atr_avg_window": 10})
    for candle in _trending_candles():
        clf.observe(candle)
    assert clf.is_ready

    clf.reset()
    assert not clf.is_ready
    assert clf.classify() is RegimeLabel.UNCLASSIFIED


def test_volatile_first_changes_which_label_wins():
    """Both conditions can hold at once — a violent trend is trending *and*
    volatile. `volatile_first` is what decides, so it must actually decide."""
    params = {"atr_avg_window": 10, "vol_high": 1.05, "adx_trend_min": 10.0}
    first = AdxAtrClassifier({**params, "volatile_first": True})
    second = AdxAtrClassifier({**params, "volatile_first": False})

    # A trend that also expands its range: satisfies both branches.
    candles = [
        OHLC(high=20000 + i * 50 + i * 5, low=20000 + i * 50, close=20000 + i * 50 + i * 5)
        for i in range(40)
    ]
    for candle in candles:
        first.observe(candle)
        second.observe(candle)

    assert first.classify() is RegimeLabel.VOLATILE
    assert second.classify() in (RegimeLabel.TRENDING_UP, RegimeLabel.TRENDING_DOWN)


def test_the_classifier_reuses_the_ported_indicators():
    """It must introduce no new indicator maths — that is why porting ADX and
    ATR was the precondition for closing D21."""
    from common.indicators import ADX, ATR

    clf = AdxAtrClassifier()
    assert isinstance(clf._adx, ADX)
    assert isinstance(clf._atr, ATR)
