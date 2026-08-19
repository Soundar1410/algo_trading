"""The engine's tick source inside a worker process: the hub's tick channel.

Phase 3 Part 2b-ii-A. :mod:`common.engine.feed` already named this module before
it existed — "They meet in Part 2b-ii, where the hub's new tick channel is wrapped
in a ``MarketDataFeed`` implementation reading the worker's queue" — and this is
that implementation.

It is the consumer half of a two-hop path::

    supervisor process                       worker process
    ------------------                       --------------
    SharedFeedHub.on_tick                    HubTickFeed.run()
      └─ tick_queue.publish(tick)  ──────▶     └─ engine.on_tick(tick)
                                                   └─ feed.subscribe(contract)
    hub.request_subscription(...)  ◀──────       └─ control_queue.put(...)

Three things make this more than a queue drain.

**The sentinel is a square-off request.** The supervisor publishes ``None`` per
worker at shutdown, so a ``SIGTERM`` delivered only to the supervisor still
reaches each worker's engine in-band. Honouring it *here* is the D18 boundary
exactly: this loop is the thread that invokes ``engine.on_tick``, so the engine's
positions and the feed are closed by the thread that owns them, not by whoever
sent the signal.

**Subscriptions travel upstream, not to a broker.** The engine picks an option
contract at runtime and calls ``feed.subscribe(security_id)``. This process holds
no connection, so the request is forwarded to the hub, which applies it on its own
feed thread. ``unsubscribe`` deliberately does not propagate to the adapter — the
subscription belongs to the group, and dropping it would starve any other worker
holding the same instrument.

**A dropped tick is reported in band.** The hub counts overflow in its *own*
process, which the engine here cannot read. Part 2b-ii-B-1 therefore sends a
:class:`~common.feed.queues.TickDropNotice` down this same queue; it is neither a
tick nor a sentinel, so the run continues and only ``on_tick_dropped`` fires —
which the worker wires to the engine's entry latch, because bars built from an
incomplete tick stream may be silently wrong (runbook limitation 14).

**``stop()`` is still the owning thread's.** The engine calls it from
``_handle_square_off``, which runs on this loop's thread. The Part 1 ownership rule
therefore holds here with no new mechanism: nothing outside this thread ever ends
the run except by way of the sentinel above.

**A silent feed still honours a shutdown.** ``run()`` wakes every ``poll_seconds``
whether or not a tick arrived, so it can ask ``should_stop`` — which the worker wires
to ``engine.square_off_requested`` — on each wake. Without that check a ``SIGTERM``
arriving while the stream is quiet would set the engine's flag and then wait for a
tick to carry it into ``on_tick``; with ``idle_timeout_seconds=None``, which is what
a live session runs, that wait is unbounded. This is the engine-level half of runbook
limitation 13, and it is fixed here rather than alarmed about, because the boundary
the fix needs already existed: returning from this loop hands control to
``TradingEngine.run()``'s ``finally``, which is the second of the two square-off
boundaries D18 names. Nothing crosses a thread to make it happen.
"""

from __future__ import annotations

import queue as queue_module
from collections.abc import Callable
from typing import Any

from common.feed.queues import FeedGapNotice, TickDropNotice
from common.logging import get_logger
from common.models import Tick

from .feed import MarketDataFeed

log = get_logger(__name__)

#: How long to block on the queue before re-checking the running flag. Short
#: enough that a ``stop()`` from inside a tick callback is noticed promptly,
#: long enough not to spin.
DEFAULT_POLL_SECONDS = 0.5

#: How long a run tolerates an empty queue before concluding the tape is
#: finished. ``None`` disables it, for a live session that must outlast a quiet
#: patch. Mirrors the worker's own ``idle_timeout_seconds``.
DEFAULT_IDLE_TIMEOUT_SECONDS: float | None = 10.0


class HubTickFeed(MarketDataFeed):
    """A :class:`MarketDataFeed` over one worker's bounded tick queue."""

    def __init__(
        self,
        tick_queue: Any,
        *,
        request_subscription: Callable[[str], None] | None = None,
        on_square_off: Callable[[str], None] | None = None,
        on_tick_dropped: Callable[[TickDropNotice], None] | None = None,
        on_feed_gap: Callable[[FeedGapNotice], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        on_poll: Callable[[], None] | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        idle_timeout_seconds: float | None = DEFAULT_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__()
        self._queue = tick_queue
        self._request_subscription = request_subscription
        self._on_square_off = on_square_off
        self._on_tick_dropped = on_tick_dropped
        #: Phase 6A: the shared feed's own connection-state notices
        #: (disconnected/resubscribed), broadcast to every worker regardless
        #: of instrument — see ``FeedGapNotice``'s own docstring.
        self._on_feed_gap = on_feed_gap
        #: Asked on every poll wake, so a shutdown is honoured even while the stream
        #: is silent. The worker wires this to ``engine.square_off_requested``; see
        #: the module docstring for why the check belongs here.
        self._should_stop = should_stop
        #: Called on every poll wake, immediately before ``should_stop`` is asked.
        #: **This is the only place in a worker that runs on a timer**, which is why
        #: the wall-clock square-off net (runbook limitation 7) lives here: the
        #: engine's own square-off is driven by the candle clock, so a feed that
        #: goes quiet before the square-off bar would never reach it.
        #:
        #: It runs on the worker's *main* thread — the same one that owns the
        #: SQLite connection — so a callback here may consult a persisting
        #: authority without violating **D31**.
        #:
        #: Exceptions are deliberately **not** caught: this hook exists to make a
        #: position close, and a net that silently swallowed its own failure would
        #: be worse than no net at all.
        self._on_poll = on_poll
        self._poll = poll_seconds
        self._idle_timeout = idle_timeout_seconds
        #: Observable outcome, so a worker can report *why* the run ended rather
        #: than inferring it. Set before ``run()`` returns, never cleared.
        self.stopped_by_sentinel = False
        self.stopped_by_idle_timeout = False
        self.stopped_by_request = False
        self.ticks_received = 0
        #: Highest drop total this feed has been told about. Zero on a healthy run.
        self.ticks_dropped_upstream = 0

    # ---------------------------------------------------------------- running
    def run(self) -> None:
        """Drain ticks into the registered handler until stopped or idle.

        Ends in exactly four ways: the sentinel (an orderly shutdown asked for
        from the supervisor), :meth:`stop` from inside a tick callback (the
        engine's own square-off), ``should_stop`` answering ``True`` on a poll wake
        (a shutdown requested while the stream was silent), or the idle timeout (an
        exhausted tape).
        """
        self._running = True
        idle_seconds = 0.0
        try:
            while self._running:
                if self._on_poll is not None:
                    # Before should_stop, so a net that *requests* a square-off is
                    # noticed on this same wake rather than the next one.
                    self._on_poll()
                if self._should_stop is not None and self._should_stop():
                    # Checked before the blocking get, so it is asked at least once
                    # every ``poll_seconds`` no matter how quiet the stream is.
                    # Returning hands control to TradingEngine.run()'s finally, which
                    # is where a request with no tick to carry it gets honoured.
                    self.stopped_by_request = True
                    log.info("a square-off was requested; ending the tick stream")
                    return
                try:
                    item = self._queue.get(timeout=self._poll)
                except queue_module.Empty:
                    idle_seconds += self._poll
                    if self._idle_timeout is not None and idle_seconds >= self._idle_timeout:
                        self.stopped_by_idle_timeout = True
                        log.info(
                            "no ticks for %.1fs; treating the stream as finished",
                            idle_seconds,
                        )
                        return
                    continue

                idle_seconds = 0.0

                if item is None:
                    # The supervisor's shutdown sentinel. Tell the engine and let
                    # it square off on this thread — see the module docstring.
                    self.stopped_by_sentinel = True
                    log.info("shutdown sentinel received; asking the engine to square off")
                    if self._on_square_off is not None:
                        self._on_square_off("supervisor sentinel")
                    return

                if isinstance(item, TickDropNotice):
                    # Not a tick and not a sentinel: the stream continues, but the
                    # bars this worker builds from it may now be quietly wrong, so
                    # the consumer is told and the run carries on. Matched by type
                    # rather than identity because it arrives via pickle.
                    self.ticks_dropped_upstream = max(self.ticks_dropped_upstream, item.dropped)
                    log.warning(
                        "the hub dropped %d tick(s) for this worker; candles built "
                        "here may differ from the hub's for the affected interval",
                        item.dropped,
                    )
                    if self._on_tick_dropped is not None:
                        self._on_tick_dropped(item)
                    continue

                if isinstance(item, FeedGapNotice):
                    # Not a tick, not a sentinel, not a drop — a feed-level
                    # connection-state change every worker sharing the feed
                    # receives (see FeedGapNotice's own docstring for why
                    # this is broadcast rather than routed like a tick).
                    log.warning("feed gap notice: kind=%s at=%s", item.kind, item.at)
                    if self._on_feed_gap is not None:
                        self._on_feed_gap(item)
                    continue

                tick: Tick = item
                self.ticks_received += 1
                self._emit(tick)
        finally:
            self._running = False

    # ---------------------------------------------------------- subscriptions
    def _do_subscribe(self, security_id: str) -> None:
        """Forward upstream; this process owns no connection to subscribe on."""
        if self._request_subscription is None:
            log.warning(
                "no subscription channel wired; ticks for %s will never arrive",
                security_id,
            )
            return
        self._request_subscription(security_id)

    def _do_unsubscribe(self, security_id: str) -> None:
        """Deliberately a no-op upstream. See the module docstring."""
