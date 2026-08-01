"""Market Regime Engine — the observational tagger the engine holds.

Ported from the reference repository's ``framework/regime/``. Phase 3 Part 2b-i
brought the registry, the :class:`RegimeClassifier` contract and the null
classifier; **Phase 4 Part 2 adds ``adx_atr``, closing deviation D21.**

D21 existed because the one real classifier is built on ADX and ATR, and Part 2a
deliberately did not port those two indicators — nothing consumed them and Phase
4 owned the indicator layer. Part 2 ports them, so the classifier's inputs now
exist and the deviation has nothing left to stand on. It is also what gives ADX
and ATR a consumer and their only 6 reference regression tests; without it they
would be exactly the "untested code that merely looks finished" Part 2a refused
to create.

D21 predicted this would be "a new file plus one decorator, with no change
here", and the file half of that turned out to be wrong. The decorator registers
at **import time**, so a classifier in its own module is registered only if
something imports that module — a classifier that silently does not exist is a
worse failure than a slightly longer file. This repository had already collapsed
the reference's five regime modules into one; ``AdxAtrClassifier`` joins them,
and registration is then unconditional.

The regime axis remains *purely observational* — it tags trades and changes no
trading decision — and ``regime_enabled`` still defaults to false, so this ships
available and not switched on.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any

from common.indicators.adx import ADX, ADXState
from common.indicators.atr import ATR
from common.indicators.base import OHLC

from .models import OptionContract


class RegimeLabel(StrEnum):
    """The single, mutually-exclusive market-regime axis stored per trade."""

    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    SIDEWAYS = "SIDEWAYS"
    VOLATILE = "VOLATILE"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    # First-class "classifier not warmed up yet" bucket (early-session trades):
    # a missing signal is a labelled state, never an exception.
    UNCLASSIFIED = "UNCLASSIFIED"


class SessionTag(StrEnum):
    """Orthogonal session flags, stored alongside (not inside) the regime axis.

    Kept separate from :class:`RegimeLabel` on purpose: an expiry day can *also*
    be trending, so folding it into the regime enum would lose that combination.
    """

    EXPIRY_DAY = "EXPIRY_DAY"
    GAP_DAY = "GAP_DAY"


# name -> RegimeClassifier subclass
_REGISTRY: dict[str, type[RegimeClassifier]] = {}


def register_regime_classifier(
    name: str,
) -> Callable[[type[RegimeClassifier]], type[RegimeClassifier]]:
    """Class decorator that registers a regime classifier under ``name``."""

    def _wrap(cls: type[RegimeClassifier]) -> type[RegimeClassifier]:
        key = name.lower()
        if key in _REGISTRY and _REGISTRY[key] is not cls:
            raise ValueError(f"Regime classifier name {name!r} is already registered.")
        _REGISTRY[key] = cls
        cls.name = key
        return cls

    return _wrap


def get_regime_classifier(name: str, params: dict[str, Any] | None = None) -> RegimeClassifier:
    """Instantiate a registered regime classifier by name with its own params."""
    key = name.lower()
    if key not in _REGISTRY:
        raise KeyError(
            f"Unknown regime classifier {name!r}. "
            f"Registered: {', '.join(sorted(_REGISTRY)) or '(none)'}."
        )
    return _REGISTRY[key](params or {})


def available_regime_classifiers() -> list[str]:
    return sorted(_REGISTRY)


class RegimeClassifier(ABC):
    """Turns a stream of *underlying* candles into one :class:`RegimeLabel`."""

    name: str = "base"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = dict(params or {})

    @abstractmethod
    def reset(self) -> None: ...

    @abstractmethod
    def observe(self, candle: OHLC) -> None: ...

    @property
    @abstractmethod
    def is_ready(self) -> bool: ...

    @abstractmethod
    def classify(self) -> RegimeLabel: ...

    def diagnostics(self) -> dict[str, float]:
        """Raw values behind the current classification, for later recalibration."""
        return {}


@register_regime_classifier("null")
class NullClassifier(RegimeClassifier):
    """Always-``UNCLASSIFIED`` classifier (regime detection switched off)."""

    def reset(self) -> None:
        return None

    def observe(self, candle: OHLC) -> None:
        return None

    @property
    def is_ready(self) -> bool:
        return True

    def classify(self) -> RegimeLabel:
        return RegimeLabel.UNCLASSIFIED


@register_regime_classifier("adx_atr")
class AdxAtrClassifier(RegimeClassifier):
    """ADX (trend strength) + ATR (volatility) — the reference's default.

    Reuses the ported stateful indicators unmodified
    (:class:`~common.indicators.adx.ADX`, :class:`~common.indicators.atr.ATR`),
    so it introduces no new indicator maths. Volatility is expressed as a
    **ratio** of the current ATR to its own rolling average (``vol_ratio``),
    which keeps the thresholds instrument- and price-level-independent — there
    are no NIFTY-specific absolute numbers to recalibrate when the index level
    drifts.

    Decision tree (first match wins; ``volatile_first`` controls step 2 vs 3):

        1. not warmed up                -> UNCLASSIFIED
        2. vol_ratio >= vol_high        -> VOLATILE
        3. adx >= adx_trend_min         -> TRENDING_UP / TRENDING_DOWN (+DI vs -DI)
        4. vol_ratio <= vol_low         -> LOW_VOLATILITY
        5. otherwise                    -> SIDEWAYS

    Every threshold is config-driven, so recalibration once real trades
    accumulate is an edit, not a code change.
    """

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        p = self.params
        self._adx_period = int(p.get("adx_period", 14))
        self._atr_period = int(p.get("atr_period", 14))
        self._atr_avg_window = int(p.get("atr_avg_window", 20))
        self._adx_trend_min = float(p.get("adx_trend_min", 25.0))
        self._vol_high = float(p.get("vol_high", 1.30))
        self._vol_low = float(p.get("vol_low", 0.70))
        self._volatile_first = bool(p.get("volatile_first", True))
        # Enough ATR samples to make the average meaningful before we classify
        # volatility off it (at least half the averaging window, min 5).
        self._min_atr_samples = max(5, self._atr_avg_window // 2)
        self.reset()

    def reset(self) -> None:
        self._adx = ADX(self._adx_period)
        self._atr = ATR(self._atr_period)
        self._atr_hist: deque[float] = deque(maxlen=self._atr_avg_window)

    def observe(self, candle: OHLC) -> None:
        self._adx.update(candle)
        state = self._atr.update(candle)
        self._atr_hist.append(state.value)

    @property
    def is_ready(self) -> bool:
        return self._adx.is_ready and len(self._atr_hist) >= self._min_atr_samples

    def _snapshot(self) -> tuple[float, ADXState]:
        """Current ``(vol_ratio, adx_state)`` — shared by :meth:`classify` and
        :meth:`diagnostics` so the two never drift apart. Only called when
        :attr:`is_ready`."""
        atr = self._atr.state.value
        avg = sum(self._atr_hist) / len(self._atr_hist) if self._atr_hist else atr
        vol_ratio = atr / avg if avg > 0 else 1.0
        return vol_ratio, self._adx.state

    def classify(self) -> RegimeLabel:
        if not self.is_ready:
            return RegimeLabel.UNCLASSIFIED

        vol_ratio, adx_state = self._snapshot()
        trending = adx_state.adx >= self._adx_trend_min

        def _trend_label() -> RegimeLabel:
            return (
                RegimeLabel.TRENDING_UP
                if adx_state.plus_di >= adx_state.minus_di
                else RegimeLabel.TRENDING_DOWN
            )

        if self._volatile_first:
            if vol_ratio >= self._vol_high:
                return RegimeLabel.VOLATILE
            if trending:
                return _trend_label()
        else:
            if trending:
                return _trend_label()
            if vol_ratio >= self._vol_high:
                return RegimeLabel.VOLATILE

        if vol_ratio <= self._vol_low:
            return RegimeLabel.LOW_VOLATILITY
        return RegimeLabel.SIDEWAYS

    def diagnostics(self) -> dict[str, float]:
        """Raw values behind the last :meth:`classify` call, for recomputing the
        label later under different thresholds (see the base-class docstring)."""
        if not self.is_ready:
            return {}
        vol_ratio, adx_state = self._snapshot()
        return {
            "adx": round(adx_state.adx, 4),
            "plus_di": round(adx_state.plus_di, 4),
            "minus_di": round(adx_state.minus_di, 4),
            "vol_ratio": round(vol_ratio, 4),
        }


class RegimeTagger:
    """Holds one classifier + derives session tags. One instance per engine."""

    def __init__(self, classifier: RegimeClassifier, *, gap_pct: float = 0.007) -> None:
        self._classifier = classifier
        self._gap_pct = gap_pct

    def reset(self) -> None:
        """Clear classifier state — called at the start of each trading day."""
        self._classifier.reset()

    def observe(self, candle: OHLC) -> None:
        """Incorporate one closed *underlying* candle into the classifier."""
        self._classifier.observe(candle)

    def current_regime(self) -> str:
        """The current :class:`RegimeLabel` value as a plain string."""
        return str(self._classifier.classify().value)

    def current_features(self) -> str:
        """JSON-encoded raw diagnostic values behind the current classification,
        for recomputing the label later if thresholds are recalibrated. ``"{}"``
        when the classifier exposes none or is not ready yet."""
        return json.dumps(self._classifier.diagnostics())

    def session_tags(
        self,
        contract: OptionContract | None,
        ts: datetime,
        *,
        today_open: float | None = None,
        prev_close: float | None = None,
    ) -> str:
        """Comma-joined orthogonal session tags for a trade (``""`` when none).

        ``EXPIRY_DAY`` is derived exactly from the traded contract's expiry vs the
        trade date (no holiday calendar needed). ``GAP_DAY`` needs the day's open
        and previous close; engines that don't track those (the single-leg engine
        does not) pass ``None`` and that tag is skipped — graceful degradation,
        never an error.
        """
        tags: list[str] = []
        if self._is_expiry_day(contract, ts):
            tags.append(SessionTag.EXPIRY_DAY.value)
        if self._is_gap_day(today_open, prev_close):
            tags.append(SessionTag.GAP_DAY.value)
        return ",".join(tags)

    @staticmethod
    def _is_expiry_day(contract: OptionContract | None, ts: datetime) -> bool:
        if contract is None or not contract.expiry:
            return False
        # contract.expiry is an ISO date or broker code; compare the leading
        # YYYY-MM-DD against the trade date. A non-ISO code simply won't match
        # (tag skipped) rather than raising.
        return str(contract.expiry)[:10] == ts.date().isoformat()

    def _is_gap_day(self, today_open: float | None, prev_close: float | None) -> bool:
        if not today_open or not prev_close or prev_close <= 0:
            return False
        return abs(today_open - prev_close) / prev_close >= self._gap_pct


def build_regime_tagger(cfg: Any) -> RegimeTagger:
    """Build a :class:`RegimeTagger` from an :class:`~common.engine.config.
    EngineConfig`.

    Regime disabled (the default) -> a :class:`NullClassifier` tagger, so trades
    are tagged ``UNCLASSIFIED`` with no behavioural change. Enabled -> the
    configured classifier; with only ``null`` registered in this phase (D21) that
    resolves to the same thing unless a caller has registered its own.
    """
    if not getattr(cfg, "regime_enabled", False):
        return RegimeTagger(NullClassifier())
    params = dict(getattr(cfg, "parameters", {}) or {})
    classifier = get_regime_classifier(str(params.get("regime_classifier", "null")), params)
    return RegimeTagger(classifier, gap_pct=float(params.get("regime_gap_pct", 0.007)))
