"""The production :data:`~common.market_data.option_chain.ChainFetcher` —
one read-only HTTP call to Dhan's Option Chain endpoint, wired through
:class:`~common.market_data.option_chain.OptionChainService`'s own
throttle/cache so no strategy or engine code ever calls Dhan directly (spec
section 8.2: "No direct Dhan access is allowed from strategy code").

Matches the exact request shape already proven against the real endpoint in
``tests/smoke/test_live_feed_smoke.py::test_the_option_chain_throttle_holds_
against_the_real_endpoint`` — same headers ("client-id", not "dhanClientId";
``dhanClientId`` in the body, not a header — see that test's own comment and
runbook limitation 19), same body keys. Constructs no order-capable client;
this module's only import beyond the standard library is ``httpx``.
"""

from __future__ import annotations

import httpx

from common.market_data.option_chain import ChainFetcher

OPTION_CHAIN_URL = "https://api.dhan.co/v2/optionchain"
EXPIRY_LIST_URL = "https://api.dhan.co/v2/optionchain/expirylist"

DEFAULT_TIMEOUT_SECONDS = 15.0


def build_dhan_chain_fetcher(
    *, client_id: str, access_token: str, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> ChainFetcher:
    """Return a :data:`~common.market_data.option_chain.ChainFetcher` bound
    to one authenticated client. Read-only: a single ``POST`` per call, no
    retry loop of its own (the chain service's own cache/throttle is the
    retry boundary), no order-related endpoint reachable from this module.
    """

    def fetch(security_id: int, segment: str, expiry: str) -> dict[str, object]:
        response = httpx.post(
            OPTION_CHAIN_URL,
            headers={"access-token": access_token, "client-id": client_id},
            json={
                "UnderlyingScrip": security_id,
                "UnderlyingSeg": segment,
                "Expiry": expiry,
                "dhanClientId": client_id,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return dict(response.json())

    return fetch


def fetch_dhan_expiry_list(
    *, client_id: str, access_token: str, security_id: int, segment: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[str]:
    """Every listed expiry date for one underlying, exchange-published order.

    Read-only. Used only to cross-check/validate a resolved expiry against
    the exchange's own current listing (spec section 3.4) — never to derive
    an expiry from weekday arithmetic.
    """
    response = httpx.post(
        EXPIRY_LIST_URL,
        headers={"access-token": access_token, "client-id": client_id},
        json={"UnderlyingScrip": security_id, "UnderlyingSeg": segment},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]
