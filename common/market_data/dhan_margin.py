"""The production margin fetchers for :class:`~common.margin.estimator.
MarginEstimator` — read-only calls to Dhan's margin calculator (spec section
3.7: "use an approved read-only Dhan basket-margin/margin-calculator
endpoint when available").

Two fetchers, and the basket one is the right default:

- :func:`build_dhan_basket_margin_fetcher` — **one** call to
  ``POST /v2/margincalculator/multi``, which prices the legs together and
  applies real hedge relief.
- :func:`build_dhan_margin_fetcher` — one call per leg to single-instrument
  ``POST /v2/margincalculator``; the estimator then sums the results, with
  no cross-margin netting.

This module previously offered only the per-leg fetcher, on the stated
premise that "the pinned SDK/API exposes no basket-margin endpoint". **That
premise was wrong** and it had a real cost: ``weekly_delta_neutral`` was
refused entry on every evaluation of every entry window it ever saw,
because a defined-risk iron condor summed leg-by-leg is priced as two
uncovered short options. Measured against the live API on 9 September 2026,
same four legs, same quantity:

    per-leg summed   Rs 61,07,252   (152.68% of allocated capital)
    hedged basket    Rs 15,92,934   ( 39.82%)

The pinned ``dhanhq`` 2.2.0 SDK still does not wrap ``/multi`` — this module
calls the REST API directly with ``httpx``, as it always has, so no SDK
change is involved.

**Never submits or constructs an order.** Both endpoints are Dhan's own
calculators — they return a computed margin figure and place nothing —
matching the same read-only guarantee ``common.market_data.
dhan_option_chain`` already gives the option-chain calls. This module's only
import beyond the standard library is ``httpx``, and it constructs no
broker/order client.

"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import httpx

from common.margin import LegMarginRequest
from common.margin.models import OVERNIGHT_PRODUCT_TYPE

MARGIN_CALCULATOR_URL = "https://api.dhan.co/v2/margincalculator"
MULTI_MARGIN_CALCULATOR_URL = "https://api.dhan.co/v2/margincalculator/multi"

DEFAULT_TIMEOUT_SECONDS = 15.0

__all__ = [
    "MARGIN_CALCULATOR_URL",
    "MULTI_MARGIN_CALCULATOR_URL",
    "OVERNIGHT_PRODUCT_TYPE",
    "BasketMargin",
    "build_dhan_basket_margin_fetcher",
    "build_dhan_margin_fetcher",
]


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


@dataclass(frozen=True)
class BasketMargin:
    """One hedged basket's margin, as the multi-leg calculator returned it.

    ``components`` is the broker's own named breakdown, recorded verbatim
    for audit. Note Dhan reports ``hedgeBenefit`` as ``0.0`` even on a
    basket where hedging is plainly recognised — the relief shows up as a
    far smaller ``spanMargin`` instead — so nothing here treats
    ``hedgeBenefit`` as evidence that the legs were netted.
    """

    total_margin: float
    components: tuple[tuple[str, float], ...] = ()


#: Response keys copied into :attr:`BasketMargin.components` when present.
#: Verified against a live 200 response on 9 September 2026, which is
#: camelCase (``totalMargin``/``spanMargin``/``exposure``) — *not* the
#: snake_case the public documentation shows. The parser below accepts both.
_COMPONENT_KEYS = (
    "spanMargin",
    "exposure",
    "exposureMargin",
    "foMargin",
    "equityMargin",
    "hedgeBenefit",
    "insufficientFund",
)


def build_dhan_basket_margin_fetcher(
    *, client_id: str, access_token: str, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> Callable[[list[LegMarginRequest]], BasketMargin]:
    """Return a ``Callable[[list[LegMarginRequest]], BasketMargin]`` bound to
    one authenticated client — the injection shape
    :class:`~common.margin.estimator.MarginEstimator` takes as
    ``basket_margin_fetcher``.

    **One** ``POST`` for the whole basket, so the broker prices the legs
    against each other. ``includePosition``/``includeOrders`` are both sent
    ``False`` on purpose: this is a pre-entry check on hypothetical legs, and
    folding in the account's existing book would answer a different question
    than "what would this basket cost". (Measured: the flags make no
    difference to the returned figure today, but sending them explicitly
    keeps the request deterministic rather than relying on a server-side
    default.)

    Quantities must be whole multiples of the instrument's lot size. Dhan
    rejects anything else with ``DH-905`` ("Missing required fields, bad
    values for parameters"), an error message that names neither the field
    nor the reason — worth knowing, because it is indistinguishable from a
    genuinely malformed body.
    """

    def fetch(legs: list[LegMarginRequest]) -> BasketMargin:
        if not legs:
            raise ValueError("basket margin requested for an empty leg list")
        response = httpx.post(
            MULTI_MARGIN_CALCULATOR_URL,
            headers={"access-token": access_token, "client-id": client_id},
            json={
                "dhanClientId": client_id,
                "includePosition": False,
                "includeOrders": False,
                "scripList": [
                    {
                        "exchangeSegment": leg.exchange_segment,
                        "transactionType": leg.side.value,
                        "quantity": int(leg.quantity),
                        "productType": leg.product_type,
                        "securityId": leg.security_id,
                        "price": float(leg.reference_price),
                        "triggerPrice": 0.0,
                    }
                    for leg in legs
                ],
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        margin = _extract_total_margin(payload)
        if margin is None:
            raise ValueError(
                f"Dhan multi-margin response has no recognisable totalMargin: {payload!r}"
            )
        return BasketMargin(
            total_margin=float(margin), components=_extract_components(payload)
        )

    return fetch


def _extract_components(payload: object) -> tuple[tuple[str, float], ...]:
    """The broker's named breakdown, best-effort and never load-bearing —
    a missing or non-numeric component is skipped, not an error, because
    the decision depends only on the total."""
    body = payload if isinstance(payload, dict) else {}
    nested = body.get("data")
    source = nested if isinstance(nested, dict) else body
    components = []
    for key in _COMPONENT_KEYS:
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            components.append((key, float(value)))
    return tuple(components)


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
    for key in ("totalMargin", "total_margin"):
        if key in payload:
            return payload.get(key)
    data = payload.get("data")
    if isinstance(data, dict) and "totalMargin" in data:
        return data.get("totalMargin")
    return None
