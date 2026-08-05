"""Phase 4 Part 4. :class:`~common.market_data.dhan_historical.
DhanHistoricalDataClient` -- the REST client, no network (injectable
``http_post``), no ``dhanhq`` import anywhere.

``test_fetch_intraday_builds_the_documented_from_and_to_date_format`` is the
primary fail-first demonstration named in the task: the reference
implementation this module started from passed bare ``"YYYY-MM-DD"`` strings,
which Dhan's own documentation does not support for this endpoint (it wants a
full datetime). Written to fail against a naive
``from_at.strftime("%Y-%m-%d")`` port, and to pass against the corrected
``"%Y-%m-%d %H:%M:%S"`` format.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx
import pytest

from common.market_data.dhan_historical import (
    DhanHistoricalDataClient,
    HistoricalDataRejectedError,
    HistoricalDataTransientError,
)

_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class _RecordingPost:
    """A stub http_post that records every call and replays scripted responses."""

    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, *, json: dict[str, Any], headers: dict[str, Any], timeout: float):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _success_response(**arrays: list[Any]) -> httpx.Response:
    payload = arrays or {
        "open": [100.0],
        "high": [101.0],
        "low": [99.0],
        "close": [100.5],
        "volume": [10],
        "timestamp": [1735700100],
    }
    return httpx.Response(
        200, json=payload, request=httpx.Request("POST", "https://api.dhan.co/v2/charts/intraday")
    )


def _failure_response(status: int, body: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=body or {"errorMessage": "boom"},
        request=httpx.Request("POST", "https://api.dhan.co/v2/charts/intraday"),
    )


def _client(post: _RecordingPost, **kwargs: Any) -> DhanHistoricalDataClient:
    return DhanHistoricalDataClient(
        "client-1", "token-1", http_post=post, sleep=lambda _seconds: None, **kwargs
    )


def test_fetch_intraday_builds_the_documented_request_shape() -> None:
    post = _RecordingPost([_success_response()])
    client = _client(post)
    client.fetch_intraday(
        security_id="13",
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        from_at=datetime(2026, 7, 30, 9, 15, 0),
        to_at=datetime(2026, 8, 3, 9, 30, 0),
        interval_minutes=1,
    )
    assert len(post.calls) == 1
    body = post.calls[0]["json"]
    assert body["securityId"] == "13"
    assert body["exchangeSegment"] == "IDX_I"
    assert body["instrument"] == "INDEX"
    assert body["interval"] == 1
    assert set(body) == {
        "securityId",
        "exchangeSegment",
        "instrument",
        "interval",
        "fromDate",
        "toDate",
    }


def test_fetch_intraday_builds_the_documented_from_and_to_date_format() -> None:
    post = _RecordingPost([_success_response()])
    client = _client(post)
    client.fetch_intraday(
        security_id="13",
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        from_at=datetime(2026, 7, 30, 9, 15, 0),
        to_at=datetime(2026, 8, 3, 14, 47, 22),
    )
    body = post.calls[0]["json"]
    assert body["fromDate"] == "2026-07-30 09:15:00"
    assert body["toDate"] == "2026-08-03 14:47:22"
    assert _DATETIME_RE.match(body["fromDate"]), "fromDate must be a full datetime, not a bare date"
    assert _DATETIME_RE.match(body["toDate"]), "toDate must be a full datetime, not a bare date"


def test_fetch_intraday_sends_the_documented_auth_headers() -> None:
    post = _RecordingPost([_success_response()])
    client = _client(post)
    client.fetch_intraday(
        security_id="13",
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        from_at=datetime(2026, 7, 30),
        to_at=datetime(2026, 8, 3),
    )
    headers = post.calls[0]["headers"]
    assert headers["access-token"] == "token-1"
    assert headers["dhanClientId"] == "client-1"


def test_success_response_is_returned_as_is() -> None:
    payload = {
        "open": [1.0],
        "high": [2.0],
        "low": [0.5],
        "close": [1.5],
        "volume": [10],
        "timestamp": [1735700100],
    }
    post = _RecordingPost([_success_response(**payload)])
    client = _client(post)
    result = client.fetch_intraday(
        security_id="13",
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        from_at=datetime(2026, 7, 30),
        to_at=datetime(2026, 8, 3),
    )
    assert result == payload
    assert client.request_count == 1


def test_transient_failure_is_retried_up_to_max_attempts() -> None:
    post = _RecordingPost([_failure_response(500), _failure_response(429), _success_response()])
    client = _client(post, max_attempts=3)
    result = client.fetch_intraday(
        security_id="13",
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        from_at=datetime(2026, 7, 30),
        to_at=datetime(2026, 8, 3),
    )
    assert "open" in result
    assert len(post.calls) == 3
    assert client.request_count == 3


def test_network_error_is_treated_as_transient_and_retried() -> None:
    post = _RecordingPost([httpx.ConnectError("boom"), _success_response()])
    client = _client(post, max_attempts=2)
    result = client.fetch_intraday(
        security_id="13",
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        from_at=datetime(2026, 7, 30),
        to_at=datetime(2026, 8, 3),
    )
    assert "open" in result


def test_transient_failure_exhausting_retries_raises() -> None:
    post = _RecordingPost([_failure_response(500), _failure_response(500), _failure_response(500)])
    client = _client(post, max_attempts=3)
    with pytest.raises(HistoricalDataTransientError):
        client.fetch_intraday(
            security_id="13",
            exchange_segment="IDX_I",
            instrument_type="INDEX",
            from_at=datetime(2026, 7, 30),
            to_at=datetime(2026, 8, 3),
        )
    assert len(post.calls) == 3


def test_credential_rejection_is_not_retried() -> None:
    post = _RecordingPost([_failure_response(401)])
    client = _client(post, max_attempts=5)
    with pytest.raises(HistoricalDataRejectedError):
        client.fetch_intraday(
            security_id="13",
            exchange_segment="IDX_I",
            instrument_type="INDEX",
            from_at=datetime(2026, 7, 30),
            to_at=datetime(2026, 8, 3),
        )
    assert len(post.calls) == 1  # no retry burned against a permanent rejection


@pytest.mark.parametrize("status", [400, 403])
def test_other_permanent_statuses_are_not_retried(status: int) -> None:
    post = _RecordingPost([_failure_response(status)])
    client = _client(post, max_attempts=5)
    with pytest.raises(HistoricalDataRejectedError):
        client.fetch_intraday(
            security_id="13",
            exchange_segment="IDX_I",
            instrument_type="INDEX",
            from_at=datetime(2026, 7, 30),
            to_at=datetime(2026, 8, 3),
        )
    assert len(post.calls) == 1


def test_client_id_or_access_token_missing_raises_at_construction() -> None:
    with pytest.raises(ValueError):
        DhanHistoricalDataClient("", "token-1")
    with pytest.raises(ValueError):
        DhanHistoricalDataClient("client-1", "")


def test_no_dhanhq_import_statement_in_this_module() -> None:
    """The module docstring names ``dhanhq`` by way of explanation (matching
    dhan_login.py's own docstring); what must never appear is an actual
    ``import``/``from`` statement. The repo-wide boundary test
    (tests/unit/test_dhan_adapter.py) enforces this via AST across every
    module in the tree -- this is a narrower, local sanity check.
    """
    import ast
    import inspect

    import common.market_data.dhan_historical as mod

    tree = ast.parse(inspect.getsource(mod))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(alias.name == "dhanhq" for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module != "dhanhq"
