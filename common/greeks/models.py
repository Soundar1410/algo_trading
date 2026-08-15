"""Typed shapes for :mod:`common.greeks` — the deterministic input/output
contract the spec requires (section 4.1/4.2): every Greek carries its own
units, source and source timestamp, so delta cannot silently change meaning
between entry, adjustment, dashboard and recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from common.models import OptionType


class GreekSource(StrEnum):
    """Where one :class:`GreekSnapshot` actually came from."""

    #: Dhan's own option-chain response — preferred whenever complete,
    #: correctly mapped and fresh (spec section 4.2 priority 1).
    BROKER_CHAIN = "BROKER_CHAIN"
    #: The centrally tested Black-Scholes-Merton model
    #: (:mod:`common.greeks.model`), used only when the chain source is
    #: unavailable/stale/incomplete (spec section 4.2 priority 2).
    MODEL = "MODEL"


class GreeksUnavailable(RuntimeError):
    """No usable Greeks could be produced for one candidate — from either
    source. The caller must block the risk-increasing decision this was
    computed for (entry or normal adjustment); it must never block a
    risk-reducing exit (spec section 4.2's own fail-open-for-exits rule —
    enforced by the caller, not this exception, since an exit path simply
    never calls into this service for its own trigger decision)."""


@dataclass(frozen=True)
class GreekInputs:
    """Every input one Greek evaluation was computed from — persisted
    verbatim (``cycle_decision_snapshots``, migration 0010) regardless of
    which source produced the output, so a decision snapshot is always fully
    reconstructable (spec section 4.2: "every Greek has a source timestamp
    and maximum age")."""

    spot: float
    strike: float
    option_type: OptionType
    implied_volatility: float
    risk_free_rate: float
    dividend_yield: float
    #: Timezone-aware. Time-to-expiry is derived from this and
    #: ``expiry_at`` at evaluation, never cached across calls.
    evaluation_timestamp: datetime
    expiry_at: datetime


@dataclass(frozen=True)
class GreekSnapshot:
    """One option's delta/gamma/theta/vega/IV, with full provenance.

    ``theta`` is per-calendar-day (not per-year) — the conventional
    options-desk unit — regardless of source; :mod:`common.greeks.model`
    converts its own per-year output before returning one of these.
    """

    security_id: str
    option_type: OptionType
    strike: float
    delta: float
    gamma: float
    theta: float
    vega: float
    implied_volatility: float
    source: GreekSource
    #: The exchange/broker's own timestamp for a chain-sourced snapshot, or
    #: the evaluation timestamp for a model-sourced one — never fabricated.
    source_timestamp: datetime
    received_at: datetime
    #: Present only for a MODEL-sourced snapshot — the exact inputs it was
    #: computed from. ``None`` for BROKER_CHAIN (Dhan's own Greeks are not
    #: reproduced from these inputs by this repository).
    model_inputs: GreekInputs | None = None

    def age_seconds(self, *, now: datetime) -> float:
        return (now - self.received_at).total_seconds()

    def is_fresh(self, *, now: datetime, max_age_seconds: float) -> bool:
        if not _is_finite(self.delta, self.gamma, self.theta, self.vega, self.implied_volatility):
            return False
        return self.age_seconds(now=now) <= max_age_seconds


def _is_finite(*values: float) -> bool:
    import math

    return all(math.isfinite(v) for v in values)
