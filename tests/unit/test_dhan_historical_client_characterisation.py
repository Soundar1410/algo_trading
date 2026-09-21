"""Characterisation tests for :class:`~common.market_data.dhan_historical.
DhanHistoricalDataClient` as it behaves *today*, written before the Phase 1
``fetch_daily`` refactor and passing against the unmodified module.

Why a second file rather than additions to ``test_dhan_historical_client.py``:
that file documents a sequence of *corrections* (the bare-date bug, known
limitation 19) and each of its tests is written to fail against a specific
superseded shape. This file has a different job — it pins behaviour nobody has
asserted yet, so that extracting the retry loop into a shared helper cannot
change it silently. The existing file is left untouched.

``fetch_intraday`` is on the warm-up path of five running paper strategies
(``c921``, ``c509``, ``c521``, ``st05``, ``st12``), and the 2026-07-17 incident
— manufactured SuperTrend flips from truncated warm-up data — is what a
regression in this retry loop looks like from the outside. The gaps pinned here
are the ones a refactor could plausibly walk through:

* the **backoff schedule** (``initial_backoff * multiplier ** (attempt - 1)``).
  Every existing test stubs ``sleep`` with a no-op lambda that discards its
  argument, so the delays were entirely unobserved;
* that **no sleep follows the final attempt** — the ``attempt < max_attempts``
  guard, which is the difference between a bounded retry and a pointless
  trailing delay on every exhausted fetch;
* the **endpoint URL** and the **timeout passthrough**, both recorded by the
  existing stub and then never asserted. A refactor that introduces a second
  endpoint is exactly where these stop being obvious;
* ``_classify``'s **error-message extraction** — key precedence, the
  ``response.text`` fallback, and the ``"no response body"`` floor;
* ``_safe_json``'s **non-dict and non-JSON** handling on a 200;
* ``max_attempts``' **floor of 1**, and that ``request_count`` accumulates
  across calls rather than resetting per call.

None of these assertions encodes a judgement about whether the current
behaviour is *right* — that is what makes them characterisation tests. They
record what is, so a change to it has to be deliberate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import pytest

from common.market_data.dhan_historical import (
    DEFAULT_TIMEOUT_SECONDS,
    INTRADAY_ENDPOINT,
    DhanHistoricalDataClient,
    HistoricalDataError,
    HistoricalDataRejectedError,
    HistoricalDataTransientError,
)

_REQUEST = httpx.Request("POST", INTRADAY_ENDPOINT)


class _RecordingPost:
    """Records every call and replays scripted responses, like the sibling file's."""

    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, *, json: dict[str, Any], headers: dict[str, Any], timeout: float
    ) -> httpx.Response:
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _RecordingSleep:
    """A sleep stub that keeps its arguments, which the existing tests discard."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _ok(**arrays: list[Any]) -> httpx.Response:
    return httpx.Response(200, json=arrays or {"close": [100.5]}, request=_REQUEST)


def _fail(status: int, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body or {"errorMessage": "boom"}, request=_REQUEST)


def _fail_text(status: int, text: str) -> httpx.Response:
    return httpx.Response(status, text=text, request=_REQUEST)


def _client(
    post: _RecordingPost, sleep: _RecordingSleep | None = None, **kwargs: Any
) -> DhanHistoricalDataClient:
    return DhanHistoricalDataClient(
        "client-1", "token-1", http_post=post, sleep=sleep or _RecordingSleep(), **kwargs
    )


def _fetch(client: DhanHistoricalDataClient) -> dict[str, Any]:
    return client.fetch_intraday(
        security_id="13",
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        from_at=datetime(2026, 7, 30, 9, 15, 0),
        to_at=datetime(2026, 8, 3, 15, 30, 0),
    )


# --------------------------------------------------------------- backoff schedule
def test_backoff_delays_follow_the_documented_geometric_schedule() -> None:
    """``initial_backoff * multiplier ** (attempt - 1)``, pinned by value.

    Unobserved until now: every existing test passes ``sleep=lambda _s: None``.
    """
    sleep = _RecordingSleep()
    post = _RecordingPost([_fail(500), _fail(500), _fail(500), _ok()])
    client = _client(post, sleep, max_attempts=4, initial_backoff=0.5, backoff_multiplier=3.0)

    _fetch(client)

    assert sleep.delays == [0.5, 1.5, 4.5]


def test_default_backoff_schedule_is_one_then_two_seconds() -> None:
    """The shipped defaults, spelled out: 1.0s then 2.0s across three attempts."""
    sleep = _RecordingSleep()
    post = _RecordingPost([_fail(500), _fail(429), _ok()])
    client = _client(post, sleep)  # DEFAULT_MAX_ATTEMPTS=3, 1.0s, x2.0

    _fetch(client)

    assert sleep.delays == [1.0, 2.0]


def test_no_sleep_follows_the_final_attempt() -> None:
    """The ``attempt < max_attempts`` guard: an exhausted fetch raises without
    first waiting out a delay nobody is going to use."""
    sleep = _RecordingSleep()
    post = _RecordingPost([_fail(500), _fail(500), _fail(500)])
    client = _client(post, sleep, max_attempts=3)

    with pytest.raises(HistoricalDataTransientError):
        _fetch(client)

    assert len(post.calls) == 3
    assert sleep.delays == [1.0, 2.0], "a third delay would be waited out for nothing"


def test_a_permanent_rejection_never_sleeps() -> None:
    sleep = _RecordingSleep()
    post = _RecordingPost([_fail(401)])
    client = _client(post, sleep, max_attempts=5)

    with pytest.raises(HistoricalDataRejectedError):
        _fetch(client)

    assert sleep.delays == []


def test_a_successful_first_attempt_never_sleeps() -> None:
    sleep = _RecordingSleep()
    post = _RecordingPost([_ok()])
    client = _client(post, sleep)

    _fetch(client)

    assert sleep.delays == []


# ------------------------------------------------------------ transport arguments
def test_the_intraday_endpoint_url_is_the_one_posted_to() -> None:
    """Recorded by the existing stub, asserted by nothing until now. A second
    endpoint arriving on this client is exactly when that stops being safe."""
    post = _RecordingPost([_ok()])
    _fetch(_client(post))

    assert post.calls[0]["url"] == INTRADAY_ENDPOINT
    assert post.calls[0]["url"] == "https://api.dhan.co/v2/charts/intraday"


def test_the_timeout_is_passed_through_to_every_attempt() -> None:
    post = _RecordingPost([_fail(500), _fail(500), _ok()])
    client = _client(post, max_attempts=3, timeout=7.5)

    _fetch(client)

    assert [call["timeout"] for call in post.calls] == [7.5, 7.5, 7.5]


def test_the_default_timeout_is_fifteen_seconds() -> None:
    post = _RecordingPost([_ok()])
    _fetch(_client(post))

    assert post.calls[0]["timeout"] == DEFAULT_TIMEOUT_SECONDS == 15.0


def test_every_attempt_repeats_the_identical_body_and_headers() -> None:
    """A retry re-sends the same request; it does not rebuild or mutate it."""
    post = _RecordingPost([_fail(500), _fail(500), _ok()])
    client = _client(post, max_attempts=3)

    _fetch(client)

    bodies = [call["json"] for call in post.calls]
    headers = [call["headers"] for call in post.calls]
    assert bodies[0] == bodies[1] == bodies[2]
    assert headers[0] == headers[1] == headers[2]


def test_content_type_header_is_sent() -> None:
    post = _RecordingPost([_ok()])
    _fetch(_client(post))

    assert post.calls[0]["headers"]["Content-Type"] == "application/json"


# ------------------------------------------------------------- request accounting
def test_request_count_accumulates_across_calls_and_does_not_reset() -> None:
    """Documented as "every request this instance has made, across every call
    and every retry" — pinned here because the refactor moves the increment."""
    post = _RecordingPost([_fail(500), _ok(), _ok()])
    client = _client(post, max_attempts=2)

    _fetch(client)
    assert client.request_count == 2

    _fetch(client)
    assert client.request_count == 3


def test_request_count_counts_attempts_that_raised_a_network_error() -> None:
    post = _RecordingPost([httpx.ConnectError("boom"), _ok()])
    client = _client(post, max_attempts=2)

    _fetch(client)

    assert client.request_count == 2


# ------------------------------------------------------------------ max_attempts
def test_max_attempts_is_floored_at_one() -> None:
    """``max(1, int(max_attempts))`` — zero or negative still makes one attempt
    rather than none, so a misconfiguration cannot turn a fetch into a silent
    no-op returning nothing."""
    post = _RecordingPost([_fail(500)])
    client = _client(post, max_attempts=0)

    with pytest.raises(HistoricalDataTransientError):
        _fetch(client)

    assert len(post.calls) == 1


def test_max_attempts_of_one_makes_exactly_one_attempt_and_never_sleeps() -> None:
    sleep = _RecordingSleep()
    post = _RecordingPost([_fail(503)])
    client = _client(post, sleep, max_attempts=1)

    with pytest.raises(HistoricalDataTransientError):
        _fetch(client)

    assert len(post.calls) == 1
    assert sleep.delays == []


# ------------------------------------------------------- classification taxonomy
@pytest.mark.parametrize("status", [404, 418, 500, 502, 503, 429])
def test_every_status_outside_the_permanent_set_is_transient(status: int) -> None:
    """The module errs toward retrying an unrecognised status rather than
    discarding a request that might have succeeded. 404 and 418 are in here
    deliberately: neither is 5xx, and both are currently retried."""
    post = _RecordingPost([_fail(status), _fail(status), _ok()])
    client = _client(post, max_attempts=3)

    _fetch(client)

    assert len(post.calls) == 3


@pytest.mark.parametrize("status", [400, 401, 403])
def test_the_permanent_set_is_exactly_400_401_403(status: int) -> None:
    post = _RecordingPost([_fail(status)])
    client = _client(post, max_attempts=5)

    with pytest.raises(HistoricalDataRejectedError):
        _fetch(client)

    assert len(post.calls) == 1


def test_rejected_and_transient_share_one_base_class() -> None:
    """A caller that wants "any failure from this client" has one name for it."""
    assert issubclass(HistoricalDataRejectedError, HistoricalDataError)
    assert issubclass(HistoricalDataTransientError, HistoricalDataError)
    assert not issubclass(HistoricalDataRejectedError, HistoricalDataTransientError)


def test_the_error_raised_after_exhaustion_is_the_last_failure_not_the_first() -> None:
    post = _RecordingPost([_fail(500, {"errorMessage": "first"}), _fail(503, {"message": "last"})])
    client = _client(post, max_attempts=2)

    with pytest.raises(HistoricalDataTransientError) as caught:
        _fetch(client)

    assert "last" in str(caught.value)
    assert "first" not in str(caught.value)


# --------------------------------------------------------- error-message extraction
@pytest.mark.parametrize(
    "body,expected",
    [
        ({"errorMessage": "em", "message": "m", "error": "e"}, "em"),
        ({"message": "m", "error": "e", "remarks": "r"}, "m"),
        ({"error": "e", "remarks": "r"}, "e"),
        ({"remarks": "r", "status": "s"}, "r"),
        ({"status": "s"}, "s"),
    ],
)
def test_error_message_key_precedence(body: dict[str, Any], expected: str) -> None:
    """errorMessage > message > error > remarks > status, first non-empty string."""
    post = _RecordingPost([_fail(500, body)])
    client = _client(post, max_attempts=1)

    with pytest.raises(HistoricalDataTransientError) as caught:
        _fetch(client)

    assert expected in str(caught.value)


def test_non_string_and_empty_values_are_skipped_when_choosing_a_message() -> None:
    post = _RecordingPost([_fail(500, {"errorMessage": "", "message": 42, "error": "picked"})])
    client = _client(post, max_attempts=1)

    with pytest.raises(HistoricalDataTransientError) as caught:
        _fetch(client)

    assert "picked" in str(caught.value)


def test_a_body_with_no_recognised_key_falls_back_to_the_raw_text() -> None:
    post = _RecordingPost([_fail_text(500, "  upstream exploded  ")])
    client = _client(post, max_attempts=1)

    with pytest.raises(HistoricalDataTransientError) as caught:
        _fetch(client)

    assert "upstream exploded" in str(caught.value)


def test_an_empty_body_falls_back_to_no_response_body() -> None:
    post = _RecordingPost([_fail_text(503, "")])
    client = _client(post, max_attempts=1)

    with pytest.raises(HistoricalDataTransientError) as caught:
        _fetch(client)

    assert "no response body" in str(caught.value)


def test_the_status_code_appears_in_both_failure_messages() -> None:
    transient = _RecordingPost([_fail(502)])
    with pytest.raises(HistoricalDataTransientError) as t:
        _fetch(_client(transient, max_attempts=1))
    assert "502" in str(t.value)

    rejected = _RecordingPost([_fail(403)])
    with pytest.raises(HistoricalDataRejectedError) as r:
        _fetch(_client(rejected, max_attempts=1))
    assert "403" in str(r.value)


# ------------------------------------------------------------- success-body parsing
def test_a_200_whose_body_is_not_json_yields_an_empty_dict() -> None:
    """``_safe_json`` swallows the ValueError. The caller
    (``parse_intraday_response``) is what raises on a shape it cannot use, so a
    garbled 200 degrades to a safe cold start rather than a crash here."""
    post = _RecordingPost([httpx.Response(200, text="<html>nope</html>", request=_REQUEST)])
    client = _client(post)

    assert _fetch(client) == {}


def test_a_200_whose_body_is_a_json_list_yields_an_empty_dict() -> None:
    post = _RecordingPost([httpx.Response(200, json=[1, 2, 3], request=_REQUEST)])
    client = _client(post)

    assert _fetch(client) == {}


def test_a_200_body_is_returned_unexamined_including_an_embedded_failure_status() -> None:
    """The client does not inspect a 200 body at all — not even for the SDK's
    own ``{"status": "failure"}`` shape. That judgement belongs to the parser,
    and pinning it here means the refactor cannot quietly add validation."""
    payload = {"status": "failure", "remarks": "whatever", "data": ""}
    post = _RecordingPost([httpx.Response(200, json=payload, request=_REQUEST)])
    client = _client(post)

    assert _fetch(client) == payload
