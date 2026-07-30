"""Option-chain caching, deduplication and Dhan's three-second throttle.

Dhan allows one unique Option Chain request every three seconds. Exceeding it is
not a local matter — it is a broker-side limit, so the tests here are about
provable request *counts*, not merely about the cache being useful.

The clock is injected throughout, so the interval is proven without any test
sleeping for three seconds.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from common.market_data.option_chain import (
    DEFAULT_STALENESS_SECONDS,
    THROTTLE_SECONDS,
    ChainKey,
    OptionChainError,
    OptionChainService,
)

NIFTY = 13
SEGMENT = "IDX_I"
EXPIRY = "2026-07-30"
OTHER_EXPIRY = "2026-08-06"


class _FakeClock:
    """A monotonic clock the test advances explicitly, including via sleep()."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _CountingFetcher:
    """Records every call and returns a distinguishable payload."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.calls: list[tuple[int, str, str]] = []
        self.fail_with = fail_with

    def __call__(self, security_id: int, segment: str, expiry: str) -> dict[str, Any]:
        self.calls.append((security_id, segment, expiry))
        if self.fail_with is not None:
            raise self.fail_with
        return {"call": len(self.calls), "expiry": expiry, "oc": {"24000": {"ce": {}}}}

    @property
    def count(self) -> int:
        return len(self.calls)


def _service(fetcher: _CountingFetcher, clock: _FakeClock, **kwargs: Any) -> OptionChainService:
    defaults: dict[str, Any] = {
        "ttl_seconds": 2.5,
        "throttle_seconds": THROTTLE_SECONDS,
        "monotonic": clock.monotonic,
        "sleep": clock.sleep,
    }
    defaults.update(kwargs)
    return OptionChainService(fetcher, **defaults)


# ------------------------------------------------------------------- basic use
def test_the_first_request_fetches():
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock)

    snapshot = service.get(NIFTY, SEGMENT, EXPIRY)

    assert fetcher.count == 1
    assert snapshot.payload["call"] == 1
    assert snapshot.key == ChainKey(NIFTY, SEGMENT, EXPIRY)
    assert snapshot.received_at.tzinfo is not None


def test_a_second_request_inside_the_ttl_is_served_from_cache():
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock)

    first = service.get(NIFTY, SEGMENT, EXPIRY)
    clock.advance(1.0)
    second = service.get(NIFTY, SEGMENT, EXPIRY)

    assert fetcher.count == 1, "the TTL must prevent a second call"
    assert second is first
    assert service.stats.cache_hits == 1


def test_a_request_after_the_ttl_and_the_throttle_refetches():
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock)

    service.get(NIFTY, SEGMENT, EXPIRY)
    clock.advance(THROTTLE_SECONDS + 0.1)
    second = service.get(NIFTY, SEGMENT, EXPIRY)

    assert fetcher.count == 2
    assert second.payload["call"] == 2
    assert clock.sleeps == [], "no wait was needed; the throttle had elapsed"


# -------------------------------------------------------------- the throttle
def test_the_throttle_holds_three_seconds_per_key():
    """The core broker constraint."""
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock, ttl_seconds=0.0)  # TTL off: isolate the throttle

    service.get(NIFTY, SEGMENT, EXPIRY)
    service.get(NIFTY, SEGMENT, EXPIRY)

    assert fetcher.count == 2
    assert clock.sleeps == [pytest.approx(THROTTLE_SECONDS)]


def test_the_throttle_waits_only_the_remaining_time():
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock, ttl_seconds=0.0)

    service.get(NIFTY, SEGMENT, EXPIRY)
    clock.advance(1.2)
    service.get(NIFTY, SEGMENT, EXPIRY)

    assert clock.sleeps == [pytest.approx(THROTTLE_SECONDS - 1.2)]


def test_the_throttle_is_per_key_not_global():
    """Two different expiries are two different unique requests."""
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock, ttl_seconds=0.0)

    service.get(NIFTY, SEGMENT, EXPIRY)
    service.get(NIFTY, SEGMENT, OTHER_EXPIRY)

    assert fetcher.count == 2
    assert clock.sleeps == [], "a different key must not wait on another key's call"


def test_seconds_until_allowed_reports_the_remaining_interval():
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock)

    assert service.seconds_until_allowed(NIFTY, SEGMENT, EXPIRY) == 0.0
    service.get(NIFTY, SEGMENT, EXPIRY)
    assert service.seconds_until_allowed(NIFTY, SEGMENT, EXPIRY) == pytest.approx(THROTTLE_SECONDS)

    clock.advance(2.0)
    assert service.seconds_until_allowed(NIFTY, SEGMENT, EXPIRY) == pytest.approx(1.0)
    clock.advance(2.0)
    assert service.seconds_until_allowed(NIFTY, SEGMENT, EXPIRY) == 0.0


def test_invalidating_the_cache_does_not_reset_the_throttle():
    """Otherwise clearing a cache would be a way to bypass the broker's limit."""
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock)

    service.get(NIFTY, SEGMENT, EXPIRY)
    service.invalidate()

    assert service.cached(NIFTY, SEGMENT, EXPIRY) is None
    assert service.seconds_until_allowed(NIFTY, SEGMENT, EXPIRY) == pytest.approx(THROTTLE_SECONDS)


def test_a_failed_call_still_consumes_the_throttle_allowance():
    """The broker counted the request whether or not it answered usefully."""
    fetcher, clock = _CountingFetcher(fail_with=RuntimeError("500")), _FakeClock()
    service = _service(fetcher, clock)

    with pytest.raises(OptionChainError):
        service.get(NIFTY, SEGMENT, EXPIRY)

    assert service.seconds_until_allowed(NIFTY, SEGMENT, EXPIRY) == pytest.approx(THROTTLE_SECONDS)


@pytest.mark.parametrize("kwargs", [{"throttle_seconds": -1}, {"ttl_seconds": -0.5}])
def test_negative_timings_are_refused(kwargs: dict[str, float]):
    with pytest.raises(ValueError):
        OptionChainService(_CountingFetcher(), **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------- dedup burst
def test_a_burst_for_one_key_collapses_to_a_single_call():
    """Deduplication across strategies: eight strategies wanting the same chain
    must produce one request, not eight."""
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock)

    snapshots = [service.get(NIFTY, SEGMENT, EXPIRY) for _ in range(8)]

    assert fetcher.count == 1
    assert all(s is snapshots[0] for s in snapshots)
    assert service.stats.cache_hits == 7
    assert service.stats.dedup_ratio == pytest.approx(7 / 8)


def test_a_concurrent_burst_from_real_threads_collapses_to_a_single_call():
    """The lock is held across the API call precisely so this holds."""
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock)
    barrier = threading.Barrier(8)
    results: list[Any] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        snapshot = service.get(NIFTY, SEGMENT, EXPIRY)
        with lock:
            results.append(snapshot)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert fetcher.count == 1, f"expected one API call, got {fetcher.count}"
    assert len(results) == 8
    assert all(r is results[0] for r in results)


def test_strategies_wanting_different_expiries_each_get_their_own_snapshot():
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock)

    near = service.get(NIFTY, SEGMENT, EXPIRY)
    far = service.get(NIFTY, SEGMENT, OTHER_EXPIRY)

    assert near.payload["expiry"] == EXPIRY
    assert far.payload["expiry"] == OTHER_EXPIRY
    assert service.stats.per_key_api_calls == {
        f"{NIFTY}/{SEGMENT}/{EXPIRY}": 1,
        f"{NIFTY}/{SEGMENT}/{OTHER_EXPIRY}": 1,
    }


# --------------------------------------------------------------- freshness
def test_a_fresh_snapshot_is_not_stale():
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock)

    snapshot = service.get(NIFTY, SEGMENT, EXPIRY)
    assert service.is_stale(snapshot) is False
    assert snapshot.age_seconds(now_monotonic=clock.monotonic()) == 0.0


def test_a_snapshot_becomes_stale_and_reports_its_true_age():
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock)

    snapshot = service.get(NIFTY, SEGMENT, EXPIRY)
    clock.advance(DEFAULT_STALENESS_SECONDS + 1)

    assert service.is_stale(snapshot) is True
    assert snapshot.age_seconds(now_monotonic=clock.monotonic()) == pytest.approx(
        DEFAULT_STALENESS_SECONDS + 1
    )


def test_a_broker_supplied_timestamp_is_used_as_the_snapshot_time():
    clock = _FakeClock()

    def fetcher(_s: int, _seg: str, _e: str) -> dict[str, Any]:
        return {"timestamp": "2026-07-30T09:20:00+00:00", "oc": {}}

    service = OptionChainService(fetcher, monotonic=clock.monotonic, sleep=clock.sleep)
    snapshot = service.get(NIFTY, SEGMENT, EXPIRY)

    assert snapshot.snapshot_at.isoformat() == "2026-07-30T09:20:00+00:00"
    assert snapshot.snapshot_at != snapshot.received_at


def test_receive_time_is_used_when_the_response_carries_no_timestamp():
    """Dhan's documented response has no timestamp today."""
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock)

    snapshot = service.get(NIFTY, SEGMENT, EXPIRY)
    assert snapshot.snapshot_at == snapshot.received_at


@pytest.mark.parametrize("bad", [0, -1, True, "not a date", None, {}])
def test_an_unusable_broker_timestamp_falls_back_to_receive_time(bad: object):
    clock = _FakeClock()

    def fetcher(_s: int, _seg: str, _e: str) -> dict[str, Any]:
        return {"timestamp": bad, "oc": {}}

    service = OptionChainService(fetcher, monotonic=clock.monotonic, sleep=clock.sleep)
    snapshot = service.get(NIFTY, SEGMENT, EXPIRY)
    assert snapshot.snapshot_at == snapshot.received_at


# -------------------------------------------------------------- degradation
def test_a_failure_with_a_cached_snapshot_degrades_rather_than_raising():
    """The spec's rule: degrade and let the consumer block on staleness, rather
    than fabricate a fresh-looking value."""
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock, ttl_seconds=0.0)

    first = service.get(NIFTY, SEGMENT, EXPIRY)
    fetcher.fail_with = RuntimeError("gateway timeout")
    clock.advance(THROTTLE_SECONDS)

    served = service.get(NIFTY, SEGMENT, EXPIRY)

    assert served is first, "the cached snapshot is reused"
    assert service.stats.failures == 1


def test_a_degraded_snapshot_keeps_its_true_age_so_consumers_can_block():
    """The age must not be reset by the failed refresh — that would present a
    stale price as current, which the spec forbids outright."""
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock, ttl_seconds=0.0)

    service.get(NIFTY, SEGMENT, EXPIRY)
    fetcher.fail_with = RuntimeError("down")
    clock.advance(DEFAULT_STALENESS_SECONDS + 5)

    served = service.get(NIFTY, SEGMENT, EXPIRY)

    assert service.is_stale(served) is True
    assert served.age_seconds(now_monotonic=clock.monotonic()) >= DEFAULT_STALENESS_SECONDS


def test_a_failure_with_no_cache_raises():
    fetcher, clock = _CountingFetcher(fail_with=RuntimeError("nope")), _FakeClock()
    service = _service(fetcher, clock)

    with pytest.raises(OptionChainError, match="Could not fetch"):
        service.get(NIFTY, SEGMENT, EXPIRY)


def test_stale_serving_can_be_refused_explicitly():
    fetcher, clock = _CountingFetcher(), _FakeClock()
    service = _service(fetcher, clock, ttl_seconds=0.0)

    service.get(NIFTY, SEGMENT, EXPIRY)
    fetcher.fail_with = RuntimeError("down")
    clock.advance(THROTTLE_SECONDS)

    with pytest.raises(OptionChainError):
        service.get(NIFTY, SEGMENT, EXPIRY, allow_stale_on_failure=False)


# ------------------------------------------------------- scope: no order path
def test_the_service_exposes_no_order_capability():
    """Spec section 14: the shared live *order* limiter is out of scope for the
    paper foundation, and this throttle must not quietly become one."""
    surface = {name for name in dir(OptionChainService) if not name.startswith("_")}
    for forbidden in ("place", "order", "modify", "cancel", "submit", "square", "exit"):
        offenders = {name for name in surface if forbidden in name.lower()}
        assert offenders == set(), f"order-related surface found: {offenders}"


def test_the_module_never_imports_a_broker():
    """A data service that can reach a broker is one refactor from placing an
    order."""
    from pathlib import Path

    import common.market_data.option_chain as module

    source = module.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    for forbidden in ("from common.broker", "import common.broker", "PaperBroker"):
        assert forbidden not in text, f"{forbidden} must not appear in the chain service"
