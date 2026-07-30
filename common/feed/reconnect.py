"""Reconnection, resubscription and staleness for the shared feed.

Implements the spec's feed-loss sequence (section 10, line 1549): mark unhealthy,
reconnect with bounded backoff and jitter, reauthorise and resubscribe, require
fresh ticks before clearing the degraded state, and track the numbers that make
all of it visible.

Why this exists rather than the SDK's own retry loop
---------------------------------------------------
``dhanhq`` 2.2.0 added a ``run()``/``start()`` threaded runner whose
``_run_async`` reconnects on a flat ``asyncio.sleep(1)`` and swallows every
exception into an optional ``on_error`` callback. That is unusable here for three
reasons: a fixed one-second retry against an authentication failure hammers
Dhan's auth path indefinitely; there is no jitter, so several runtimes restarting
together would synchronise their reconnect storms; and it cannot distinguish
reason code **807 (access token expired)** — where the fix is a new token — from
a transient network drop, where it is patience. The spec also requires a single
owner for reconnect and subscription state (line 1439), and splitting that
between the SDK and this project would give it two. Recorded as deviation D15.

Candle integrity across the gap is not this module's own invention: it delegates
to :meth:`~common.candles.aggregator.CandleAggregator.mark_feed_gap`, which
discards any bar that was open during the outage rather than publishing one
stitched from the ticks either side of it.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from common.logging import get_logger
from common.market_data.adapter import MarketFeedAdapter, TickCallback
from common.models import Tick

_log = get_logger(__name__)

DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_INITIAL_BACKOFF = 1.0
DEFAULT_MAX_BACKOFF = 60.0
DEFAULT_BACKOFF_MULTIPLIER = 2.0
#: Fraction of the delay randomised. Without jitter, several runtimes restarting
#: together reconnect in lockstep and keep colliding.
DEFAULT_JITTER_RATIO = 0.25
#: An instrument with no tick for this long is stale. Deliberately generous: a
#: quiet far-OTM strike is not a broken feed.
DEFAULT_STALENESS_SECONDS = 120.0


class FeedUnavailableError(RuntimeError):
    """Raised when the feed could not be re-established within the attempt budget.

    Fails closed: the caller must stop rather than continue with no market data.
    """


@dataclass
class ReconnectPolicy:
    """Bounded exponential backoff with jitter."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    initial_backoff: float = DEFAULT_INITIAL_BACKOFF
    max_backoff: float = DEFAULT_MAX_BACKOFF
    multiplier: float = DEFAULT_BACKOFF_MULTIPLIER
    jitter_ratio: float = DEFAULT_JITTER_RATIO

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.initial_backoff <= 0 or self.max_backoff <= 0:
            raise ValueError("backoff values must be positive")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1, or backoff would shrink")
        if not 0.0 <= self.jitter_ratio <= 1.0:
            raise ValueError("jitter_ratio must be between 0 and 1")

    def delay_for(self, attempt: int, *, rng: Callable[[], float] = random.random) -> float:
        """Delay before ``attempt`` (1-based), capped and jittered.

        Jitter is applied downward only, so the cap is a real cap: an operator
        reading ``max_backoff=60`` will never wait 75 seconds.
        """
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        raw = self.initial_backoff * (self.multiplier ** (attempt - 1))
        capped = min(raw, self.max_backoff)
        return capped * (1.0 - self.jitter_ratio * rng())


@dataclass
class FeedHealth:
    """The observability the spec asks for at line 1560."""

    connected: bool = False
    degraded: bool = True
    reconnect_count: int = 0
    failed_attempts: int = 0
    token_refreshes: int = 0
    last_disconnect_reason: str | None = None
    last_disconnect_code: int | None = None
    last_connected_at: datetime | None = None
    last_disconnected_at: datetime | None = None
    total_downtime_seconds: float = 0.0
    expected_subscriptions: int = 0
    active_subscriptions: int = 0
    last_tick_at: dict[str, datetime] = field(default_factory=dict)
    gap_candles_discarded: int = 0

    @property
    def subscriptions_match(self) -> bool:
        return self.expected_subscriptions == self.active_subscriptions

    def stale_instruments(
        self,
        *,
        now: datetime | None = None,
        threshold_seconds: float = DEFAULT_STALENESS_SECONDS,
    ) -> tuple[str, ...]:
        """Instruments whose last tick is older than the threshold.

        An instrument never seen at all is *not* reported here: it has no last
        tick to be stale relative to, and conflating "quiet" with "never
        subscribed" would hide the more serious of the two problems.
        """
        moment = now or datetime.now(UTC)
        cutoff = moment - timedelta(seconds=threshold_seconds)
        return tuple(sorted(sid for sid, seen in self.last_tick_at.items() if seen < cutoff))


class ReconnectingFeed:
    """Wraps a feed adapter with reconnection, resubscription and health.

    Satisfies :class:`~common.market_data.adapter.MarketFeedAdapter`, so the hub
    is indifferent to whether it holds a bare adapter or a reconnecting one.
    """

    def __init__(
        self,
        adapter: MarketFeedAdapter,
        *,
        policy: ReconnectPolicy | None = None,
        on_feed_gap: Callable[[], int] | None = None,
        refresh_token: Callable[[], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        rng: Callable[[], float] = random.random,
        staleness_seconds: float = DEFAULT_STALENESS_SECONDS,
    ) -> None:
        self._adapter = adapter
        self._policy = policy or ReconnectPolicy()
        #: Invoked when the feed drops, so the aggregator can invalidate any bar
        #: that was open during the outage.
        self._on_feed_gap = on_feed_gap
        #: Invoked on Dhan reason code 807 before reconnecting, because retrying
        #: with an expired token cannot succeed however long we wait.
        self._refresh_token = refresh_token
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._staleness_seconds = staleness_seconds
        self._security_ids: list[str] = []
        self._stopped = False
        #: Consecutive failures, not lifetime failures. The attempt budget is
        #: meant to bound one *unrecovered* outage; counting for the whole session
        #: would kill a runtime on its ninth instant recovery of the day.
        self._consecutive_failures = 0
        self.health = FeedHealth()

    # ------------------------------------------------------------- properties
    @property
    def is_running(self) -> bool:
        return self._adapter.is_running

    def stopped(self) -> bool:
        """Whether :meth:`stop` has been called.

        A method rather than a bare attribute read because ``stop()`` can be
        called from inside the tick callback, i.e. from within
        ``adapter.start()``. Reading the attribute directly inside the reconnect
        loop lets a type checker narrow it to ``False`` for the whole body and
        conclude the mid-flight stop is unreachable — which is wrong, and would
        mean silencing a warning that is usually worth listening to.
        """
        return self._stopped

    @property
    def policy(self) -> ReconnectPolicy:
        return self._policy

    # ----------------------------------------------------------- subscription
    def subscribe(self, security_ids: Sequence[str]) -> None:
        merged = dict.fromkeys([*self._security_ids, *(str(s) for s in security_ids)])
        self._security_ids = list(merged)
        self.health.expected_subscriptions = len(self._security_ids)
        self._adapter.subscribe(self._security_ids)

    # ---------------------------------------------------------------- running
    def start(self, on_tick: TickCallback) -> None:
        """Run the feed, reconnecting until stopped or the budget is exhausted."""
        if not self._security_ids:
            raise FeedUnavailableError("Refusing to start the feed with no subscriptions")

        # Deliberately does *not* reset ``_stopped``: a feed that has been stopped
        # stays stopped, so a stray restart cannot reopen a socket during
        # shutdown. (It also keeps mypy from narrowing the flag to Literal[False]
        # and concluding that the callback-driven stop below is unreachable —
        # ``stop()`` can be called from inside ``on_tick``, which mypy cannot see.)
        self._consecutive_failures = 0

        while not self.stopped():
            try:
                self._mark_connected()
                self._adapter.start(self._wrap(on_tick))
            except Exception as exc:
                if self.stopped():
                    break
                self._consecutive_failures += 1
                self.health.failed_attempts += 1
                self._mark_disconnected(exc)

                attempt = self._consecutive_failures
                if attempt >= self._policy.max_attempts:
                    raise FeedUnavailableError(
                        f"Feed did not recover after {attempt} attempts; last error: {exc}. "
                        "Failing closed rather than continuing without market data."
                    ) from exc

                self._maybe_refresh_token()
                delay = self._policy.delay_for(attempt, rng=self._rng)
                _log.warning(
                    "feed reconnect attempt=%d/%d in %.2fs reason=%s",
                    attempt,
                    self._policy.max_attempts,
                    delay,
                    self.health.last_disconnect_reason,
                )
                self._sleep(delay)
                self._reconnect()
                continue

            # A clean return means the adapter is finished, not that it failed.
            #
            # This holds for both implementations, and the distinction matters.
            # The live adapter loops `while self._running`, and only stop() clears
            # that flag, so returning implies an intentional stop. The recorded
            # adapter returns when its tape is exhausted, which is the normal end
            # of a replay. Treating either as a failure would make this wrapper
            # unusable with a recorded feed — which is what every automated test
            # and the walking skeleton use.
            break

        self.health.connected = False

    def stop(self) -> None:
        """Stop for good. Idempotent, and suppresses further reconnection."""
        self._stopped = True
        self.health.connected = False
        self._adapter.stop()

    # ---------------------------------------------------------------- internals
    def _wrap(self, on_tick: TickCallback) -> TickCallback:
        """Track per-instrument freshness, and clear degraded state on real data.

        The spec requires fresh ticks before the degraded flag is cleared
        (line 1556): a socket that connects but delivers nothing is not a
        recovered feed.
        """

        def _observe(tick: Tick) -> None:
            self.health.last_tick_at[tick.security_id] = tick.exchange_time
            if self.health.degraded:
                self.health.degraded = False
                # Real data is the only proof of recovery, so it is also the only
                # thing that restores the attempt budget.
                self._consecutive_failures = 0
                _log.info("feed no longer degraded: fresh tick for %s", tick.security_id)
            on_tick(tick)

        return _observe

    def _mark_connected(self) -> None:
        now = self._clock()
        self.health.connected = True
        self.health.last_connected_at = now
        if self.health.last_disconnected_at is not None:
            self.health.total_downtime_seconds += (
                now - self.health.last_disconnected_at
            ).total_seconds()
            self.health.last_disconnected_at = None

    def _mark_disconnected(self, exc: Exception | None) -> None:
        self.health.connected = False
        self.health.degraded = True
        self.health.last_disconnected_at = self._clock()

        code = getattr(self._adapter, "last_disconnect_code", None)
        if isinstance(code, int):
            from common.market_data.dhan import DISCONNECT_REASONS

            self.health.last_disconnect_code = code
            self.health.last_disconnect_reason = DISCONNECT_REASONS.get(code, f"code {code}")
        elif exc is not None:
            self.health.last_disconnect_reason = f"{type(exc).__name__}: {exc}"
        else:
            self.health.last_disconnect_reason = "feed exited without being stopped"

        # Invalidate any bar that was open during the outage.
        if self._on_feed_gap is not None:
            discarded = self._on_feed_gap()
            self.health.gap_candles_discarded += discarded
            if discarded:
                _log.warning("discarded %d candle(s) that spanned the feed gap", discarded)

    def _maybe_refresh_token(self) -> None:
        """Refresh the token when, and only when, Dhan said it expired.

        Reconnecting with an expired token fails identically however long the
        backoff grows, so a 807 has to be handled differently from a network
        drop rather than simply waited out.
        """
        if self._refresh_token is None:
            return
        if self.health.last_disconnect_code != 807:
            return
        try:
            self._refresh_token()
            self.health.token_refreshes += 1
            _log.info("refreshed the access token after a 807 disconnect")
        except Exception as exc:
            # A failed refresh is not fatal here: the next attempt may succeed,
            # and the attempt budget still bounds the whole loop.
            _log.error("token refresh after 807 failed: %s", exc)

    def _reconnect(self) -> None:
        self.health.reconnect_count += 1
        self._adapter.stop()
        resubscribe = getattr(self._adapter, "resubscribe_all", None)
        if callable(resubscribe):
            # The full set, not the delta: a new socket carries no subscriptions.
            self._adapter.subscribe(self._security_ids)
            resubscribe()
        else:
            self._adapter.subscribe(self._security_ids)
        self.health.active_subscriptions = len(
            getattr(self._adapter, "subscribed", self._security_ids)
        )
