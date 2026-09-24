"""Dhan historical candles, intraday and daily — REST, not the SDK.

Speaks ``POST https://api.dhan.co/v2/charts/intraday`` and
``POST https://api.dhan.co/v2/charts/historical`` directly via ``httpx``,
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

**Client-identity fields, corrected against the SDK's own source, not just
documentation (known limitation 19, fixed).** This module used to send
``dhanClientId`` as an HTTP header and nothing in the JSON body. The installed
``dhanhq==2.2.0`` SDK's ``dhan_http.py`` shows the real contract for a POST:
``"client-id"`` is the header key (``dhan_http.py:43``), and ``dhanClientId``
is injected into the JSON body unconditionally, for every POST, before it is
sent (``dhan_http.py:53-56``). ``fetch_intraday`` now matches that shape.
Whether the old shape ever actually failed a live call is not established —
this endpoint has still never been exercised against a real one — which is
exactly why the correction is against the SDK's source rather than against an
observed failure.

**The daily endpoint (Phase 1, ``wsr1_weekly_stochrsi``), verified against a
real call on 2026-09-21** — the body shape comes from the installed SDK's own
``_historical_data.py:historical_daily_data`` (``securityId``,
``exchangeSegment``, ``instrument``, ``expiryCode``, ``oi``, ``fromDate``,
``toDate``), and three things were then established by probing the live
endpoint rather than assumed:

1. ``fromDate``/``toDate`` are bare ``"YYYY-MM-DD"`` here — **not** the full
   datetime ``/charts/intraday`` needed. The SDK documents it that way and the
   live endpoint accepts it.
2. **``toDate`` is EXCLUSIVE.** Requesting ``2024-12-23 -> 2024-12-31``
   returned its last session on 2024-12-30, though 2024-12-31 was a trading
   day; requesting ``-> 2025-01-01`` (a holiday) returned 2024-12-31. So
   :meth:`~DhanHistoricalDataClient.fetch_daily` takes an **inclusive**
   ``to_date`` — the shape every caller wants — and adds the day internally.
   An off-by-one here would fail every staleness check closed (spec 6.2).
3. **No per-request range cap was found.** One call for RELIANCE over
   2000-01-01 -> 2026-09-22 returned 6,145 sessions back to 2002-01-01, so the
   full history ``wsr1_weekly_stochrsi`` fetches (spec 6.1 v1.2d) needs no
   chunking.

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
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from common.logging import get_logger

_log = get_logger(__name__)

INTRADAY_ENDPOINT = "https://api.dhan.co/v2/charts/intraday"
HISTORICAL_ENDPOINT = "https://api.dhan.co/v2/charts/historical"

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
    """Fetches raw candle JSON for one security over one date range.

    Two endpoints, one retry policy: :meth:`fetch_intraday` (minute candles,
    the warm-up path) and :meth:`fetch_daily` (daily candles, the
    ``positional_stocks`` weekly job). Both go through
    :meth:`_post_with_retries`, so the attempt count, the backoff schedule and
    :func:`_classify`'s taxonomy cannot drift apart between them.
    """

    def __init__(
        self,
        client_id: str,
        access_token: str,
        *,
        endpoint: str = INTRADAY_ENDPOINT,
        historical_endpoint: str = HISTORICAL_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        http_post: HttpPost | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        initial_backoff: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
        backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
        sleep: Callable[[float], None] = time.sleep,
        before_request: Callable[[], None] | None = None,
    ) -> None:
        if not (client_id and access_token):
            raise ValueError("client_id and access_token are both required")
        self._client_id = client_id
        self._access_token = access_token
        self._endpoint = endpoint
        self._historical_endpoint = historical_endpoint
        self._timeout = timeout
        self._http_post = http_post or httpx.post
        self._max_attempts = max(1, int(max_attempts))
        self._initial_backoff = initial_backoff
        self._backoff_multiplier = backoff_multiplier
        self._sleep = sleep
        #: Called immediately before **every** attempt, retries included. This
        #: is where a caller's rate throttle belongs: a throttle applied around
        #: ``fetch_*`` instead would be bypassed by this client's own internal
        #: retries, which is precisely the burst a 429 provokes. ``None``
        #: (the default) leaves the intraday warm-up path byte-identical.
        self._before_request = before_request
        #: Every request this instance has made, across every call and every
        #: retry -- a test's cheapest way to prove the attempt count.
        self.request_count = 0

    def _headers(self) -> dict[str, str]:
        return {
            "access-token": self._access_token,
            # "client-id", not "dhanClientId" -- matches the SDK's own header
            # key (dhan_http.py:43). See known limitation 19.
            "client-id": self._client_id,
            "Content-Type": "application/json",
        }

    def _post_with_retries(
        self, endpoint: str, body: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """One request, retried within the configured bounds. Shared by both fetches.

        Raises:
            HistoricalDataRejectedError: a permanent rejection (400/401/403).
                Not retried.
            HistoricalDataTransientError: every attempt failed transiently
                (network error, 429, 5xx, or an unrecognised failure shape).
        """
        last_exc: HistoricalDataTransientError | None = None
        for attempt in range(1, self._max_attempts + 1):
            self.request_count += 1
            if self._before_request is not None:
                self._before_request()
            try:
                response = self._http_post(
                    endpoint, json=body, headers=headers, timeout=self._timeout
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
            # Known limitation 19, fixed: the installed SDK's own dhan_http.py
            # injects this into the body of every POST unconditionally
            # (dhan_http.py:53-56) -- it does not send it as a header. This
            # used to be a "dhanClientId" header instead, which the SDK's
            # source shows Dhan does not read for a POST.
            "dhanClientId": self._client_id,
        }
        return self._post_with_retries(self._endpoint, body, self._headers())

    def fetch_daily(
        self,
        *,
        security_id: str,
        exchange_segment: str,
        instrument_type: str,
        from_date: date,
        to_date: date,
        expiry_code: int = 0,
    ) -> dict[str, Any]:
        """Fetch raw daily-candle JSON. Returns the response body on success.

        ``from_date`` and ``to_date`` are both **inclusive**, which is what
        every caller means by a date range. Dhan's own ``toDate`` is exclusive
        (see the module docstring for the call that established this), so a day
        is added on the way out; that conversion lives here rather than in each
        caller precisely because getting it wrong is invisible until a
        staleness check rejects a series that is in fact current.

        The response body is returned unexamined, exactly as
        :meth:`fetch_intraday` does — validating the parallel-array shape is
        the parser's job, not this client's.

        Raises:
            ValueError: ``to_date`` precedes ``from_date``.
            HistoricalDataRejectedError: a permanent rejection (400/401/403).
                On this endpoint that also covers "the Data API subscription is
                not active for this account", which is not separately
                distinguishable in the response.
            HistoricalDataTransientError: every attempt failed transiently.
        """
        if to_date < from_date:
            raise ValueError(f"to_date {to_date} precedes from_date {from_date}")
        body = {
            "securityId": str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument": instrument_type,
            # Both required by the SDK's own payload for this endpoint
            # (_historical_data.py:historical_daily_data). expiry_code is
            # meaningless for cash equity and an index, but the endpoint is
            # shared with derivatives and the SDK sends it unconditionally.
            "expiryCode": int(expiry_code),
            "oi": False,
            # Bare dates here -- unlike /charts/intraday. See the module
            # docstring: verified against a real call, not assumed.
            "fromDate": from_date.isoformat(),
            "toDate": (to_date + timedelta(days=1)).isoformat(),
            "dhanClientId": self._client_id,
        }
        return self._post_with_retries(self._historical_endpoint, body, self._headers())


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
