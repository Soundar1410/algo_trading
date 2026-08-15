"""Typed shapes internal to ``weekly_delta_neutral`` — never imported by
:mod:`common.engine.positional` (the strategy depends on the generic
engine, never the reverse)."""

from __future__ import annotations

from dataclasses import dataclass

from common.engine.models import OptionContract
from common.engine.positional.positional_models import LegRole
from common.greeks import GreekSnapshot
from common.market_data.chain_view import ChainQuote


@dataclass(frozen=True)
class LegCandidate:
    """One fully-resolved, quote/Greek-validated candidate leg."""

    role: LegRole
    contract: OptionContract
    quote: ChainQuote
    greeks: GreekSnapshot

    @property
    def strike(self) -> float:
        return self.contract.strike


@dataclass(frozen=True)
class IronCondorCandidate:
    """One complete, atomically-selected four-leg candidate (spec section
    3.5) — the deterministic search's final output, or ``None`` when no
    complete candidate satisfies every filter (never a relaxed one)."""

    hedge_put: LegCandidate
    hedge_call: LegCandidate
    short_put: LegCandidate
    short_call: LegCandidate
    initial_net_credit: float
    wing_width: float
    credit_to_width_ratio: float
    net_delta_per_lot: float
    maximum_theoretical_loss: float

    def legs(self) -> tuple[LegCandidate, LegCandidate, LegCandidate, LegCandidate]:
        """Hedge-first order — matches spec section 5.2's required entry
        sequence exactly: both hedges before either short."""
        return (self.hedge_put, self.hedge_call, self.short_put, self.short_call)
