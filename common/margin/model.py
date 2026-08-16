"""``ConservativeMarginModel`` — an offline/test-only deterministic margin
approximation. **Never wired into the production composition root.**

This is not the fallback-model half of a chain-then-model priority the way
:mod:`common.greeks.model` is for Greeks. Independent review of the first
draft of this package correctly rejected that shape for margin: an
"upward-biased" percentage-of-notional formula has never been validated
against NSE's real SPAN+exposure margin, and treating an unproven formula as
safe-because-conservative is exactly the kind of unverified assumption that
must not gate real (even paper-forward) capital. So this model exists only
as something a test, or an explicitly-written offline/paper-analysis script,
may construct and pass to :class:`~common.margin.estimator.MarginEstimator`
as ``fallback_model=...`` — the production entrypoint
(``runtimes/positional_options/__main__.py``) never constructs one, and
:class:`~common.margin.estimator.MarginEstimator` never selects it on its
own.
"""

from __future__ import annotations

from dataclasses import dataclass

from common.models import OrderSide

from .models import LegMarginRequest


@dataclass(frozen=True)
class MarginModelAssumptions:
    """Version-controlled constants for :class:`ConservativeMarginModel` —
    never hardcoded inline at a call site. Not derived from, calibrated
    against, or claimed to approximate NSE's real SPAN+exposure margin
    methodology; picked only to give a deterministic, non-zero, non-trivial
    number for offline/test use."""

    #: Fraction of a short leg's notional (spot x quantity) charged as
    #: margin. Long (hedge) legs are assumed to require no additional margin
    #: in this simplified model — the opposite of "upward-biased", which is
    #: exactly why this must never run in production: a real exchange
    #: margin call for a naked-looking short leg can exceed this.
    short_leg_notional_percent: float = 0.12


class ConservativeMarginModel:
    """A deterministic, offline-only margin approximation. See module
    docstring: this is a test double, not a broker-margin substitute."""

    def __init__(self, assumptions: MarginModelAssumptions | None = None) -> None:
        self._assumptions = assumptions or MarginModelAssumptions()

    def estimate(self, legs: list[LegMarginRequest], *, spot: float) -> float:
        total = 0.0
        for leg in legs:
            if leg.side is OrderSide.SELL:
                total += self._assumptions.short_leg_notional_percent * spot * leg.quantity
        return total
