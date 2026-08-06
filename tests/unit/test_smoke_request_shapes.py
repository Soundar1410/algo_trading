"""Request-shape regression test for a test-only smoke-suite helper.

``tests/smoke/test_live_feed_smoke.py`` is opt-in (``ALGO_LIVE_SMOKE=1``): a
request-shape bug in its ``_index_last_price`` helper -- known limitation 19 --
was never caught by the default suite, because nothing in the default suite
ever called it. This test imports the helper directly, with an injected token
and a fake ``http_post`` (see the helper's own docstring), so the request it
builds can be asserted without any credential or network call, runs by
default, and would have caught the bug: it fails against the old shape
(``dhanClientId`` sent as a header) and passes against the corrected one
(``client-id`` header, ``dhanClientId`` in the JSON body) -- matching the
installed SDK's own ``dhan_http.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx

from tests.smoke.test_live_feed_smoke import _CLIENT_ID, _index_last_price


class _RecordingPost:
    """A stub http_post that records the one call made and replays a fixed response."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, *, headers: dict[str, Any], json: dict[str, Any], timeout: float
    ) -> httpx.Response:
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return self._response


def _ltp_response(segment: str, security_id: str, last_price: float) -> httpx.Response:
    payload = {"data": {segment: {security_id: {"last_price": last_price}}}}
    return httpx.Response(
        200, json=payload, request=httpx.Request("POST", "https://api.dhan.co/v2/marketfeed/ltp")
    )


def test_index_last_price_builds_the_documented_request_shape() -> None:
    meta = SimpleNamespace(segment="IDX_I", security_id="13")
    post = _RecordingPost(_ltp_response("IDX_I", "13", 24500.0))

    price = _index_last_price(meta, token="fake-token", http_post=post)

    assert price == 24500.0
    assert len(post.calls) == 1
    call = post.calls[0]
    assert call["url"] == "https://api.dhan.co/v2/marketfeed/ltp"
    assert call["headers"]["access-token"] == "fake-token"
    assert call["headers"]["client-id"] == str(_CLIENT_ID)
    assert "dhanClientId" not in call["headers"], (
        "dhanClientId belongs in the JSON body, not a header -- see known limitation 19"
    )
    assert call["json"]["IDX_I"] == [13]
    assert call["json"]["dhanClientId"] == str(_CLIENT_ID), (
        "dhanClientId is missing from the JSON body -- see known limitation 19"
    )
