"""Market Regime Engine — the observational tagger the engine holds.

Ported from the reference repository's ``framework/regime/`` (Phase 3 Part 2b-i),
**null classifier only** — deviation D21. The reference package is 421 lines
across five modules; this is the ~120 the engine actually needs.

Why only the null classifier: the one real classifier (``adx_atr``) is built on
ADX and ATR, and Part 2a deliberately did not port those two indicators because
nothing consumed them and Phase 4 owns the indicator layer. Porting a classifier
whose inputs do not exist would produce either a broken import or two more
untested indicators. The regime axis is *purely observational* — it tags trades
and changes no trading decision — so a tagger that labels everything
``UNCLASSIFIED`` is exactly the reference's own behaviour with ``regime.enabled:
false``, which is also this repository's default.

The registry and the :class:`RegimeClassifier` contract come across intact, so
adding ``adx_atr`` later is a new file plus one decorator, with no change here.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any

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
