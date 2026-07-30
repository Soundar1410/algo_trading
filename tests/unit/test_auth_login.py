"""Retryability classification for the Dhan auth response.

These tests exist because the cost of a wrong answer is asymmetric. Wrongly
treating a failure as transient burns token-generation attempts against a
~1-per-2-minute limit and, on a credential rejection, risks an account lockout.
Wrongly treating one as permanent merely means a human restarts the bootstrap.

So the invariant under test throughout is: **one call to Dhan per attempt, and
credential-shaped failures are never retryable.**
"""

from __future__ import annotations

import httpx
import pytest

from common.authentication import (
    AuthError,
    DhanTotpLogin,
    InvalidCredentialsError,
    TokenGenerationError,
    TokenRateLimitedError,
    validate_token,
)
from common.authentication.exceptions import (
    MissingCredentialsError,
    TokenRejectedRecentlyError,
    TokenStoreError,
)

CLIENT_ID = "1100000000"
PIN = "1234"
TOTP = "654321"

_JWT = (
    "eyJhbGciOiJIUzI1NiJ9."
    "eyJleHAiOjQ4NzE1MzI4MDAsImRoYW5DbGllbnRJZCI6IjExMDAwMDAwMDAifQ."
    "c2lnbmF0dXJl"
)


class _Recorder:
    """Captures every request and returns a scripted response."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def __call__(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        if not self._responses:
            raise AssertionError("the login made more requests than the test scripted")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    @property
    def count(self) -> int:
        return len(self.calls)


def _response(status: int, payload: object = None, text: str = "") -> httpx.Response:
    request = httpx.Request("POST", "https://auth.dhan.co/app/generateAccessToken")
    if payload is not None:
        return httpx.Response(status, json=payload, request=request)
    return httpx.Response(status, text=text, request=request)


def _login(*responses: httpx.Response | Exception) -> tuple[DhanTotpLogin, _Recorder]:
    recorder = _Recorder(*responses)
    return (
        DhanTotpLogin(CLIENT_ID, PIN, lambda: TOTP, http_post=recorder),
        recorder,
    )


# --------------------------------------------------------------------- success


def test_a_camelcase_response_yields_a_token():
    login, recorder = _login(
        _response(200, {"accessToken": _JWT, "expiryTime": "2026-07-31T09:00:00Z"})
    )
    token = login.generate()
    assert token.access_token == _JWT
    assert token.expiry_time == "2026-07-31T09:00:00Z"
    assert recorder.count == 1


def test_a_snake_case_response_yields_a_token():
    login, _ = _login(_response(200, {"access_token": _JWT, "expiry_time": "later"}))
    assert login.generate().access_token == _JWT


def test_a_wrapped_envelope_yields_a_token():
    """Dhan sometimes wraps the payload in {"status", "data": {...}}."""
    login, _ = _login(_response(200, {"status": "success", "data": {"accessToken": _JWT}}))
    token = login.generate()
    assert token.access_token == _JWT
    assert token.expiry_time is None


def test_the_request_carries_the_credentials_as_query_params():
    login, recorder = _login(_response(200, {"accessToken": _JWT}))
    login.generate()
    params = recorder.calls[0]["params"]
    assert params == {"dhanClientId": CLIENT_ID, "pin": PIN, "totp": TOTP}


# ------------------------------------------------------- permanent failures


@pytest.mark.parametrize("status", [400, 401, 403])
def test_a_credential_rejection_is_permanent_and_costs_one_request(status: int):
    login, recorder = _login(_response(status, {"errorMessage": "Invalid PIN"}))
    with pytest.raises(InvalidCredentialsError) as caught:
        login.generate()
    assert caught.value.retryable is False
    assert recorder.count == 1, "a rejected credential must cost exactly one request"
    assert "Not retrying" in str(caught.value)


def test_a_200_with_no_token_is_permanent_even_with_unfamiliar_wording():
    """The hardening over the reference implementation.

    Dhan reports its generation cap as HTTP 200 with a message and no token. The
    reference classified that by substring-matching the message, so a rephrasing
    upstream would fall through to "unknown error" and be *retried* — three
    attempts in ten seconds against a two-minute limit. The structural invariant
    is what holds: a successful response that declines to issue a token will not
    start issuing one a few seconds later.
    """
    login, recorder = _login(
        _response(200, {"errorMessage": "Kindly hold on for a short while before retrying"})
    )
    with pytest.raises(InvalidCredentialsError) as caught:
        login.generate()
    assert caught.value.retryable is False
    assert recorder.count == 1
    assert "declined to issue a token" in str(caught.value)


def test_a_recognised_rate_limit_message_is_reported_as_such():
    login, recorder = _login(_response(200, {"errorMessage": "Allowed once every 2 minutes"}))
    with pytest.raises(TokenRateLimitedError) as caught:
        login.generate()
    assert caught.value.retryable is False
    assert recorder.count == 1
    assert "two minutes" in str(caught.value)


def test_a_rate_limit_marker_on_a_4xx_is_still_rate_limited_not_credentials():
    """Marker matching precedes the status check, as in the reference.

    Both outcomes are non-retryable and cost one request, so the request count is
    unaffected — but the operator gets the accurate message.
    """
    login, recorder = _login(_response(400, {"errorMessage": "Too many requests"}))
    with pytest.raises(TokenRateLimitedError):
        login.generate()
    assert recorder.count == 1


def test_an_empty_body_still_produces_a_typed_failure():
    login, _ = _login(_response(401, text=""))
    with pytest.raises(InvalidCredentialsError, match="no response body"):
        login.generate()


def test_a_non_json_body_does_not_raise_a_decode_error():
    login, _ = _login(_response(500, text="<html>gateway</html>"))
    with pytest.raises(TokenGenerationError, match="gateway"):
        login.generate()


# ------------------------------------------------------- transient failures


def test_a_5xx_is_transient():
    login, _ = _login(_response(503, {"errorMessage": "upstream unavailable"}))
    with pytest.raises(TokenGenerationError) as caught:
        login.generate()
    assert caught.value.retryable is True
    assert not isinstance(caught.value, InvalidCredentialsError)


def test_a_network_error_is_transient():
    login, _ = _login(httpx.ConnectError("connection refused"))
    with pytest.raises(TokenGenerationError) as caught:
        login.generate()
    assert caught.value.retryable is True


def test_an_unexpected_status_is_transient():
    login, _ = _login(_response(418, {"message": "teapot"}))
    with pytest.raises(TokenGenerationError) as caught:
        login.generate()
    assert caught.value.retryable is True


# ------------------------------------------------------------- construction


def test_a_missing_pin_is_refused_before_any_request():
    with pytest.raises(InvalidCredentialsError, match="client_id and pin"):
        DhanTotpLogin(CLIENT_ID, "", lambda: TOTP)


def test_the_totp_is_generated_once_per_attempt():
    calls: list[int] = []

    def provider() -> str:
        calls.append(1)
        return TOTP

    recorder = _Recorder(_response(200, {"accessToken": _JWT}))
    login = DhanTotpLogin(CLIENT_ID, PIN, provider, http_post=recorder)
    login.generate()
    assert len(calls) == 1


# --------------------------------------------------------- retryable contract


def test_every_auth_error_declares_retryability_explicitly():
    """Retryability must be a stated attribute, not an artefact of subclassing.

    In the reference implementation InvalidCredentialsError subclasses the
    retryable TokenGenerationError, so non-retryability held only because of
    ``except`` clause ordering at the call site. Reordering those clauses would
    have silently made a wrong PIN retryable.
    """
    assert AuthError.retryable is False
    assert TokenGenerationError.retryable is True
    for permanent in (
        InvalidCredentialsError,
        TokenRateLimitedError,
        TokenRejectedRecentlyError,
        MissingCredentialsError,
        TokenStoreError,
    ):
        assert permanent.retryable is False, f"{permanent.__name__} must not be retryable"
        assert not issubclass(permanent, TokenGenerationError), (
            f"{permanent.__name__} must not inherit from the retryable class, so that "
            "except-clause ordering cannot change its meaning"
        )


# ------------------------------------------------------------- validation


def test_validation_uses_a_read_only_get_and_never_an_order_endpoint():
    recorder = _Recorder(httpx.Response(200, json={"dhanClientId": CLIENT_ID}))
    assert validate_token(CLIENT_ID, _JWT, http_get=recorder) is True
    url = str(recorder.calls[0]["url"])
    assert url == "https://api.dhan.co/v2/profile"
    assert "order" not in url
    headers = recorder.calls[0]["headers"]
    assert headers == {"access-token": _JWT, "dhanClientId": CLIENT_ID}


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_token_validates_false(status: int):
    recorder = _Recorder(httpx.Response(status, json={"errorMessage": "invalid token"}))
    assert validate_token(CLIENT_ID, _JWT, http_get=recorder) is False


def test_an_inconclusive_validation_raises_rather_than_reporting_invalid():
    """A network blip must not be reported as "token invalid".

    Reporting invalid would trigger a regeneration against the rate limit for
    what may be a perfectly good token.
    """
    recorder = _Recorder(httpx.Response(502, text="bad gateway"))
    with pytest.raises(TokenGenerationError, match="inconclusive"):
        validate_token(CLIENT_ID, _JWT, http_get=recorder)


def test_a_network_failure_during_validation_is_not_reported_as_invalid():
    recorder = _Recorder(httpx.ConnectTimeout("timed out"))
    with pytest.raises(TokenGenerationError):
        validate_token(CLIENT_ID, _JWT, http_get=recorder)
