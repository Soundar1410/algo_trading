"""Dhan headless access-token generation, and read-only token validation.

**This is the only module that speaks to Dhan's auth server.** It implements the
direct TOTP flow, ported from the reference repository's
``framework/auth/dhan_auth.py`` (verified read-only, never imported):

    POST https://auth.dhan.co/app/generateAccessToken
        ?dhanClientId=<id>&pin=<pin>&totp=<6 digits>
    -> {"accessToken": "<jwt>", "expiryTime": "<iso8601>", ...}

Two deliberate departures from the reference:

* **``httpx``, not ``requests``.** ``httpx`` is already a pinned dependency of
  this project; adding ``requests`` for one call would be a second HTTP stack.
* **The SDK is not used.** ``dhanhq`` 2.2.0 ships a ``DhanLogin`` class hitting
  this same endpoint, but importing it here would break the project's
  SDK-isolation rule — ``common/market_data/dhan.py`` is the only module allowed
  to import ``dhanhq``, and a test enforces it. Its version also has no retry
  policy, no rate-limit detection and swallows every failure into a bare
  ``Exception``, so there is nothing to gain by coupling to it.

**Retryability is the whole point of this module.** See :func:`_classify`.
Nothing here places, modifies or cancels an order; order placement lives on
``/orders``, which this project does not touch in any phase before Phase 10.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from common.logging import get_logger

from .exceptions import (
    InvalidCredentialsError,
    TokenGenerationError,
    TokenRateLimitedError,
)
from .totp import TotpProvider, clock_skew_note

_log = get_logger(__name__)

AUTH_ENDPOINT = "https://auth.dhan.co/app/generateAccessToken"
PROFILE_ENDPOINT = "https://api.dhan.co/v2/profile"

DEFAULT_TIMEOUT_SECONDS = 10.0

#: Substrings Dhan has been observed to use when refusing a too-frequent request.
#: These only improve the *error message*; they never decide retryability. The
#: reference implementation used them to classify, which made a rephrasing
#: upstream silently convert a rate-limit into a retryable error — see
#: :func:`_classify`.
_RATE_LIMIT_MARKERS = ("every 2 minutes", "once every", "rate limit", "too many")

#: Statuses that mean "your credentials are wrong", not "try again later".
_CREDENTIAL_REJECTION_STATUSES = frozenset({400, 401, 403})


@dataclass(frozen=True)
class GeneratedToken:
    """A freshly minted token, exactly as Dhan returned it."""

    access_token: str
    expiry_time: str | None


#: Injectable HTTP seam. Tests supply a stub, so no test needs the network.
HttpPost = Callable[..., httpx.Response]
HttpGet = Callable[..., httpx.Response]


class DhanTotpLogin:
    """Generates a Dhan access token from client id + PIN + TOTP secret."""

    def __init__(
        self,
        client_id: str,
        pin: str,
        totp_provider: TotpProvider,
        *,
        endpoint: str = AUTH_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        http_post: HttpPost | None = None,
    ) -> None:
        if not (client_id and pin):
            raise InvalidCredentialsError(
                "client_id and pin are both required for the TOTP login flow"
            )
        self._client_id = client_id
        self._pin = pin
        self._totp_provider = totp_provider
        self._endpoint = endpoint
        self._timeout = timeout
        self._http_post = http_post or httpx.post
        #: Every request this instance has made. The bootstrap asserts on this,
        #: because "exactly one attempt on a credential rejection" is a property
        #: worth measuring rather than assuming.
        self.request_count = 0

    def generate(self) -> GeneratedToken:
        """Perform one login attempt.

        Makes **exactly one** HTTP request. Retry policy belongs to the caller,
        which decides using the raised exception's ``retryable`` flag.

        Raises:
            InvalidCredentialsError: Dhan rejected the PIN/TOTP/client id, or
                answered 200 without a token. Permanent — must not be retried.
            TokenRateLimitedError: refused as too frequent. Not retryable within
                any sane retry window.
            TokenGenerationError: transient (network, timeout, 5xx).
        """
        totp = self._totp_provider()
        params = {"dhanClientId": self._client_id, "pin": self._pin, "totp": totp}
        self.request_count += 1
        try:
            response = self._http_post(self._endpoint, params=params, timeout=self._timeout)
        except httpx.HTTPError as exc:
            # Network-level failure: nothing was decided by Dhan, so a bounded
            # retry is legitimate here and only here.
            raise TokenGenerationError(f"Network error contacting Dhan auth: {exc}") from exc
        return _classify(response)


def _classify(response: httpx.Response) -> GeneratedToken:
    """Turn one auth response into a token or a correctly-typed failure.

    The retryability rules, in order of application:

    1. **200 with a token** — success.
    2. **200 without a token** — permanent, *regardless of message wording*.
       This is the hardening. Dhan reports its ~1-per-2-minute generation cap as
       an HTTP 200 carrying an explanatory message and no token. The reference
       implementation detected that by substring-matching the message, which
       means a rephrasing upstream would fall through to "unknown error" and be
       **retried** — three attempts in ten seconds against a two-minute limit.
       The invariant that actually holds is structural: if the server answered
       successfully and declined to issue a token, no amount of retrying in the
       next few seconds changes that answer. Marker matching now only chooses
       between two error *messages*.
    3. **400/401/403** — the credentials themselves are wrong. Permanent, so
       retries cannot hammer the account toward a lockout.
    4. **Anything else** (5xx, unexpected status) — transient.
    """
    status = response.status_code
    body = _safe_json(response)
    data = _unwrap(body)
    token = data.get("accessToken") or data.get("access_token")

    if status == 200 and token:
        expiry = data.get("expiryTime") or data.get("expiry_time")
        return GeneratedToken(
            access_token=str(token),
            expiry_time=str(expiry) if expiry else None,
        )

    message = _error_message(body) or (response.text or "").strip() or "no response body"
    looks_rate_limited = any(marker in message.lower() for marker in _RATE_LIMIT_MARKERS)

    if status == 200:
        # Rule 2. Both branches are permanent; only the wording differs.
        if looks_rate_limited:
            raise TokenRateLimitedError(
                f"Dhan is rate-limiting token generation: {message}. "
                "It caps generation at roughly one request every two minutes."
            )
        raise InvalidCredentialsError(
            f"Dhan returned HTTP 200 with no access token: {message}. "
            "Treated as permanent: the server answered successfully and declined "
            "to issue a token, so an immediate retry cannot change the outcome."
        )

    if looks_rate_limited:
        raise TokenRateLimitedError(f"Dhan is rate-limiting token generation: {message}")

    if status in _CREDENTIAL_REJECTION_STATUSES:
        skew = clock_skew_note()
        hint = f" Note: {skew}" if skew else ""
        raise InvalidCredentialsError(
            f"Dhan rejected the credentials (HTTP {status}): {message}. "
            f"Not retrying — repeated rejected logins risk locking the account.{hint}"
        )

    raise TokenGenerationError(f"Dhan auth failed (HTTP {status}): {message}")


def validate_token(
    client_id: str,
    access_token: str,
    *,
    endpoint: str = PROFILE_ENDPOINT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    http_get: HttpGet | None = None,
) -> bool:
    """Validate a token with the spec's "safe read request".

    A **read-only** ``GET`` of the account profile. It cannot place, modify or
    cancel an order. Returns True when Dhan accepts the token, False when it
    rejects it (401/403), and raises :class:`TokenGenerationError` when the
    answer is genuinely unknown — a network failure must not be reported as
    "token invalid", because that would trigger a needless regeneration against
    the rate limit.
    """
    getter = http_get or httpx.get
    headers = {"access-token": access_token, "dhanClientId": client_id}
    try:
        response = getter(endpoint, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise TokenGenerationError(f"Could not validate the token: {exc}") from exc

    if response.status_code == 200:
        return True
    if response.status_code in _CREDENTIAL_REJECTION_STATUSES:
        _log.warning("dhan rejected the cached token (HTTP %s)", response.status_code)
        return False
    raise TokenGenerationError(
        f"Token validation was inconclusive (HTTP {response.status_code}); "
        "treating as unknown rather than invalid so a good token is not discarded"
    )


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {}


def _unwrap(body: Any) -> dict[str, Any]:
    """Dhan sometimes wraps the payload in ``{"status": ..., "data": {...}}``."""
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return dict(body["data"])
    return dict(body) if isinstance(body, dict) else {}


def _error_message(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in ("errorMessage", "message", "error", "remarks", "status"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None
