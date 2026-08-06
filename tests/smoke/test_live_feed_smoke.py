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

**Fixing known limitation 18** (see the runbook): every test here used to build
its own ``AuthBootstrap`` against a throwaway ``tmp_path``, which meant that with
``DHAN_PIN``/``DHAN_TOTP_SECRET`` exported it *always* attempted a fresh TOTP
login -- even when this repo already held a perfectly good cached token.
Confirmed live on 6 August 2026: that fresh login minted a new access token for
a Dhan client ID shared with ``Trading_Automation``, which silently invalidated
that system's already-active session. ``ALGO_LIVE_SMOKE=1`` was never meant to
also mean "mint a new token" -- those are now two separate decisions:

* By default, every test in this file reuses this repo's own token cache
  (``data/cache/token_cache.json``, via the same :class:`TokenCache` a real
  runtime reads) or an exported ``DHAN_ACCESS_TOKEN``. If neither is usable, a
  test fails closed with ``MissingCredentialsError`` rather than logging
  anyone out.
* A fresh TOTP login is only attempted when ``ALGO_SMOKE_ALLOW_FRESH_LOGIN=1``
  is *also* exported, on top of ``DHAN_PIN``/``DHAN_TOTP_SECRET``. Exactly one
  test needs this: the one that verifies the cache-writing code path itself,
  which cannot be exercised without a real generation.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from common.models import Tick

_ENABLED = os.environ.get("ALGO_LIVE_SMOKE") == "1"
_CLIENT_ID = os.environ.get("DHAN_CLIENT_ID")

#: The **separate** gate for minting a fresh Dhan login. Deliberately distinct
#: from ALGO_LIVE_SMOKE=1: conflating "run the live smoke tests" with "mint a
#: new token" is exactly what caused the incident behind known limitation 18 --
#: a run of this file generated a new access token for a Dhan client ID shared
#: with Trading_Automation, which silently invalidated that system's
#: already-active session. Only one test in this file needs to set this.
_ALLOW_FRESH_LOGIN = os.environ.get("ALGO_SMOKE_ALLOW_FRESH_LOGIN") == "1"


def _cached_token_is_usable() -> bool:
    """True if this repo's own token cache already holds a usable token.

    Checked at collection time, before any fixture runs and before any test
    body executes, using the same :class:`TokenCache` a real runtime reads --
    not a fresh ``AuthBootstrap``. This never touches the network and never
    mints anything; it only decides whether the module's skip gate reflects
    what is already true on disk.
    """
    from common.authentication import TokenCache
    from common.authentication.bootstrap import TOKEN_CACHE_FILENAME
    from common.config.paths import load_paths

    cache = TokenCache(load_paths().cache_root / TOKEN_CACHE_FILENAME)
    stored = cache.load(expected_client_id=_CLIENT_ID)
    return stored is not None and stored.is_usable()


_CAN_AUTHENTICATE = bool(
    _CLIENT_ID
    and (
        os.environ.get("DHAN_ACCESS_TOKEN")
        or _cached_token_is_usable()
        or (
            _ALLOW_FRESH_LOGIN and os.environ.get("DHAN_PIN") and os.environ.get("DHAN_TOTP_SECRET")
        )
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
        "Needs DHAN_CLIENT_ID plus a usable token: DHAN_ACCESS_TOKEN, an "
        "already-cached token in data/cache/token_cache.json, or "
        "ALGO_SMOKE_ALLOW_FRESH_LOGIN=1 plus DHAN_PIN + DHAN_TOTP_SECRET."
    ),
)

#: The narrower gate for the one test that must mint a fresh login to do its
#: job -- it verifies the cache-writing code path itself. Every other test in
#: this file is content to reuse whatever token is already available.
needs_fresh_login = pytest.mark.skipif(
    not (_ALLOW_FRESH_LOGIN and os.environ.get("DHAN_PIN") and os.environ.get("DHAN_TOTP_SECRET")),
    reason="Needs ALGO_SMOKE_ALLOW_FRESH_LOGIN=1 plus DHAN_PIN + DHAN_TOTP_SECRET.",
)

#: NIFTY 50 index on the IDX segment.
_SECURITY_ID = os.environ.get("ALGO_SMOKE_SECURITY_ID", "13")
_SEGMENT = int(os.environ.get("ALGO_SMOKE_SEGMENT", "0"))
_TIMEOUT_SECONDS = 30.0


def _default_cache_dir() -> Path:
    """This repo's own token cache directory -- the same one a real runtime
    reads and writes, so a test run here sees whatever a pre-market
    ``scripts.auth_bootstrap`` already minted today."""
    from common.config.paths import load_paths

    return load_paths().cache_root


def _bootstrap(*, cache_dir: Path | None = None, allow_fresh_login: bool = _ALLOW_FRESH_LOGIN):  # type: ignore[no-untyped-def]
    """Build the bootstrap. Defaults to reuse-only.

    A fresh login is only ever possible when ``allow_fresh_login`` is
    explicitly True (by default, whatever ``ALGO_SMOKE_ALLOW_FRESH_LOGIN``
    says) -- not merely because ``DHAN_PIN``/``DHAN_TOTP_SECRET`` are present.
    Omitting the pin/totp from ``AuthCredentials`` entirely when fresh login is
    not allowed is what makes this safe: it leaves
    ``AuthCredentials.can_generate`` False, so ``AuthBootstrap`` never builds a
    login object at all -- there is no path to a network call to forget to
    take.
    """
    from common.authentication import AuthBootstrap, AuthCredentials

    return AuthBootstrap(
        AuthCredentials(
            client_id=str(_CLIENT_ID),
            pin=os.environ.get("DHAN_PIN") if allow_fresh_login else None,
            totp_secret=os.environ.get("DHAN_TOTP_SECRET") if allow_fresh_login else None,
            access_token=os.environ.get("DHAN_ACCESS_TOKEN"),
        ),
        cache_dir=cache_dir if cache_dir is not None else _default_cache_dir(),
    )


def _token(cache_dir: Path | None = None) -> str:
    """Obtain a token the way a real runtime does: reuse first, and never mint
    one unless explicitly told to."""
    token, _outcome = _bootstrap(cache_dir=cache_dir).get_token()
    return token


@needs_credentials
def test_the_bootstrap_obtains_a_token_and_dhan_accepts_it():
    """Auth plus the spec's safe read request. Places no order.

    Reuse-only by default (see the module docstring): this proves the *reuse*
    path works end to end, which is the path every other test in this file
    also takes.
    """
    bootstrap = _bootstrap()
    token, outcome = bootstrap.get_token()

    assert token
    assert outcome.source in {"environment", "cache", "generated"}
    assert bootstrap.validate(token) is True, "Dhan rejected the token via GET /v2/profile"


@needs_fresh_login
def test_the_token_cache_is_written_atomically_and_privately(tmp_path: Path):
    """Ratifies the cache against a real, freshly-generated token.

    The one test in this file that must mint a login rather than reuse one --
    the whole point is to observe the write happen. Isolated to its own
    ``tmp_path`` rather than this repo's real cache dir, so it never touches
    (or races) ``data/cache/token_cache.json``.
    """
    import stat

    bootstrap = _bootstrap(cache_dir=tmp_path, allow_fresh_login=True)
    bootstrap.get_token()

    assert bootstrap.cache.path.exists()
    assert stat.S_IMODE(bootstrap.cache.path.stat().st_mode) == 0o600
    reloaded = bootstrap.cache.load(expected_client_id=str(_CLIENT_ID))
    assert reloaded is not None and reloaded.access_token


@needs_credentials
def test_one_live_tick_reaches_the_hub():
    """Read-only: subscribe, receive one tick, disconnect. No order is placed."""
    from common.market_data.dhan import DhanMarketFeedAdapter

    adapter = DhanMarketFeedAdapter(
        client_id=str(_CLIENT_ID),
        access_token=_token(),
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
def test_the_live_payload_matches_the_ratified_shape():
    """The Block 2 ratification assertion.

    Phase 1 shipped normalisation written against an *unobserved* payload. This
    checks the reconstructed exchange timestamp against real data: a non-zero
    fallback count means Dhan's LTT format is not what the SDK source implies,
    and candles would be silently bucketed by arrival time instead.
    """
    from common.market_data.dhan import DhanMarketFeedAdapter

    adapter = DhanMarketFeedAdapter(
        client_id=str(_CLIENT_ID),
        access_token=_token(),
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
def test_the_option_chain_throttle_holds_against_the_real_endpoint():
    """One read-only /optionchain call through the service. Places no order."""
    import httpx

    from common.market_data.option_chain import OptionChainService

    expiry = os.environ.get("ALGO_SMOKE_EXPIRY")
    if not expiry:
        pytest.skip("set ALGO_SMOKE_EXPIRY=YYYY-MM-DD to exercise the option chain")

    token = _token()
    client_id = str(_CLIENT_ID)

    def fetch(security_id: int, segment: str, chain_expiry: str) -> dict[str, object]:
        response = httpx.post(
            "https://api.dhan.co/v2/optionchain",
            # "client-id", not "dhanClientId" -- and dhanClientId belongs in
            # the body, not a header. See known limitation 19; matches the
            # SDK's own dhan_http.py.
            headers={"access-token": token, "client-id": client_id},
            json={
                "UnderlyingScrip": security_id,
                "UnderlyingSeg": segment,
                "Expiry": chain_expiry,
                "dhanClientId": client_id,
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
    spot = _index_last_price(meta)
    atm = min(strikes, key=lambda strike: abs(strike - spot))
    contract = master.get(atm, OptionType.CE, expiry)
    assert contract is not None

    adapter = DhanMarketFeedAdapter(
        client_id=str(_CLIENT_ID),
        access_token=_token(),
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


# ------------------------------------------------------ Phase 4 Part 5: depth
@needs_credentials
def test_a_real_option_in_full_mode_delivers_a_two_sided_book(tmp_path: Path):
    """**The Part 5 gate item, and the one that must be run live.**

    Everything else about the fill model is provable offline against a Full frame
    packed from Dhan's documented layout and parsed by the SDK's own
    ``process_full``. What that cannot establish is that a *real* ``NSE_FNO``
    option, subscribed in mode 21 against the real socket, actually delivers a
    two-sided book — which is the single fact the whole of Part 5 rests on, and
    the reason Part 1 was a hard precondition for it.

    It also pins the mode split that makes the subscription work at all: the
    index stays on Ticker (an index has no order book in any mode) while its
    contract goes on Full, both on one adapter and one socket.

    Read-only: it subscribes and listens. No order, no order-capable endpoint.
    Needs market hours; a far wing that has not traded delivers nothing, which is
    correct behaviour rather than a defect — hence the ATM strike below.
    """
    from common.market_data.dhan import FULL_MODE, TICKER_MODE, DhanMarketFeedAdapter
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
    spot = _index_last_price(meta)
    atm = min(master.strikes_for_expiry(expiry), key=lambda strike: abs(strike - spot))
    contract = master.get(atm, OptionType.CE, expiry)
    assert contract is not None

    adapter = DhanMarketFeedAdapter(
        client_id=str(_CLIENT_ID),
        access_token=_token(),
        exchange_segment=segment_code(meta.segment),
    )
    adapter.subscribe([meta.security_id])
    adapter.subscribe(
        [contract.security_id],
        segment=segment_code(meta.fno_segment),
        mode=FULL_MODE,
    )
    assert adapter.mode_for(meta.security_id) == TICKER_MODE
    assert adapter.mode_for(contract.security_id) == FULL_MODE

    with_book: list[Tick] = []
    done = threading.Event()

    def _on_tick(tick: Tick) -> None:
        if tick.security_id != contract.security_id:
            return
        if tick.bid_price is not None and tick.ask_price is not None:
            with_book.append(tick)
            done.set()
            adapter.request_stop()

    thread = threading.Thread(target=adapter.start, args=(_on_tick,), daemon=True)
    thread.start()
    arrived = done.wait(timeout=_TIMEOUT_SECONDS)
    adapter.request_stop()
    thread.join(timeout=_TIMEOUT_SECONDS)

    assert arrived, (
        f"no two-sided book for {contract.symbol} (id {contract.security_id}) within "
        f"{_TIMEOUT_SECONDS:.0f}s. ticks={adapter.counters.ticks} "
        f"with_depth={adapter.counters.ticks_with_depth} "
        f"one_sided={adapter.counters.ticks_one_sided_book} "
        f"non_tick={adapter.counters.non_tick_frames}. A non_tick count rising with "
        "no ticks would mean 'Full Data' is not being recognised at all."
    )
    tick = with_book[0]
    assert tick.bid_price is not None and tick.ask_price is not None
    assert 0 < tick.bid_price <= tick.ask_price, "a crossed or zero book is not a book"
    assert tick.exchange_time <= tick.received_at
    assert adapter.counters.malformed_payloads == 0
    assert adapter.counters.ticks_with_depth > 0


# --------------------------------------------------- Phase 4 Part 4: warm-up
@needs_credentials
def test_the_intraday_endpoint_returns_a_success_shape_during_market_hours() -> None:
    """**Narrows, but cannot fully resolve, the one thing this port could not
    verify from source or documentation alone**: whether Dhan's
    ``/v2/charts/intraday`` returns a partial candle for the still-forming
    period when ``toDate`` is "now" during a live session. No fixture or
    captured evidence exists anywhere in this repository for this endpoint's
    exact response — only DhanHQ's documented shape. This test confirms the
    documented shape holds against a real call; it does not (and structurally
    cannot, without a captured tape) settle the partial-candle question on its
    own — see the next test for what the code does regardless of the answer.

    Read-only: a single ``POST /v2/charts/intraday``. No order-capable
    endpoint is reachable from here.
    """
    from common.market_data.dhan_historical import DhanHistoricalDataClient
    from common.utils.timeutils import now_ist
    from common.warmup.historical import parse_intraday_response

    now = now_ist()
    client = DhanHistoricalDataClient(str(_CLIENT_ID), _token())
    resp = client.fetch_intraday(
        security_id=_SECURITY_ID,
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        from_at=now.replace(hour=9, minute=15, second=0, microsecond=0),
        to_at=now,
    )
    assert isinstance(resp, dict) and resp, "empty/non-dict response from a 200"
    candles = parse_intraday_response(resp, "Asia/Kolkata")
    assert candles, (
        "no candles parsed from a real intraday response during market hours; "
        "check the documented shape still matches what was actually returned"
    )


@needs_credentials
def test_the_still_forming_bucket_is_excluded_from_a_live_fetch() -> None:
    """The code's own defence against a partial trailing candle, checked
    against a real response rather than a synthetic one. Whatever Dhan
    actually returns for the still-open period, nothing at or after the
    current timeframe bucket may survive :func:`fetch_warmup_candles_range`'s
    own filter.
    """
    from common.engine.config import SessionConfig
    from common.engine.session import MarketSession
    from common.market_data.dhan_historical import DhanHistoricalDataClient
    from common.utils.timeutils import now_ist
    from common.warmup.historical import fetch_warmup_candles_range

    now = now_ist()
    session = MarketSession(SessionConfig())
    client = DhanHistoricalDataClient(str(_CLIENT_ID), _token())
    candles = fetch_warmup_candles_range(
        client,
        security_id=_SECURITY_ID,
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        session=session,
        timeframe_minutes=5,
        lookback_sessions=1,
        now=now,
    )
    assert candles, "no candles returned during market hours; nothing to check the filter against"
    for candle in candles:
        assert candle.end_at <= now, (
            f"candle {candle.start_at}-{candle.end_at} was not excluded even though it "
            f"had not fully closed by {now} -- the still-forming-bucket filter did not hold"
        )


def _index_last_price(meta, *, token: str | None = None, http_post=None) -> float:  # type: ignore[no-untyped-def]
    """The index's LTP, via the same read-only endpoint Phase 2 Block 2 used.

    ``token``/``http_post`` are injectable so the request *shape* can be unit
    tested without credentials or a network call -- see
    ``tests/unit/test_smoke_request_shapes.py``, which is what would have
    caught known limitation 19. Left unset, as every real call here leaves
    them, this behaves exactly as before: resolve a token the normal way and
    call the real endpoint.
    """
    import httpx

    resolved_token = token if token is not None else _token()
    post = http_post or httpx.post
    response = post(
        "https://api.dhan.co/v2/marketfeed/ltp",
        # "client-id", not "dhanClientId" -- and dhanClientId belongs in the
        # body, not a header. See known limitation 19; matches the SDK's own
        # dhan_http.py (client-id header at dhan_http.py:43, dhanClientId
        # injected into the body at dhan_http.py:53-56).
        headers={"access-token": resolved_token, "client-id": str(_CLIENT_ID)},
        json={meta.segment: [int(meta.security_id)], "dhanClientId": str(_CLIENT_ID)},
        timeout=15.0,
    )
    response.raise_for_status()
    data = response.json()["data"][meta.segment][str(meta.security_id)]
    return float(data["last_price"])
