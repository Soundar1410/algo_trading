"""Dhan intraday historical candles — REST, not the SDK.

Speaks ``POST https://api.dhan.co/v2/charts/intraday`` directly via ``httpx``,
for the same reason ``common/authentication/dhan_login.py`` bypasses the SDK
for auth: this project's SDK-isolation rule says only
``common/market_data/dhan.py`` may import ``dhanhq`` — a test enforces it
(``tests/unit/test_dhan_adapter.py``) — and the installed 2.2.0 SDK's own
historical-data call (``_historical_data.py:intraday_minute_data``) has **no**
retry policy and **no** rate-limit handling at all; it never raises, it just
returns ``{"status": "failure", ...}`` and leaves the caller to notice.

**Request shape, verified against Dhan's own documentation, not the reference
implementation this module started from.** The reference (a different, older
repository) passed ``fromDate``/``toDate`` as bare ``"YYYY-MM-DD"`` strings.
Dhan's documented request shape for this endpoint is a full datetime string,
e.g. ``"2024-09-11 09:30:00"`` — :meth:`DhanHistoricalDataClient.fetch_intraday`
uses that corrected format. The response is a **top-level** object with
parallel arrays (``open``/``high``/``low``/``close``/``volume``/``timestamp``,
epoch seconds) — not nested under a ``"data"`` key, though
:func:`common.warmup.historical.parse_intraday_response` keeps a defensive
fallback for a nested shape anyway, since the still-unverified case (a partial
candle for the still-forming period during live market hours) has no captured
evidence in this repository either way.

**Retry is single-process and single-call scoped, deliberately narrow.** A
bounded number of attempts with short backoff for *this worker's own* fetch —
nothing here coordinates across processes. Multiple strategy workers starting
simultaneously can still collide on Dhan's own rate limit; that residual risk
is recorded as a known limitation, not solved here (the reference's fix for
the equivalent problem, ``framework/warmup/coordinator.py``, is cross-strategy
scope and stays out of this part).

Nothing here reaches an order-placing endpoint. This module never imports
``dhanhq``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

from common.logging import get_logger

_log = get_logger(__name__)

INTRADAY_ENDPOINT = "https://api.dhan.co/v2/charts/intraday"

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_INITIAL_BACKOFF_SECONDS = 1.0
DEFAULT_BACKOFF_MULTIPLIER = 2.0

#: Statuses that mean "this request is wrong", not "try again". Matches
#: dhan_login.py's own classification for the same reason: retrying a bad
#: request cannot fix it, and hammering a failing request risks whatever rate
#: limit or lockout the account is subject to.
_PERMANENT_REJECTION_STATUSES = frozenset({400, 401, 403})

#: Injectable HTTP seam, exactly dhan_login.py's pattern -- tests supply a
#: stub, so no test here needs the network.
HttpPost = Callable[..., httpx.Response]


class HistoricalDataError(RuntimeError):
    """Base for every failure this client raises."""


class HistoricalDataTransientError(HistoricalDataError):
    """A network failure, a 429, a 5xx, or an unrecognised failure shape.

    Retried internally up to ``max_attempts``; raised only once attempts are
    exhausted.
    """


class HistoricalDataRejectedError(HistoricalDataError):
    """A permanent rejection (bad credentials or a malformed request).

    Never retried -- see ``_PERMANENT_REJECTION_STATUSES``.
    """


class DhanHistoricalDataClient:
    """Fetches raw intraday-candle JSON for one security over one date range."""

    def __init__(
        self,
        client_id: str,
        access_token: str,
        *,
        endpoint: str = INTRADAY_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        http_post: HttpPost | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        initial_backoff: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
        backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not (client_id and access_token):
            raise ValueError("client_id and access_token are both required")
        self._client_id = client_id
        self._access_token = access_token
        self._endpoint = endpoint
        self._timeout = timeout
        self._http_post = http_post or httpx.post
        self._max_attempts = max(1, int(max_attempts))
        self._initial_backoff = initial_backoff
        self._backoff_multiplier = backoff_multiplier
        self._sleep = sleep
        #: Every request this instance has made, across every call and every
        #: retry -- a test's cheapest way to prove the attempt count.
        self.request_count = 0

    def fetch_intraday(
        self,
        *,
        security_id: str,
        exchange_segment: str,
        instrument_type: str,
        from_at: datetime,
        to_at: datetime,
        interval_minutes: int = 1,
    ) -> dict[str, Any]:
        """Fetch raw intraday-candle JSON. Returns the response body on success.

        Raises:
            HistoricalDataRejectedError: a permanent rejection (400/401/403).
                Not retried.
            HistoricalDataTransientError: every attempt failed transiently
                (network error, 429, 5xx, or an unrecognised failure shape).
        """
        body = {
            "securityId": str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument": instrument_type,
            "interval": interval_minutes,
            # Corrected format -- see the module docstring. Dhan documents a
            # full "YYYY-MM-DD HH:MM:SS" datetime, not a bare date.
            "fromDate": from_at.strftime("%Y-%m-%d %H:%M:%S"),
            "toDate": to_at.strftime("%Y-%m-%d %H:%M:%S"),
        }
        headers = {
            "access-token": self._access_token,
            "dhanClientId": self._client_id,
            "Content-Type": "application/json",
        }

        last_exc: HistoricalDataTransientError | None = None
        for attempt in range(1, self._max_attempts + 1):
            self.request_count += 1
            try:
                response = self._http_post(
                    self._endpoint, json=body, headers=headers, timeout=self._timeout
                )
            except httpx.HTTPError as exc:
                last_exc = HistoricalDataTransientError(
                    f"Network error contacting Dhan historical data: {exc}"
                )
            else:
                try:
                    return _classify(response)
                except HistoricalDataRejectedError:
                    raise
                except HistoricalDataTransientError as exc:
                    last_exc = exc

            if attempt < self._max_attempts:
                delay = self._initial_backoff * (self._backoff_multiplier ** (attempt - 1))
                _log.warning(
                    "historical data fetch attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt,
                    self._max_attempts,
                    last_exc,
                    delay,
                )
                self._sleep(delay)

        assert last_exc is not None  # the loop always sets it before falling through
        raise last_exc


def _classify(response: httpx.Response) -> dict[str, Any]:
    """Turn one HTTP response into a parsed body, or a correctly-typed failure.

    1. **200** -- success. Returned as the parsed JSON body, unexamined: the
       parallel-array shape is :func:`common.warmup.historical.
       parse_intraday_response`'s job to validate, not this client's.
    2. **400/401/403** -- permanent. The request or credentials are wrong;
       retrying cannot fix either.
    3. **429** -- transient (rate limited).
    4. **Anything else** (5xx, unexpected status) -- transient. This endpoint's
       failure-message shape for "permanently invalid" vs. "rate limited" is
       not documented anywhere consulted, so this errs toward retrying rather
       than toward discarding a request that might have succeeded on a second
       attempt -- costs latency on a genuinely malformed request, never
       correctness, since a warm-up failure always degrades to a safe cold
       start.
    """
    status = response.status_code
    if status == 200:
        return _safe_json(response)

    message = _error_message(response) or (response.text or "").strip() or "no response body"

    if status in _PERMANENT_REJECTION_STATUSES:
        raise HistoricalDataRejectedError(
            f"Dhan rejected the historical-data request (HTTP {status}): {message}"
        )

    raise HistoricalDataTransientError(
        f"Dhan historical-data call failed (HTTP {status}): {message}"
    )


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_message(response: httpx.Response) -> str | None:
    body = _safe_json(response)
    for key in ("errorMessage", "message", "error", "remarks", "status"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None
