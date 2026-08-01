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

#: The opt-in gate every test here shares: nothing in this file touches the
#: network unless it is set.
pytestmark = pytest.mark.skipif(
    not _ENABLED,
    reason="Live smoke tests are opt-in: set ALGO_LIVE_SMOKE=1.",
)

#: The **additional** gate for tests that authenticate. Kept separate because not
#: every live test needs a credential: Dhan's instrument master is a public CSV,
#: so the scrip-master test below reaches the network and nothing else. Folding
#: it into the module gate — as this file did until Phase 4 Part 1 — made that
#: test unrunnable without credentials it never uses, and its own docstring said
#: it needed none.
needs_credentials = pytest.mark.skipif(
    not _CAN_AUTHENTICATE,
    reason=(
        "Needs DHAN_CLIENT_ID plus either DHAN_ACCESS_TOKEN or "
        "DHAN_PIN + DHAN_TOTP_SECRET, exported into the environment."
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


@needs_credentials
def test_the_bootstrap_obtains_a_token_and_dhan_accepts_it(tmp_path: Path):
    """Auth plus the spec's safe read request. Places no order."""
    bootstrap = _bootstrap(tmp_path)
    token, outcome = bootstrap.get_token()

    assert token
    assert outcome.source in {"environment", "cache", "generated"}
    assert bootstrap.validate(token) is True, "Dhan rejected the token via GET /v2/profile"


@needs_credentials
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


@needs_credentials
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
    # request_stop(), not stop(): this is the main thread and the feed thread owns
    # the SDK's loop. The callback above already closed it on the right thread if a
    # tick arrived; this covers the case where none did.
    adapter.request_stop()
    thread.join(timeout=5.0)

    assert received, (
        f"No tick within {_TIMEOUT_SECONDS}s. Check market hours, the security id "
        f"({_SECURITY_ID}) and token validity."
    )
    tick = received[0]
    assert tick.last_price > 0
    assert tick.security_id
    assert tick.exchange_time.tzinfo is not None


@needs_credentials
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
    adapter.request_stop()  # main thread: signal only, as above
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


@needs_credentials
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


@needs_credentials
def test_the_live_adapter_refuses_to_start_without_a_subscription():
    """Runs whenever the smoke suite is enabled; needs no market data."""
    from common.market_data.dhan import DhanFeedError, DhanMarketFeedAdapter

    adapter = DhanMarketFeedAdapter(client_id=str(_CLIENT_ID), access_token="unused-token")
    with pytest.raises(DhanFeedError, match="no subscriptions"):
        adapter.start(lambda tick: None)


# ------------------------------------------------- Phase 4 Part 1: the rehearsal
def test_the_scrip_master_resolves_a_real_contract(tmp_path: Path):
    """Downloads Dhan's real instrument master and resolves one NIFTY option.

    Needs no market hours and no credentials — the master is a public CSV — so
    this is the half of the rehearsal that can be run at any time of day. Places
    no order and calls no account endpoint.
    """
    from common.engine.selection import DhanOptionChainResolver
    from common.market_data.scrip_master import ScripMaster, ScripMasterCache
    from common.models import OptionType

    master = ScripMaster("NIFTY").load(cache=ScripMasterCache(tmp_path))
    assert master.lot_size and master.lot_size > 0
    assert master.expiries, "the master listed no NIFTY expiries"

    resolver = DhanOptionChainResolver(master)
    strikes = master.strikes_for_expiry(resolver.expiry)
    assert strikes, f"no strikes listed for {resolver.expiry}"

    middle = strikes[len(strikes) // 2]
    contract = resolver.resolve(int(middle), OptionType.CE)
    assert contract.security_id.isdigit(), (
        f"a real Dhan security id is numeric; got {contract.security_id!r}"
    )
    assert not contract.security_id.startswith("SIM:")
    assert contract.lot_size == master.lot_size

    # The cache must have been written, so a restart costs no second download.
    assert ScripMasterCache(tmp_path).cached_text() is not None


@needs_credentials
def test_a_real_option_contract_delivers_ticks_on_the_fno_segment(tmp_path: Path):
    """**The Part 1 rehearsal.** Resolve a real contract, subscribe it on
    ``NSE_FNO`` alongside the index on ``IDX_I``, and require a tick on the
    option leg.

    This is what limitation 17 blocked: before Part 1 every id the engine chose
    was synthetic, so this test could not have been written. Read-only — it
    subscribes and listens. No order, no order-capable endpoint.

    Needs market hours: an option that has not traded delivers nothing, which is
    correct behaviour rather than a defect.
    """
    from common.market_data.dhan import DhanMarketFeedAdapter
    from common.market_data.scrip_master import (
        ScripMaster,
        ScripMasterCache,
        resolve_index_meta,
        segment_code,
    )
    from common.models import OptionType

    meta = resolve_index_meta("NIFTY")
    master = ScripMaster("NIFTY").load(cache=ScripMasterCache(tmp_path))
    expiry = master.nearest_expiry()

    # Pick the strike nearest the index's own last price, so the contract is one
    # that is actually trading rather than a far wing that may be untouched.
    strikes = master.strikes_for_expiry(expiry)
    spot = _index_last_price(tmp_path, meta)
    atm = min(strikes, key=lambda strike: abs(strike - spot))
    contract = master.get(atm, OptionType.CE, expiry)
    assert contract is not None

    adapter = DhanMarketFeedAdapter(
        client_id=str(_CLIENT_ID),
        access_token=_token(tmp_path),
        exchange_segment=segment_code(meta.segment),  # the underlying's segment
    )
    adapter.subscribe([meta.security_id])
    adapter.subscribe([contract.security_id], segment=segment_code(meta.fno_segment))

    # Both segments, on one adapter — the thing that was impossible before.
    assert adapter.segment_for(meta.security_id) == segment_code(meta.segment)
    assert adapter.segment_for(contract.security_id) == segment_code(meta.fno_segment)

    seen: dict[str, Tick] = {}
    done = threading.Event()

    def _on_tick(tick: Tick) -> None:
        seen[tick.security_id] = tick
        if contract.security_id in seen:
            done.set()
            adapter.request_stop()

    thread = threading.Thread(target=adapter.start, args=(_on_tick,), daemon=True)
    thread.start()
    arrived = done.wait(timeout=_TIMEOUT_SECONDS)
    adapter.request_stop()
    thread.join(timeout=_TIMEOUT_SECONDS)

    assert arrived, (
        f"no tick for the real contract {contract.symbol} "
        f"(id {contract.security_id}) within {_TIMEOUT_SECONDS:.0f}s. "
        f"Received ids: {sorted(seen)}"
    )
    tick = seen[contract.security_id]
    assert tick.last_price > 0
    assert tick.exchange_time <= tick.received_at


def _index_last_price(cache_dir: Path, meta) -> float:  # type: ignore[no-untyped-def]
    """The index's LTP, via the same read-only endpoint Phase 2 Block 2 used."""
    import httpx

    response = httpx.post(
        "https://api.dhan.co/v2/marketfeed/ltp",
        headers={"access-token": _token(cache_dir), "dhanClientId": str(_CLIENT_ID)},
        json={meta.segment: [int(meta.security_id)]},
        timeout=15.0,
    )
    response.raise_for_status()
    data = response.json()["data"][meta.segment][str(meta.security_id)]
    return float(data["last_price"])
