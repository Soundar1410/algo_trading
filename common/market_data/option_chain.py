"""Option-chain snapshot service with Dhan's three-second throttle.

Dhan documents one unique Option Chain request every three seconds. The spec
requires that constraint from the first options runtime (section 7, line 1485)
and adds rules this module implements:

* strategies never call the endpoint directly — they come through here;
* requests are deduplicated across strategies;
* snapshot time, receive time and freshness are recorded;
* the WebSocket feed, not repeated chain calls, supplies fast leg LTP.

**This is a data-call throttle, and only that.** The shared cross-process *order*
limiter is explicitly out of scope (spec section 14): paper mode sends no orders,
and the limiter belongs to the controlled-live phase where multiple live runtimes
share one Dhan account. Nothing here can place, modify or cancel an order — a
test asserts the surface contains no order-related method.

Throttling uses a monotonic clock so a system-clock adjustment mid-session cannot
grant a burst of requests (or stall the service for hours). The clock and the
sleeper are injectable, so the tests prove the interval without waiting.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from common.logging import get_logger

_log = get_logger(__name__)

#: Dhan's documented minimum interval between unique Option Chain requests.
THROTTLE_SECONDS = 3.0

#: How long a snapshot is served from cache before a refetch is considered. Just
#: under the throttle so a caller polling at the throttle rate gets fresh data
#: rather than being handed the same snapshot twice.
DEFAULT_TTL_SECONDS = 2.5

#: Beyond this a snapshot is stale and delta-dependent entries must be blocked
#: (spec line 1508). Enforced by consumers in a later phase; surfaced here.
DEFAULT_STALENESS_SECONDS = 30.0


class OptionChainError(RuntimeError):
    """The chain could not be fetched. Never raised for a merely stale cache."""


@dataclass(frozen=True)
class ChainKey:
    """Identity of one unique Option Chain request.

    Dhan's limit is per *unique* request, so this tuple is both the cache key and
    the throttle key. Frozen so it can be a dict key.
    """

    underlying_security_id: int
    underlying_segment: str
    expiry: str

    def __str__(self) -> str:
        return f"{self.underlying_security_id}/{self.underlying_segment}/{self.expiry}"


@dataclass(frozen=True)
class ChainSnapshot:
    """One chain response plus the timing metadata the spec requires."""

    key: ChainKey
    payload: dict[str, Any]
    #: Exchange/broker timestamp *if the response carried one, else receive
    #: time* — but Phase 4A correction (16 August 2026 gap-closing
    #: session) confirmed against a real, live Dhan response that it never
    #: does, so this always equals ``received_at`` in practice today. Kept
    #: only as non-authoritative provenance metadata (via
    #: ``GreekSnapshot.source_timestamp``, which never uses it for
    #: freshness math either) — never call it "the exchange timestamp" as
    #: though that were independently confirmed.
    snapshot_at: datetime
    #: When we received it — the one honest, unconditional timestamp here.
    #: All freshness/staleness arithmetic below uses this alone. A recent
    #: value proves the HTTP round trip was recent; it does not by itself
    #: prove the underlying quotes are genuinely live/moving market data —
    #: only an observation made while the market is actually open can (see
    #: ``scripts/verify_dhan_option_chain.py``'s own separately-reported,
    #: never-inferred market-hours section).
    received_at: datetime
    #: Monotonic reading at receipt, used for all age arithmetic.
    received_monotonic: float

    def age_seconds(self, *, now_monotonic: float) -> float:
        return now_monotonic - self.received_monotonic

    def is_fresh(self, *, now_monotonic: float, ttl_seconds: float) -> bool:
        return self.age_seconds(now_monotonic=now_monotonic) < ttl_seconds

    def is_stale(
        self,
        *,
        now_monotonic: float,
        staleness_seconds: float = DEFAULT_STALENESS_SECONDS,
    ) -> bool:
        """True when this snapshot is too old to base a delta decision on."""
        return self.age_seconds(now_monotonic=now_monotonic) >= staleness_seconds


@dataclass
class ChainStats:
    """Observability: how well the cache and throttle are doing their jobs."""

    requests: int = 0
    api_calls: int = 0
    cache_hits: int = 0
    throttle_waits: int = 0
    throttle_wait_seconds: float = 0.0
    failures: int = 0
    per_key_api_calls: dict[str, int] = field(default_factory=dict)

    @property
    def dedup_ratio(self) -> float:
        """Share of requests served without an API call."""
        return self.cache_hits / self.requests if self.requests else 0.0


#: The read-only Dhan call this service wraps: (security_id, segment, expiry).
ChainFetcher = Callable[[int, str, str], dict[str, Any]]


class OptionChainService:
    """Caches and throttles Option Chain snapshots for every strategy in a group.

    Thread-safe: one lock guards both cache and throttle state, and it is held
    across the API call. Holding it across the call is deliberate — it is what
    collapses a burst of concurrent requests for the same key into one call,
    which is the throttle's whole purpose. Requests for *different* keys do
    serialise as a result; that is acceptable because the throttle already limits
    the group to one call every three seconds regardless of key.
    """

    def __init__(
        self,
        fetcher: ChainFetcher,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        throttle_seconds: float = THROTTLE_SECONDS,
        staleness_seconds: float = DEFAULT_STALENESS_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if throttle_seconds < 0:
            raise ValueError("throttle_seconds cannot be negative")
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds cannot be negative")
        self._fetcher = fetcher
        self._ttl = ttl_seconds
        self._throttle = throttle_seconds
        self._staleness = staleness_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._wall_clock = wall_clock
        self._cache: dict[ChainKey, ChainSnapshot] = {}
        self._last_call_at: dict[ChainKey, float] = {}
        self._lock = threading.RLock()
        self.stats = ChainStats()

    # ------------------------------------------------------------------ public
    def get(
        self,
        underlying_security_id: int,
        underlying_segment: str,
        expiry: str,
        *,
        allow_stale_on_failure: bool = True,
    ) -> ChainSnapshot:
        """Return a snapshot, fetching only when the cache is not fresh enough.

        On a fetch failure a cached snapshot is returned when one exists and
        ``allow_stale_on_failure`` is set — with its true age intact, so the
        caller can decide whether it is fit for a delta decision. That is the
        spec's rule: degrade and let the consumer block, rather than fabricate a
        fresh-looking value. A stale price is never presented as current.
        """
        key = ChainKey(underlying_security_id, underlying_segment, expiry)
        with self._lock:
            self.stats.requests += 1

            cached = self._cache.get(key)
            if cached is not None and cached.is_fresh(
                now_monotonic=self._monotonic(), ttl_seconds=self._ttl
            ):
                self.stats.cache_hits += 1
                return cached

            self._await_throttle(key)

            try:
                payload = self._fetcher(
                    key.underlying_security_id,
                    key.underlying_segment,
                    key.expiry,
                )
            except Exception as exc:
                self.stats.failures += 1
                # The attempt consumed the broker's allowance whether or not it
                # succeeded, so record it before deciding what to return.
                self._last_call_at[key] = self._monotonic()
                if cached is not None and allow_stale_on_failure:
                    _log.warning(
                        "option chain fetch failed for %s; serving cached snapshot aged %.1fs: %s",
                        key,
                        cached.age_seconds(now_monotonic=self._monotonic()),
                        exc,
                    )
                    return cached
                raise OptionChainError(
                    f"Could not fetch the option chain for {key}: {exc}"
                ) from exc

            now_monotonic = self._monotonic()
            self._last_call_at[key] = now_monotonic
            self.stats.api_calls += 1
            self.stats.per_key_api_calls[str(key)] = (
                self.stats.per_key_api_calls.get(str(key), 0) + 1
            )

            # One wall-clock reading, used for both fields. Reading it twice made
            # the no-timestamp fallback differ from receive time by a microsecond,
            # so "snapshot_at == received_at" — the signal that the broker sent no
            # timestamp — was not reliably true.
            received_at = self._wall_clock()
            snapshot = ChainSnapshot(
                key=key,
                payload=payload,
                snapshot_at=_snapshot_time(payload) or received_at,
                received_at=received_at,
                received_monotonic=now_monotonic,
            )
            self._cache[key] = snapshot
            return snapshot

    def cached(
        self, underlying_security_id: int, underlying_segment: str, expiry: str
    ) -> ChainSnapshot | None:
        """The cached snapshot, however old, without fetching."""
        key = ChainKey(underlying_security_id, underlying_segment, expiry)
        with self._lock:
            return self._cache.get(key)

    def is_stale(self, snapshot: ChainSnapshot) -> bool:
        return snapshot.is_stale(now_monotonic=self._monotonic(), staleness_seconds=self._staleness)

    def seconds_until_allowed(
        self, underlying_security_id: int, underlying_segment: str, expiry: str
    ) -> float:
        """How long until another request for this key is permitted."""
        key = ChainKey(underlying_security_id, underlying_segment, expiry)
        with self._lock:
            return self._wait_for(key)

    def invalidate(self) -> None:
        """Drop cached snapshots. Throttle state is deliberately retained —
        clearing a cache must not become a way to bypass the broker's limit."""
        with self._lock:
            self._cache.clear()

    # --------------------------------------------------------------- internals
    def _wait_for(self, key: ChainKey) -> float:
        last = self._last_call_at.get(key)
        if last is None:
            return 0.0
        elapsed = self._monotonic() - last
        return max(0.0, self._throttle - elapsed)

    def _await_throttle(self, key: ChainKey) -> None:
        wait = self._wait_for(key)
        if wait <= 0:
            return
        self.stats.throttle_waits += 1
        self.stats.throttle_wait_seconds += wait
        _log.debug("option chain throttle: waiting %.2fs for %s", wait, key)
        self._sleep(wait)


def _snapshot_time(payload: dict[str, Any]) -> datetime | None:
    """Pull a broker-supplied timestamp out of a chain response, if present.

    Dhan's documented response does not include one today, so this normally
    returns None and receive time is used. Written now because a timestamp
    appearing later must not be silently ignored — freshness is the whole point
    of the metadata the spec asks us to store.
    """
    for field_name in ("timestamp", "snapshotTime", "snapshot_time", "lastUpdated"):
        raw = payload.get(field_name)
        if raw is None:
            continue
        if isinstance(raw, int | float) and not isinstance(raw, bool):
            if raw <= 0:
                continue
            return datetime.fromtimestamp(float(raw), tz=UTC)
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None
