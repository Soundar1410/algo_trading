"""Opt-in live-feed smoke test. **Skipped by default.**

This is the only test that touches the network, and it runs only when both
conditions hold:

* ``ALGO_LIVE_SMOKE=1`` is set explicitly, and
* real Dhan credentials are present in the environment.

It is **read-only**: it subscribes to one instrument and asserts that a tick
arrives. It places no order, and there is no code path from here to a broker.

It also requires market hours to pass meaningfully. Failing outside them is not
a defect in the platform, which is exactly why it is not part of the default
run — a test that fails at 21:00 for correct reasons trains people to ignore
failures.

Note that Phase 1 has no authentication bootstrap: the access token must be
supplied directly via ``DHAN_ACCESS_TOKEN``. Token generation and the atomic
cache are Phase 2.
"""

from __future__ import annotations

import os
import threading

import pytest

from common.models import Tick

_ENABLED = os.environ.get("ALGO_LIVE_SMOKE") == "1"
_CLIENT_ID = os.environ.get("DHAN_CLIENT_ID")
_ACCESS_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN")

pytestmark = pytest.mark.skipif(
    not (_ENABLED and _CLIENT_ID and _ACCESS_TOKEN),
    reason=(
        "Live feed smoke test is opt-in: set ALGO_LIVE_SMOKE=1 with DHAN_CLIENT_ID "
        "and DHAN_ACCESS_TOKEN, and run during market hours."
    ),
)

#: NIFTY 50 index on the IDX segment.
_SECURITY_ID = os.environ.get("ALGO_SMOKE_SECURITY_ID", "13")
_SEGMENT = int(os.environ.get("ALGO_SMOKE_SEGMENT", "0"))
_TIMEOUT_SECONDS = 30.0


def test_one_live_tick_reaches_the_hub():
    """Read-only: subscribe, receive one tick, disconnect. No order is placed."""
    from common.market_data.dhan import DhanMarketFeedAdapter

    adapter = DhanMarketFeedAdapter(
        client_id=str(_CLIENT_ID),
        access_token=str(_ACCESS_TOKEN),
        exchange_segment=_SEGMENT,
        instrument_label="NIFTY",
    )
    adapter.subscribe([_SECURITY_ID])

    received: list[Tick] = []
    finished = threading.Event()

    def _collect(tick: Tick) -> None:
        received.append(tick)
        finished.set()
        adapter.stop()

    thread = threading.Thread(target=lambda: adapter.start(_collect), daemon=True)
    thread.start()

    finished.wait(timeout=_TIMEOUT_SECONDS)
    adapter.stop()
    thread.join(timeout=5.0)

    assert received, (
        f"No tick within {_TIMEOUT_SECONDS}s. Check market hours, the security id "
        f"({_SECURITY_ID}) and token validity."
    )
    tick = received[0]
    assert tick.last_price > 0
    assert tick.security_id
    assert tick.exchange_time.tzinfo is not None


def test_the_live_adapter_refuses_to_start_without_a_subscription():
    """Runs whenever the smoke suite is enabled; needs no market data."""
    from common.market_data.dhan import DhanFeedError, DhanMarketFeedAdapter

    adapter = DhanMarketFeedAdapter(client_id=str(_CLIENT_ID), access_token=str(_ACCESS_TOKEN))
    with pytest.raises(DhanFeedError, match="no subscriptions"):
        adapter.start(lambda tick: None)
