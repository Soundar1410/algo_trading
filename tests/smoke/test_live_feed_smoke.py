"""Opt-in live-feed smoke test. **Skipped by default.**

This is the only test that touches the network, and it runs only when both hold:

* ``ALGO_LIVE_SMOKE=1`` is set explicitly, and
* real Dhan credentials are present in the environment.

It is **read-only**: it authenticates, subscribes to one instrument, and asserts
that a tick arrives. It places no order, and there is no code path from here to a
broker.

It also needs market hours to pass meaningfully. Failing outside them is not a
defect in the platform, which is exactly why it is not part of the default run —
a test that fails at 21:00 for correct reasons trains people to ignore failures.

Phase 2 change: the token now comes from the authentication bootstrap rather than
a hand-pasted ``DHAN_ACCESS_TOKEN``. That variable still works as an override, so
either path can be exercised.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from common.models import Tick

_ENABLED = os.environ.get("ALGO_LIVE_SMOKE") == "1"
_CLIENT_ID = os.environ.get("DHAN_CLIENT_ID")
_CAN_AUTHENTICATE = bool(
    _CLIENT_ID
    and (
        os.environ.get("DHAN_ACCESS_TOKEN")
        or (os.environ.get("DHAN_PIN") and os.environ.get("DHAN_TOTP_SECRET"))
    )
)

pytestmark = pytest.mark.skipif(
    not (_ENABLED and _CAN_AUTHENTICATE),
    reason=(
        "Live feed smoke test is opt-in: set ALGO_LIVE_SMOKE=1 with DHAN_CLIENT_ID "
        "plus either DHAN_ACCESS_TOKEN or DHAN_PIN + DHAN_TOTP_SECRET, and run "
        "during market hours."
    ),
)

#: NIFTY 50 index on the IDX segment.
_SECURITY_ID = os.environ.get("ALGO_SMOKE_SECURITY_ID", "13")
_SEGMENT = int(os.environ.get("ALGO_SMOKE_SEGMENT", "0"))
_TIMEOUT_SECONDS = 30.0


def _bootstrap(cache_dir: Path):  # type: ignore[no-untyped-def]
    from common.authentication import AuthBootstrap, AuthCredentials

    return AuthBootstrap(
        AuthCredentials(
            client_id=str(_CLIENT_ID),
            pin=os.environ.get("DHAN_PIN"),
            totp_secret=os.environ.get("DHAN_TOTP_SECRET"),
            access_token=os.environ.get("DHAN_ACCESS_TOKEN"),
        ),
        cache_dir=cache_dir,
    )


def _token(cache_dir: Path) -> str:
    """Obtain a token the way a real runtime does."""
    token, _outcome = _bootstrap(cache_dir).get_token()
    return token


def test_the_bootstrap_obtains_a_token_and_dhan_accepts_it(tmp_path: Path):
    """Auth plus the spec's safe read request. Places no order."""
    bootstrap = _bootstrap(tmp_path)
    token, outcome = bootstrap.get_token()

    assert token
    assert outcome.source in {"environment", "cache", "generated"}
    assert bootstrap.validate(token) is True, "Dhan rejected the token via GET /v2/profile"


def test_the_token_cache_is_written_atomically_and_privately(tmp_path: Path):
    """Ratifies the cache against a real token rather than a synthetic one."""
    import stat

    if not (os.environ.get("DHAN_PIN") and os.environ.get("DHAN_TOTP_SECRET")):
        pytest.skip("token generation requires DHAN_PIN and DHAN_TOTP_SECRET")

    bootstrap = _bootstrap(tmp_path)
    bootstrap.get_token()

    assert bootstrap.cache.path.exists()
    assert stat.S_IMODE(bootstrap.cache.path.stat().st_mode) == 0o600
    reloaded = bootstrap.cache.load(expected_client_id=str(_CLIENT_ID))
    assert reloaded is not None and reloaded.access_token


def test_one_live_tick_reaches_the_hub(tmp_path: Path):
    """Read-only: subscribe, receive one tick, disconnect. No order is placed."""
    from common.market_data.dhan import DhanMarketFeedAdapter

    adapter = DhanMarketFeedAdapter(
        client_id=str(_CLIENT_ID),
        access_token=_token(tmp_path),
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


def test_the_live_payload_matches_the_ratified_shape(tmp_path: Path):
    """The Block 2 ratification assertion.

    Phase 1 shipped normalisation written against an *unobserved* payload. This
    checks the reconstructed exchange timestamp against real data: a non-zero
    fallback count means Dhan's LTT format is not what the SDK source implies,
    and candles would be silently bucketed by arrival time instead.
    """
    from common.market_data.dhan import DhanMarketFeedAdapter

    adapter = DhanMarketFeedAdapter(
        client_id=str(_CLIENT_ID),
        access_token=_token(tmp_path),
        exchange_segment=_SEGMENT,
        instrument_label="NIFTY",
    )
    adapter.subscribe([_SECURITY_ID])

    collected: list[Tick] = []
    finished = threading.Event()

    def _collect(tick: Tick) -> None:
        collected.append(tick)
        if len(collected) >= 5:
            finished.set()
            adapter.stop()

    thread = threading.Thread(target=lambda: adapter.start(_collect), daemon=True)
    thread.start()
    finished.wait(timeout=_TIMEOUT_SECONDS)
    adapter.stop()
    thread.join(timeout=5.0)

    assert collected, "no ticks captured; cannot ratify the payload shape"
    assert adapter.counters.malformed_payloads == 0, (
        f"{adapter.counters.malformed_payloads} live frames failed normalisation"
    )
    assert adapter.counters.exchange_time_fallbacks == 0, (
        "every tick fell back to receipt time, so the LTT format is not what "
        "reconstruct_exchange_time expects — candles would be bucketed by arrival"
    )
    for tick in collected:
        assert tick.exchange_time <= tick.received_at, "exchange time cannot be in the future"


def test_the_option_chain_throttle_holds_against_the_real_endpoint(tmp_path: Path):
    """One read-only /optionchain call through the service. Places no order."""
    import httpx

    from common.market_data.option_chain import OptionChainService

    expiry = os.environ.get("ALGO_SMOKE_EXPIRY")
    if not expiry:
        pytest.skip("set ALGO_SMOKE_EXPIRY=YYYY-MM-DD to exercise the option chain")

    token = _token(tmp_path)
    client_id = str(_CLIENT_ID)

    def fetch(security_id: int, segment: str, chain_expiry: str) -> dict[str, object]:
        response = httpx.post(
            "https://api.dhan.co/v2/optionchain",
            headers={"access-token": token, "dhanClientId": client_id},
            json={
                "UnderlyingScrip": security_id,
                "UnderlyingSeg": segment,
                "Expiry": chain_expiry,
            },
            timeout=15.0,
        )
        response.raise_for_status()
        return dict(response.json())

    service = OptionChainService(fetch)
    first = service.get(13, "IDX_I", expiry)
    assert first.payload

    # A second immediate request must be served from cache, not the API.
    service.get(13, "IDX_I", expiry)
    assert service.stats.api_calls == 1, "the cache allowed a second live call"


def test_the_live_adapter_refuses_to_start_without_a_subscription():
    """Runs whenever the smoke suite is enabled; needs no market data."""
    from common.market_data.dhan import DhanFeedError, DhanMarketFeedAdapter

    adapter = DhanMarketFeedAdapter(client_id=str(_CLIENT_ID), access_token="unused-token")
    with pytest.raises(DhanFeedError, match="no subscriptions"):
        adapter.start(lambda tick: None)
