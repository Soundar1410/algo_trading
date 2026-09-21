"""Phase 1. :meth:`~common.market_data.dhan_historical.DhanHistoricalDataClient.
fetch_daily` -- ``POST /v2/charts/historical``, no network (injectable
``http_post``).

Every assertion about the wire contract here was established against a **real
call on 2026-09-21**, not read off documentation:

* the body shape comes from the installed SDK's own
  ``_historical_data.py:historical_daily_data``;
* bare ``"YYYY-MM-DD"`` dates are accepted here, unlike ``/charts/intraday``;
* **``toDate`` is exclusive** -- ``2024-12-23 -> 2024-12-31`` returned its last
  session on 2024-12-30 although 2024-12-31 traded, while ``-> 2025-01-01`` (a
  holiday) returned 2024-12-31. ``fetch_daily`` therefore takes an inclusive
  ``to_date`` and adds the day itself, and
  ``test_to_date_is_sent_exclusive_because_dhans_own_to_date_is`` is what stops
  that silently regressing into an off-by-one that fails spec 6.2's staleness
  check closed.

Retry, backoff and classification are shared with ``fetch_intraday`` via
``_post_with_retries`` and pinned in
``test_dhan_historical_client_characterisation.py``; what is re-checked here is
that ``fetch_daily`` actually routes through that shared policy rather than
growing its own.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import httpx
import pytest

from common.market_data.dhan_historical import (
    HISTORICAL_ENDPOINT,
    INTRADAY_ENDPOINT,
    DhanHistoricalDataClient,
    HistoricalDataRejectedError,
    HistoricalDataTransientError,
)

_BARE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REQUEST = httpx.Request("POST", HISTORICAL_ENDPOINT)


class _RecordingPost:
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


def _ok(**arrays: list[Any]) -> httpx.Response:
    payload = arrays or {
        "open": [1400.0],
        "high": [1410.0],
        "low": [1390.0],
        "close": [1405.0],
        "volume": [1000],
        "timestamp": [1727740800],
    }
    return httpx.Response(200, json=payload, request=_REQUEST)


def _fail(status: int, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body or {"errorMessage": "boom"}, request=_REQUEST)


def _client(post: _RecordingPost, **kwargs: Any) -> DhanHistoricalDataClient:
    return DhanHistoricalDataClient(
        "client-1", "token-1", http_post=post, sleep=lambda _s: None, **kwargs
    )


def _fetch(client: DhanHistoricalDataClient, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "security_id": "2885",
        "exchange_segment": "NSE_EQ",
        "instrument_type": "EQUITY",
        "from_date": date(2024, 9, 1),
        "to_date": date(2024, 12, 31),
    }
    kwargs.update(overrides)
    return client.fetch_daily(**kwargs)


# ------------------------------------------------------------------ request shape
def test_fetch_daily_posts_to_the_historical_endpoint_not_the_intraday_one() -> None:
    post = _RecordingPost([_ok()])
    _fetch(_client(post))

    assert post.calls[0]["url"] == HISTORICAL_ENDPOINT
    assert post.calls[0]["url"] == "https://api.dhan.co/v2/charts/historical"
    assert post.calls[0]["url"] != INTRADAY_ENDPOINT


def test_fetch_daily_builds_the_sdks_documented_request_shape() -> None:
    """Key-for-key against ``_historical_data.py:historical_daily_data``, plus
    the ``dhanClientId`` the SDK's ``dhan_http.py`` injects into every POST."""
    post = _RecordingPost([_ok()])
    _fetch(_client(post), security_id=2885)

    body = post.calls[0]["json"]
    assert body["securityId"] == "2885", "security_id is coerced to a string"
    assert body["exchangeSegment"] == "NSE_EQ"
    assert body["instrument"] == "EQUITY"
    assert body["expiryCode"] == 0
    assert body["oi"] is False
    assert body["dhanClientId"] == "client-1"
    assert set(body) == {
        "securityId",
        "exchangeSegment",
        "instrument",
        "expiryCode",
        "oi",
        "fromDate",
        "toDate",
        "dhanClientId",
    }


def test_fetch_daily_sends_bare_dates_not_the_intraday_datetime_format() -> None:
    post = _RecordingPost([_ok()])
    _fetch(_client(post), from_date=date(2024, 9, 1), to_date=date(2024, 12, 30))

    body = post.calls[0]["json"]
    assert body["fromDate"] == "2024-09-01"
    assert _BARE_DATE_RE.match(body["fromDate"]), "this endpoint takes a bare date"
    assert _BARE_DATE_RE.match(body["toDate"]), "this endpoint takes a bare date"


def test_to_date_is_sent_exclusive_because_dhans_own_to_date_is() -> None:
    """The caller's ``to_date`` is inclusive; the wire value is one day later.

    Verified live: ``-> 2024-12-31`` stopped at 2024-12-30 (a trading day),
    ``-> 2025-01-01`` (a holiday) reached 2024-12-31. Asking for a week's last
    session inclusively must actually return it, or spec 6.2's staleness check
    rejects a perfectly current series.
    """
    post = _RecordingPost([_ok()])
    _fetch(_client(post), from_date=date(2024, 12, 23), to_date=date(2024, 12, 31))

    body = post.calls[0]["json"]
    assert body["fromDate"] == "2024-12-23", "from_date is sent as given"
    assert body["toDate"] == "2025-01-01", "to_date is sent as the day after the one wanted"


def test_a_single_day_range_is_still_a_valid_request() -> None:
    post = _RecordingPost([_ok()])
    _fetch(_client(post), from_date=date(2026, 9, 18), to_date=date(2026, 9, 18))

    body = post.calls[0]["json"]
    assert body["fromDate"] == "2026-09-18"
    assert body["toDate"] == "2026-09-19"


def test_to_date_before_from_date_is_refused_without_a_request() -> None:
    post = _RecordingPost([])
    client = _client(post)

    with pytest.raises(ValueError, match="precedes"):
        _fetch(client, from_date=date(2026, 9, 18), to_date=date(2026, 9, 17))

    assert post.calls == [], "a nonsensical range must not reach the network"
    assert client.request_count == 0


def test_a_month_end_to_date_rolls_into_the_next_month() -> None:
    post = _RecordingPost([_ok()])
    _fetch(_client(post), from_date=date(2025, 8, 1), to_date=date(2025, 8, 31))

    assert post.calls[0]["json"]["toDate"] == "2025-09-01"


def test_expiry_code_is_configurable_but_defaults_to_zero() -> None:
    post = _RecordingPost([_ok(), _ok()])
    client = _client(post)

    _fetch(client)
    assert post.calls[0]["json"]["expiryCode"] == 0

    _fetch(client, expiry_code=2)
    assert post.calls[1]["json"]["expiryCode"] == 2


def test_fetch_daily_sends_the_same_auth_headers_as_fetch_intraday() -> None:
    post = _RecordingPost([_ok()])
    _fetch(_client(post))

    headers = post.calls[0]["headers"]
    assert headers["access-token"] == "token-1"
    assert headers["client-id"] == "client-1"
    assert headers["Content-Type"] == "application/json"
    assert "dhanClientId" not in headers, "that belongs in the body -- known limitation 19"


def test_the_index_path_uses_idx_i_and_index() -> None:
    """NIFTY 50 is fetched through the same method, on its own segment."""
    post = _RecordingPost([_ok()])
    _fetch(
        _client(post),
        security_id="13",
        exchange_segment="IDX_I",
        instrument_type="INDEX",
    )

    body = post.calls[0]["json"]
    assert (body["securityId"], body["exchangeSegment"], body["instrument"]) == (
        "13",
        "IDX_I",
        "INDEX",
    )


# --------------------------------------------------------------------- response
def test_the_success_body_is_returned_unexamined() -> None:
    payload = {
        "open": [1400.0],
        "high": [1410.0],
        "low": [1390.0],
        "close": [1405.0],
        "volume": [1000],
        "timestamp": [1727740800],
    }
    post = _RecordingPost([_ok(**payload)])
    client = _client(post)

    assert _fetch(client) == payload
    assert client.request_count == 1


# -------------------------------------------------- shared retry/classify policy
def test_a_transient_failure_is_retried_on_this_endpoint_too() -> None:
    post = _RecordingPost([_fail(500), _fail(429), _ok()])
    client = _client(post, max_attempts=3)

    assert "close" in _fetch(client)
    assert len(post.calls) == 3
    assert client.request_count == 3
    assert {call["url"] for call in post.calls} == {HISTORICAL_ENDPOINT}


def test_a_network_error_is_transient_here_as_well() -> None:
    post = _RecordingPost([httpx.ConnectError("boom"), _ok()])
    client = _client(post, max_attempts=2)

    assert "close" in _fetch(client)


def test_exhausting_retries_raises_transient() -> None:
    post = _RecordingPost([_fail(503), _fail(503)])
    client = _client(post, max_attempts=2)

    with pytest.raises(HistoricalDataTransientError):
        _fetch(client)

    assert len(post.calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403])
def test_a_permanent_rejection_is_not_retried(status: int) -> None:
    """On this endpoint a 401/403 is also how "the Data API subscription is not
    active" arrives. Retrying it cannot help and risks the account's limits."""
    post = _RecordingPost([_fail(status)])
    client = _client(post, max_attempts=5)

    with pytest.raises(HistoricalDataRejectedError):
        _fetch(client)

    assert len(post.calls) == 1


def test_the_backoff_schedule_is_the_shared_one() -> None:
    delays: list[float] = []
    post = _RecordingPost([_fail(500), _fail(500), _ok()])
    client = DhanHistoricalDataClient(
        "client-1", "token-1", http_post=post, sleep=delays.append, max_attempts=3
    )

    _fetch(client)

    assert delays == [1.0, 2.0]


def test_request_count_is_shared_across_both_endpoints() -> None:
    """One budget, one counter -- the two fetches are not accounted separately."""
    post = _RecordingPost([_ok(), _ok()])
    client = _client(post)

    _fetch(client)
    client.fetch_intraday(
        security_id="13",
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        from_at=datetime(2026, 9, 18, 9, 15),
        to_at=datetime(2026, 9, 18, 15, 30),
    )

    assert client.request_count == 2
    assert post.calls[0]["url"] == HISTORICAL_ENDPOINT
    assert post.calls[1]["url"] == INTRADAY_ENDPOINT


# ----------------------------------------------------------- before_request hook
def test_before_request_fires_once_per_attempt_including_retries() -> None:
    """Where the 3 req/s throttle plugs in. A throttle wrapped around
    ``fetch_daily`` instead would not see the retries -- and a retry burst is
    exactly what a 429 provokes."""
    ticks: list[int] = []
    post = _RecordingPost([_fail(429), _fail(429), _ok()])
    client = _client(post, max_attempts=3, before_request=lambda: ticks.append(1))

    _fetch(client)

    assert len(ticks) == 3, "one throttle tick per HTTP attempt, retries included"


def test_before_request_fires_before_the_post_not_after() -> None:
    order: list[str] = []
    post = _RecordingPost([_ok()])

    def recording_post(*args: Any, **kwargs: Any) -> httpx.Response:
        order.append("post")
        return post(*args, **kwargs)

    client = DhanHistoricalDataClient(
        "client-1",
        "token-1",
        http_post=recording_post,
        sleep=lambda _s: None,
        before_request=lambda: order.append("throttle"),
    )
    _fetch(client)

    assert order == ["throttle", "post"]


def test_before_request_defaults_to_none_so_the_warmup_path_is_unchanged() -> None:
    """The five running paper strategies share this client. Their behaviour
    must not depend on a hook they never pass."""
    post = _RecordingPost([_ok()])
    client = _client(post)

    _fetch(client)

    assert client._before_request is None
    assert len(post.calls) == 1


def test_before_request_also_fires_on_the_intraday_path_when_supplied() -> None:
    ticks: list[int] = []
    post = _RecordingPost([_ok()])
    client = _client(post, before_request=lambda: ticks.append(1))

    client.fetch_intraday(
        security_id="13",
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        from_at=datetime(2026, 9, 18, 9, 15),
        to_at=datetime(2026, 9, 18, 15, 30),
    )

    assert ticks == [1]
