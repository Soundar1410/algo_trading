"""Central, generic option-Greeks service (spec section 4).

No strategy computes its own Greeks — that was the exact legacy defect this
package exists to not repeat (spec section 4.2: "Do not copy the legacy
Greek calculator"). :class:`~common.greeks.service.GreeksService` is the one
door: real Dhan option-chain Greeks first when the response is complete,
correctly mapped and fresh; a vetted third-party model
(:mod:`common.greeks.model`, wrapping ``vollib`` — never a handwritten
pricing formula) second, only when the chain source is unavailable/stale/
incomplete. Every :class:`~common.greeks.models.GreekSnapshot` carries its
own source and source timestamp; nothing here ever fabricates one.
"""

from __future__ import annotations

from .model import GreeksModelUnavailable, black_scholes_merton_greeks
from .models import GreekInputs, GreekSnapshot, GreekSource, GreeksUnavailable
from .service import GreeksService, ModelAssumptions

__all__ = [
    "GreekInputs",
    "GreekSnapshot",
    "GreekSource",
    "GreeksModelUnavailable",
    "GreeksService",
    "GreeksUnavailable",
    "ModelAssumptions",
    "black_scholes_merton_greeks",
]
