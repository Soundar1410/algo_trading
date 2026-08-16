"""Central, generic margin-estimation service (request item 2 of the
`strategy-weekly-delta-neutral` gap-closing task).

No strategy calculates its own margin, and no strategy — and nothing in this
package — ever submits or constructs an order to find one out.
:class:`~common.margin.estimator.MarginEstimator` is the one door: a real,
read-only Dhan margin-calculator response, summed per leg (a deliberately
conservative choice — no cross-margin/hedge netting assumed), never a
fabricated zero and never silently substituted with
:class:`maximum_theoretical_loss`.

**Production posture, corrected after independent review:** unlike
:mod:`common.greeks` (which has a centrally tested fallback *model* it may
use automatically when the broker source is stale/unavailable),
:class:`MarginEstimator` has no automatic production fallback. When the real
Dhan margin-calculator source is missing, stale, invalid or fails,
:meth:`~common.margin.estimator.MarginEstimator.estimate_basket` raises
:class:`~common.margin.models.MarginUnavailable` — full stop. The caller
(the strategy's entry gate) must block entry and record an incident; it must
never substitute an estimate to let entry proceed.

:class:`~common.margin.model.ConservativeMarginModel` exists only as an
explicitly-injected offline/test double (a fixture, an offline analysis
script) — never wired into the production composition root
(`runtimes/positional_options/__main__.py`). Its own docstring states this
plainly: it is not a proven upper bound on real broker margin.
"""

from __future__ import annotations

from .estimator import MarginEstimator
from .model import ConservativeMarginModel, MarginModelAssumptions
from .models import OVERNIGHT_PRODUCT_TYPE, LegMarginRequest, MarginEstimate, MarginUnavailable

__all__ = [
    "OVERNIGHT_PRODUCT_TYPE",
    "ConservativeMarginModel",
    "LegMarginRequest",
    "MarginEstimate",
    "MarginEstimator",
    "MarginModelAssumptions",
    "MarginUnavailable",
]
