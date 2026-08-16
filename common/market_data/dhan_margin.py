"""The production margin fetcher for :class:`~common.margin.estimator.
MarginEstimator` — one read-only HTTP call per leg to Dhan's margin
calculator (spec section 3.7: "use an approved read-only Dhan basket-margin/
margin-calculator endpoint when available"). The pinned SDK/API exposes no
*basket*-margin endpoint (only single-instrument
``POST /v2/margincalculator``), so this module calls it once per leg;
:class:`~common.margin.estimator.MarginEstimator` sums the results, a
documented conservative choice (no cross-margin netting assumed).

**Never submits or constructs an order.** ``/margincalculator`` is Dhan's
own calculator endpoint — it returns a computed margin figure and places
nothing — matching the same read-only guarantee
``common.market_data.dhan_option_chain`` already gives the option-chain
calls. This module's only import beyond the standard library is ``httpx``,
and it constructs no broker/order client.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from common.margin import LegMarginRequest
from common.margin.models import OVERNIGHT_PRODUCT_TYPE

MARGIN_CALCULATOR_URL = "https://api.dhan.co/v2/margincalculator"

DEFAULT_TIMEOUT_SECONDS = 15.0

__all__ = ["MARGIN_CALCULATOR_URL", "OVERNIGHT_PRODUCT_TYPE", "build_dhan_margin_fetcher"]


def build_dhan_margin_fetcher(
    *, client_id: str, access_token: str, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> Callable[[LegMarginRequest], float]:
    """Return a ``Callable[[LegMarginRequest], float]`` bound to one
    authenticated client — the exact injection shape
    :class:`~common.margin.estimator.MarginEstimator` takes as
    ``margin_fetcher``. One ``POST`` per call, no retry loop of its own (the
    estimator's own per-leg loop is the caller), no order-related endpoint
    reachable from this module.
    """

    def fetch(leg: LegMarginRequest) -> float:
        response = httpx.post(
            MARGIN_CALCULATOR_URL,
            headers={"access-token": access_token, "client-id": client_id},
            json={
                "dhanClientId": client_id,
                "securityId": leg.security_id,
                "exchangeSegment": leg.exchange_segment,
                "transactionType": leg.side.value,
                "quantity": int(leg.quantity),
                "productType": leg.product_type,
                "price": float(leg.reference_price),
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        margin = _extract_total_margin(payload)
        if margin is None:
            raise ValueError(
                f"Dhan margin-calculator response has no recognisable totalMargin: {payload!r}"
            )
        return float(margin)

    return fetch


def _extract_total_margin(payload: object) -> float | None:
    """Dhan's own public reference shows ``/margincalculator`` returning
    ``totalMargin`` at the top level (unlike ``/optionchain``'s
    ``{"data": {...}}`` envelope) — but this has not been re-verified
    against a live response in this environment (the same category of gap
    ``common.market_data.chain_view`` already documents for the option
    chain), so both the documented top-level shape and a defensive
    ``data.totalMargin`` fallback are accepted here. Neither guess is
    trusted blindly: :func:`build_dhan_margin_fetcher`'s caller
    (:class:`~common.margin.estimator.MarginEstimator`) raises
    ``MarginUnavailable`` — never proceeds with a fabricated value — the
    moment neither shape matches.
    """
    if not isinstance(payload, dict):
        return None
    if "totalMargin" in payload:
        return payload.get("totalMargin")
    data = payload.get("data")
    if isinstance(data, dict) and "totalMargin" in data:
        return data.get("totalMargin")
    return None
