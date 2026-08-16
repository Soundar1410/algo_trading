"""Typed shapes for :mod:`common.margin` — mirrors :mod:`common.greeks.models`'
discipline: every estimate carries its own source and timestamp, so a margin
decision can never silently change meaning between entry, dashboard and
recovery.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from common.models import OrderSide

#: Dhan's overnight-carry product code — spec section 3.1: "Never use an
#: intraday-only product for this strategy." Lives here (not in
#: ``common.market_data.dhan_margin``) so a strategy building
#: :class:`LegMarginRequest` values never has to import the Dhan-specific
#: fetcher module just for this constant.
OVERNIGHT_PRODUCT_TYPE = "MARGIN"


class MarginUnavailable(RuntimeError):
    """No usable margin estimate could be produced for one basket — from the
    real broker source, or (only when a caller explicitly injected one) the
    offline/test fallback model. The caller must block the entry this was
    computed for and record an incident; it must never substitute a
    fabricated value to let entry proceed. Unlike
    :class:`~common.greeks.models.GreeksUnavailable`, this exception has no
    "still block adjustments but not exits" nuance baked into a caller
    contract — margin is an *entry-only* gate (spec section 3.7); the
    ongoing exit-priority margin-breach check (spec section 6.2 step 10)
    already treats "no estimate available" as "no breach signal this
    evaluation", never as a reason to suppress a real exit.
    """


@dataclass(frozen=True)
class LegMarginRequest:
    """One leg's inputs to a margin calculation — never an order. Product
    type is fixed to the overnight-carry product (spec section 3.1: never an
    intraday-only product), passed explicitly rather than assumed by the
    fetcher so it is visible at every call site."""

    security_id: str
    exchange_segment: str
    side: OrderSide
    quantity: int
    product_type: str
    #: The reference price a margin calculator prices the leg at — never
    #: synthesized from nothing; the caller supplies the same fresh
    #: bid/ask-derived price the entry candidate itself was evaluated with.
    reference_price: float


@dataclass(frozen=True)
class MarginEstimate:
    """One basket's estimated margin requirement, with full provenance.

    ``source`` is a free-text label naming where ``estimated_margin`` came
    from (e.g. ``"dhan_margin_calculator_summed_legs"`` in production, or
    ``"conservative_model_v1"`` only ever from an explicitly-injected
    offline/test fallback) — never guessed at by a caller that only has the
    number.
    """

    estimated_margin: float
    source: str
    estimated_at: datetime
    allocated_capital: float
    #: One entry per leg this estimate was built from — (security_id,
    #: margin component) — kept for audit/dashboard display, never used to
    #: recompute ``estimated_margin`` (that sum is fixed at construction).
    per_leg: tuple[tuple[str, float], ...] = ()

    @property
    def utilization_percent(self) -> float:
        if self.allocated_capital <= 0:
            return math.inf
        return 100.0 * self.estimated_margin / self.allocated_capital

    def age_seconds(self, *, now: datetime) -> float:
        return (now - self.estimated_at).total_seconds()

    def is_fresh(self, *, now: datetime, max_age_seconds: float) -> bool:
        if not math.isfinite(self.estimated_margin) or self.estimated_margin < 0:
            return False
        return self.age_seconds(now=now) <= max_age_seconds
