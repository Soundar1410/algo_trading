"""The shared feed hub.

One Dhan WebSocket per strategy group, never one per strategy. The hub owns that
single connection, subscribes once for the union of every worker's instruments,
aggregates ticks into candles, and fans the **completed candles** out to bounded
per-worker queues — and, for workers that ask for it, the **raw ticks** too.

Fanning out candles rather than raw ticks is deviation **D9** from the spec,
which describes distributing normalised ticks. Aggregating once, centrally,
means every worker provably sees the same bars: two workers cannot disagree
about the 09:16 close because they each rebuilt it from their own view of the
tick stream. It also satisfies the spec's "prevent duplicate candle publication"
rule structurally, since there is exactly one aggregator per instrument in the
group. The cost is that a worker cannot pick its own timeframe off the raw
stream; it aggregates further from completed bars instead.

The tick channel (Phase 3 Part 2b-ii-A)
---------------------------------------
D9 reserved exactly this: "a tick channel can be added alongside this one
without reshaping the queues". The ported :class:`~common.engine.engine.
TradingEngine` is tick-driven — underlying ticks build its candles, option ticks
drive risk management and pending-entry fills — so without it the engine has no
data source at all.

It is a **second queue** per worker rather than a mixed-type one: the two have
very different arrival rates and therefore different depths, and keeping them
separate means candle consumers are bit-for-bit unaffected by this addition.
Opt-in, so a worker that does not ask for ticks pays nothing on the callback
path.

**The trade-off, stated rather than buried.** A worker driving the engine off
this channel aggregates the underlying *again*, beside the hub's own aggregator,
so a dropped tick silently changes that worker's OHLC — the disagreement D9
exists to prevent. Three mitigations: the depth is sized from a measured tick
rate (see ``DEFAULT_TICK_MAX_DEPTH``), every drop is counted and surfaced in
``queue_stats()``, and — since Part 2b-ii-B-1 — a :class:`~common.feed.queues.
TickDropNotice` is published down the same channel so the engine in the child
process can latch its entries off for the day. Until that notice existed this
paragraph claimed the entry block as fact while no code performed it; the claim is
now backed by ``tests/integration/test_tick_drop_blocks_entries.py``. See the
runbook's deviation and limitation entries for Parts 2b-ii-A and 2b-ii-B-1.

Dynamic subscription, and who calls the adapter
-----------------------------------------------
The engine picks an option contract at runtime and subscribes to it mid-session,
but the hub subscribes a fixed union at :meth:`start`. :meth:`request_subscription`
closes that gap the same way Part 1's ``request_stop()`` and Part 2b-i's
``request_square_off()`` close theirs: it appends to a queue and returns, never
touching the adapter. The actual ``adapter.subscribe()`` happens on
:meth:`_apply_pending_subscriptions`, on the thread that owns the connection —
never from the calling thread.

That restraint is deliberate. ``dhanhq`` 2.2.0's ``subscribe_symbols`` does route
through ``asyncio.run_coroutine_threadsafe`` and so *would* tolerate a
cross-thread call, unlike ``close_connection`` — but relying on that would put
this repository's ownership rule back in the SDK's hands, where an upgrade could
quietly revoke it. This hub never takes that dependency: every thread other than
the feed-owning one only ever touches the thread-safe ``deque`` below.

**Applied without depending on an unrelated tick (straddle_920 port
correction).** Before this fix, ``_apply_pending_subscriptions`` ran only at
the top of :meth:`on_tick` — so a request made while the market stream was
silent (or an instrument's own first tick simply hadn't arrived yet) sat
unapplied for as long as the stream stayed quiet, the exact class of failure
recorded against commit ``bcd2d5a`` and reproducible again for any dynamically
selected contract. It is now **also** wired as the adapter's ``on_idle``
callback (see :meth:`~common.market_data.dhan.DhanMarketFeedAdapter.start`),
invoked on this same feed-owning thread on a bounded poll cadence
(:data:`~common.market_data.dhan.IDLE_POLL_SECONDS`, 1 second) regardless of
whether any tick arrives — a genuine wake-up, not merely the supervisor's
``STUCK_SUBSCRIPTION_SECONDS`` alarm, which stays wired as defence-in-depth on
top of this, not as the mechanism itself.

The hub deliberately does no strategy work on the callback path. It validates,
aggregates and publishes — anything slower belongs in a worker.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from common.candles import CandleAggregator
from common.logging import get_logger
from common.market_data.adapter import MarketFeedAdapter
from common.models import Candle, Tick

from .queues import (
    DEFAULT_TICK_MAX_DEPTH,
    BoundedWorkerQueue,
    FeedGapNotice,
    QueueStats,
    TickDropNotice,
    drop_notice_cadence,
)

_log = get_logger(__name__)


@dataclass(frozen=True)
class WorkerChannel:
    """One registered worker: which instruments it wants, and where to send them.

    ``security_ids`` is the immutable registration record — what the worker asked
    for before anything started. ``dynamic_ids`` is what it has asked for *since*,
    through :meth:`SharedFeedHub.request_subscription`. Keeping them apart means
    the dataclass stays frozen (nobody can swap a channel's queue mid-run) while
    routing can still grow, and a reader can always tell a runtime subscription
    from a configured one.
    """

    strategy_id: str
    security_ids: frozenset[str]
    queue: BoundedWorkerQueue
    #: Opt-in raw-tick channel. ``None`` means this worker never receives ticks,
    #: and costs nothing on the callback path.
    tick_queue: BoundedWorkerQueue | None = None
    #: Instruments subscribed at runtime. Mutated only on the feed thread, from
    #: :meth:`SharedFeedHub._apply_pending_subscriptions`.
    dynamic_ids: set[str] = field(default_factory=set)
    #: The exchange segment/mode ``security_ids`` (the *initial*, configured
    #: registration — never ``dynamic_ids``) should be subscribed under.
    #: ``None`` means "the adapter's default", which is what every caller before
    #: Phase 10's feed fix meant and is still correct for a recorded/fixture
    #: channel. An options runtime must set ``segment`` explicitly: the
    #: adapter's own default segment serves its *option* subscriptions, and an
    #: underlying index subscribed under it silently receives nothing — see
    #: :meth:`SharedFeedHub._grouped_initial_subscriptions`.
    segment: int | None = None
    mode: int | None = None
    #: Whether this worker consumes completed candles from :attr:`queue`.
    #: ``False`` marks a tick-only worker — one whose child process is handed
    #: only ``tick_queue.raw`` and which reads through
    #: :class:`~common.engine.hub_feed.HubTickFeed`. The hub then never
    #: publishes candles to ``queue``, so a queue nobody drains cannot fill,
    #: drop-oldest, and report an overflow that means nothing for this worker.
    #: The queue object is still built (it stays non-optional, and the
    #: supervisors' release path is unchanged); it simply stays empty.
    receive_candles: bool = True

    def wants(self, security_id: str) -> bool:
        return security_id in self.security_ids or security_id in self.dynamic_ids


class SharedFeedHub:
    """Owns one feed adapter and fans completed candles out to workers."""

    def __init__(
        self,
        adapter: MarketFeedAdapter,
        *,
        interval_seconds: int = 60,
    ) -> None:
        self._adapter = adapter
        self._interval_seconds = interval_seconds
        self._channels: list[WorkerChannel] = []
        #: One aggregator per instrument — the single source of every bar.
        self._aggregators: dict[str, CandleAggregator] = {}
        #: Runtime subscription requests awaiting the feed thread. ``deque`` because
        #: ``append``/``popleft`` are atomic under the GIL, so a request can be
        #: enqueued from any thread without a lock on the callback path.
        #: ``(strategy_id, security_id, segment, mode)``; either may be ``None``
        #: for "the adapter's default", which is what every pre-Part-5 caller means.
        self._pending_subscriptions: deque[tuple[str, str, int | None, int | None]] = deque()
        #: Monotonic reading when the pending queue last went from empty to
        #: non-empty, or ``None`` when it is empty. Read by the supervisor to
        #: alarm on runbook limitation 15 — a subscription needs a tick to be
        #: applied, so on a silent feed one can wait for ever and the engine's
        #: pending entry simply never fills. Monotonic so a clock adjustment
        #: mid-session cannot invent or erase an age.
        self._pending_since: float | None = None
        #: strategy_id -> the drop total at which that worker was last notified.
        self._last_drop_notice: dict[str, int] = {}
        self.tick_count = 0
        self.candle_count = 0
        self.ticks_published = 0
        self.gap_candles_discarded = 0
        self.subscriptions_applied = 0
        #: A pending subscription the adapter rejected (e.g. a genuine
        #: conflicting-segment reassignment) — observable rather than a silent
        #: drop, and isolated per-request so one bad request cannot take the
        #: whole feed down. See :meth:`_apply_pending_subscriptions`.
        self.subscriptions_rejected = 0
        self.last_tick_at: datetime | None = None
        #: Monotonic timestamp of the last time a pending subscription was
        #: actually applied (successfully or rejected) — distinct from
        #: ``last_tick_at``, so a caller/test can prove application happened
        #: on an idle poll with no tick at all. ``None`` until the first one.
        self.last_subscription_service_at: float | None = None

    # ------------------------------------------------------------ wiring
    def register(self, channel: WorkerChannel) -> None:
        if any(c.strategy_id == channel.strategy_id for c in self._channels):
            raise ValueError(f"Worker {channel.strategy_id!r} is already registered with the hub")
        self._channels.append(channel)

    @property
    def channels(self) -> tuple[WorkerChannel, ...]:
        return tuple(self._channels)

    def subscription_union(self) -> frozenset[str]:
        """Every instrument any registered worker needs — subscribed once."""
        union: set[str] = set()
        for channel in self._channels:
            union.update(channel.security_ids)
        return frozenset(union)

    def _grouped_initial_subscriptions(
        self,
    ) -> list[tuple[int | None, int | None, list[str]]]:
        """Group every channel's *initial* ids by their declared segment/mode.

        A single flat ``adapter.subscribe(sorted(union))`` call cannot express
        that the underlying index and its option contracts live in different
        exchange segments — the whole set would go out under one segment, and
        whichever instrument does not actually live there would silently
        receive nothing (see :meth:`~common.market_data.adapter.
        MarketFeedAdapter.subscribe`'s docstring; this is exactly the bug that
        left the group's own underlying subscription silent while the adapter
        itself connected, authenticated and stayed open).

        Every channel that leaves ``segment``/``mode`` unset (``None``, the
        default) collapses into a single ``(None, None)`` group holding the
        whole union — byte-identical to the call this replaces, so a fixture
        or recorded-tape channel that never named a segment is unaffected.
        """
        resolved: dict[str, tuple[int | None, int | None]] = {}
        for channel in self._channels:
            key = (channel.segment, channel.mode)
            for security_id in channel.security_ids:
                existing = resolved.get(security_id)
                if existing is not None and existing != key:
                    raise RuntimeError(
                        f"Instrument {security_id!r} is registered under conflicting "
                        f"segment/mode pairs {existing} and {key}; one instrument "
                        "cannot live in two segments at once."
                    )
                resolved[security_id] = key

        groups: dict[tuple[int | None, int | None], list[str]] = {}
        for security_id, key in resolved.items():
            groups.setdefault(key, []).append(security_id)
        return [(segment, mode, sorted(ids)) for (segment, mode), ids in groups.items()]

    # ------------------------------------------------------------ running
    def start(self) -> None:
        """Subscribe the union and run the adapter until it stops."""
        union = self.subscription_union()
        if not union:
            raise RuntimeError("Refusing to start the feed hub with no registered workers")
        for segment, mode, security_ids in self._grouped_initial_subscriptions():
            if segment is None and mode is None:
                # Byte-identical to the call this replaces: a minimal test
                # double implementing only subscribe(ids), the pre-Phase-10
                # shape, still works unchanged.
                self._adapter.subscribe(security_ids)
            else:
                self._adapter.subscribe(security_ids, segment=segment, mode=mode)
        _log.info(
            "feed hub starting instruments=%d workers=%d",
            len(union),
            len(self._channels),
        )
        # Anything requested before the first tick — a worker recovering an open
        # position on startup, say — is applied here rather than waiting for a
        # tick that may be a minute away. This is still the feed thread.
        self._apply_pending_subscriptions()
        # ``on_idle``: the real fix for a request made mid-session, while the
        # market stream is silent. The adapter calls this on the same
        # feed-owning thread on a bounded poll cadence regardless of whether
        # any tick arrives — see ``DhanMarketFeedAdapter.start``'s own
        # docstring. A minimal test double whose ``start`` does not accept
        # ``on_idle`` at all is not supported here; every adapter this hub
        # ships against (Dhan, recorded, reconnecting) does.
        self._adapter.start(self.on_tick, on_idle=self._apply_pending_subscriptions)

    def request_stop(self) -> None:
        """Ask the feed to finish, without flushing. Safe from another thread.

        Deliberately **not** ``stop()``. The two halves of a shutdown belong to
        different threads and different moments: this one only signals, and may be
        called while :meth:`start` is still blocked on another thread — a signal
        handler's case. Flushing here as well would read the aggregators while the
        feed thread is still writing to them, which is a data race that would
        surface as a corrupt final bar rather than as a crash.

        The caller signals, waits for the feed thread to return, and only then
        calls :meth:`stop`.
        """
        self._adapter.request_stop()

    def stop(self) -> None:
        """Stop the adapter and flush every partial bar to its subscribers.

        For the thread that ran :meth:`start`, or for any thread once that call
        has returned. See :meth:`request_stop` for the other case.
        """
        self._adapter.stop()
        for security_id, aggregator in self._aggregators.items():
            for candle in aggregator.flush(security_id):
                self._fan_out(candle)

    def mark_feed_gap(self) -> int:
        """Invalidate every open bar because the feed was lost.

        Wired to :class:`~common.feed.reconnect.ReconnectingFeed` as its
        ``on_feed_gap`` hook. Any bar open during the outage is missing the ticks
        that occurred while the socket was down, so publishing it would emit a
        bar stitched across a hole — see the aggregator for the full argument.

        Note that the aggregators are **not** rebuilt here, and must not be: each
        one's record of what it has already published is what makes duplicate
        publication across a reconnect structurally impossible. A fresh
        aggregator would have forgotten.
        """
        discarded = 0
        for security_id, aggregator in self._aggregators.items():
            discarded += aggregator.mark_feed_gap(security_id)
        self.gap_candles_discarded += discarded
        return discarded

    # ------------------------------------------------------ subscription
    def request_subscription(
        self,
        strategy_id: str,
        security_id: str,
        *,
        segment: int | None = None,
        mode: int | None = None,
    ) -> None:
        """Ask for an instrument to be added mid-session. Safe from **any** thread.

        ``segment`` names the exchange segment the instrument lives in. An
        options runtime must pass it: the hub's default is the *underlying's*
        segment (``IDX_I``), and an option contract subscribed there delivers
        nothing at all rather than erroring. ``None`` keeps the adapter default.

        ``mode`` names the subscription mode, and an options runtime must pass
        that too: the hub's default is the underlying's Ticker mode, which carries
        no order book, so a contract subscribed there streams prices the paper fill
        model then has to price off last price instead of bid/ask.

        Enqueues and returns. It never calls the adapter, for the same reason
        :meth:`request_stop` never closes the connection: the thread that called
        ``start()`` owns the loop, and this one may be a supervisor thread or —
        via the control queue — another process entirely. The request is applied
        by :meth:`_apply_pending_subscriptions`, which the feed's own loop calls
        both at the top of :meth:`on_tick` (a real tick) and as the adapter's
        ``on_idle`` callback (a bounded poll, regardless of tick arrival — see
        the module docstring's "Applied without depending on an unrelated tick"
        section). A request is therefore applied within one poll interval
        (:data:`~common.market_data.dhan.IDLE_POLL_SECONDS`, 1 second for the
        live adapter) even on a completely silent market — no longer only "when
        the next tick happens to arrive".
        """
        if self._pending_since is None:
            self._pending_since = time.monotonic()
        self._pending_subscriptions.append((strategy_id, security_id, segment, mode))

    def pending_subscription_age_seconds(self) -> float:
        """How long the oldest unapplied subscription has been waiting.

        ``0.0`` when nothing is pending. Safe from any thread: it reads two
        values that are only ever written on the feed thread, and a torn read
        can only make the age briefly wrong, never make a false alarm stick —
        the caller re-reads on its next poll.
        """
        started = self._pending_since
        if started is None or not self._pending_subscriptions:
            return 0.0
        return max(0.0, time.monotonic() - started)

    def _apply_pending_subscriptions(self) -> None:
        """Drain the request queue into the adapter. **Feed thread only.**

        Called from two places: at the top of :meth:`on_tick` (unchanged,
        applies promptly on a real tick) and, since the dynamic-subscription
        fix, as the adapter's ``on_idle`` callback — invoked on this same
        thread on a bounded poll cadence regardless of whether any tick
        arrives (see :meth:`~common.market_data.dhan.DhanMarketFeedAdapter.
        start`). That is what makes a request applied without depending on an
        unrelated tick a real, tested property rather than an alarm about its
        absence.

        Each request is applied **independently** — a rejection (a genuine
        conflicting-segment reassignment; see
        :meth:`~common.market_data.dhan.DhanMarketFeedAdapter.subscribe`) is
        caught, counted in :attr:`subscriptions_rejected` and logged, rather
        than left to propagate and tear down the whole feed thread over one
        bad request while other, unrelated pending requests are still queued.
        """
        applied_any = False
        while True:
            try:
                (
                    strategy_id,
                    security_id,
                    segment,
                    mode,
                ) = self._pending_subscriptions.popleft()
            except IndexError:
                # Drained. Clear the clock so the next request times from itself
                # rather than from a request that has already been served.
                self._pending_since = None
                break

            applied_any = True
            channel = next(
                (c for c in self._channels if c.strategy_id == strategy_id),
                None,
            )
            if channel is None:
                _log.warning(
                    "ignoring a subscription request for unregistered worker %r (%s)",
                    strategy_id,
                    security_id,
                )
                continue
            if channel.wants(security_id):
                continue  # already routed to this worker

            try:
                # Union semantics in the adapter mean a second worker asking for
                # an instrument the group already holds sends nothing to the
                # broker.
                self._adapter.subscribe([security_id], segment=segment, mode=mode)
            except Exception:
                self.subscriptions_rejected += 1
                _log.exception(
                    "subscription rejected strategy_id=%s security_id=%s segment=%s mode=%s",
                    strategy_id,
                    security_id,
                    "default" if segment is None else segment,
                    "default" if mode is None else mode,
                )
                continue

            channel.dynamic_ids.add(security_id)
            self.subscriptions_applied += 1
            _log.info(
                "subscribed at runtime strategy_id=%s security_id=%s segment=%s mode=%s",
                strategy_id,
                security_id,
                "default" if segment is None else segment,
                "default" if mode is None else mode,
            )
        if applied_any:
            self.last_subscription_service_at = time.monotonic()

    def drop_subscription(self, strategy_id: str, security_id: str) -> None:
        """Stop routing an instrument to one worker. **Feed thread only.**

        Deliberately does *not* unsubscribe the adapter: the subscription is the
        group's, not this worker's, and dropping it here would starve any other
        worker holding the same instrument. Only the routing entry goes.
        """
        for channel in self._channels:
            if channel.strategy_id == strategy_id:
                channel.dynamic_ids.discard(security_id)
                return

    # ------------------------------------------------------------ callback
    def on_tick(self, tick: Tick) -> None:
        """Feed-callback path: validate minimally, aggregate, publish. Nothing else."""
        # First, so an instrument requested since the last tick is routed on this
        # one rather than the next. This is the thread that owns the connection.
        self._apply_pending_subscriptions()

        self.tick_count += 1
        self.last_tick_at = tick.exchange_time

        aggregator = self._aggregators.get(tick.security_id)
        if aggregator is None:
            aggregator = CandleAggregator(interval_seconds=self._interval_seconds)
            self._aggregators[tick.security_id] = aggregator

        completed = aggregator.add(tick)
        if completed is not None:
            self._fan_out(completed)

        self._fan_out_tick(tick)

    def _fan_out_tick(self, tick: Tick) -> None:
        """Publish the raw tick to every opted-in worker that wants the instrument."""
        for channel in self._channels:
            if channel.tick_queue is None or not channel.wants(tick.security_id):
                continue
            self.ticks_published += 1
            if not channel.tick_queue.publish(tick):
                # Overflow is a health event, never a silent drop — and on this
                # channel it is worse than on the candle one: a worker building
                # its own bars from these ticks gets a quietly wrong OHLC rather
                # than a visibly missing bar. See the module docstring.
                _log.warning(
                    "worker tick queue overflow strategy_id=%s dropped=%d depth=%d",
                    channel.strategy_id,
                    channel.tick_queue.dropped,
                    channel.tick_queue.depth(),
                )
                # And tell the worker, because a log line in the *supervisor's*
                # process is not something the engine in the child can act on. The
                # notice rides the same queue as the ticks and the shutdown
                # sentinel; the engine turns it into a refusal to take new entries
                # for the rest of the day.
                #
                # On a cadence, not per drop: the notice is pushed into a queue that
                # is by definition full, so it evicts a real tick. One per drop was
                # measured to cost 7x the ticks for a 6.5% earlier block. See
                # DROP_NOTICE_EVERY.
                self._notify_drop(channel)

    def _notify_drop(self, channel: WorkerChannel) -> None:
        if channel.tick_queue is None:
            return
        dropped = channel.tick_queue.dropped
        last = self._last_drop_notice.get(channel.strategy_id, 0)
        if last != 0 and dropped - last < drop_notice_cadence(channel.tick_queue.max_depth):
            return
        self._last_drop_notice[channel.strategy_id] = dropped
        channel.tick_queue.publish(TickDropNotice(dropped=dropped))

    def broadcast_feed_gap(self, notice: FeedGapNotice) -> None:
        """Publish a feed-level connection-state change to *every* worker's
        tick channel, unconditionally (Phase 6A).

        Unlike :meth:`_fan_out_tick`/:meth:`_notify_drop` (routed to the one
        channel that wants the instrument, or whose own queue overflowed), a
        disconnect or reconnect of the one shared feed affects every worker
        equally — every registered tick channel gets this notice, whether or
        not it currently "wants" any particular instrument. Callable from any
        thread that already owns a tick-publishing moment (the supervisor's
        own feed-health-event callback runs on the feed thread, the same one
        that publishes ticks), matching :meth:`request_subscription`'s own
        any-thread-safe posture for the write side of this hub.
        """
        for channel in self._channels:
            if channel.tick_queue is None:
                continue
            channel.tick_queue.publish(notice)

    def _fan_out(self, candle: Candle) -> None:
        self.candle_count += 1
        for channel in self._channels:
            # ``receive_candles`` first: a tick-only worker never drains this
            # queue, so publishing to it would fill it, drop the oldest, and
            # warn — forever, about a queue that does not exist for its owner.
            if not channel.receive_candles or not channel.wants(candle.security_id):
                continue
            if not channel.queue.publish(candle):
                # Overflow is a health event, never a silent drop.
                _log.warning(
                    "worker queue overflow strategy_id=%s dropped=%d depth=%d",
                    channel.strategy_id,
                    channel.queue.dropped,
                    channel.queue.depth(),
                )

    # ------------------------------------------------------------- health
    def queue_stats(self) -> tuple[QueueStats, ...]:
        """Every queue in the group, candle and tick alike.

        The candle queue keeps ``name=strategy_id`` unchanged, so anything already
        reading these stats sees exactly what it saw before; a tick queue is
        suffixed ``:ticks`` so the two are distinguishable in a heartbeat.

        A channel with ``receive_candles=False`` contributes no candle-queue
        entry at all. The hub never publishes to that queue, so its stats would
        be a permanent zero row for a queue its worker does not have — and a
        heartbeat that reports a queue nobody consumes is worse than one that
        omits it. Its tick queue is reported exactly as any other.
        """
        stats: list[QueueStats] = []
        for channel in self._channels:
            if channel.receive_candles:
                stats.append(channel.queue.stats())
            if channel.tick_queue is not None:
                stats.append(channel.tick_queue.stats())
        return tuple(stats)


def build_channel(
    strategy_id: str,
    security_ids: Sequence[str],
    *,
    max_depth: int = 1000,
    in_process: bool = False,
    tick_channel: bool = False,
    tick_max_depth: int = DEFAULT_TICK_MAX_DEPTH,
    segment: int | None = None,
    mode: int | None = None,
    receive_candles: bool = True,
) -> WorkerChannel:
    """Create a worker channel with its own bounded queue.

    ``tick_channel=True`` adds the opt-in raw-tick queue the ported engine reads;
    the default leaves the channel exactly as Phase 1 built it.

    ``segment``/``mode`` name the exchange segment/subscription mode
    ``security_ids`` should be registered under — see
    :attr:`WorkerChannel.segment` for why an options runtime must set these
    explicitly for its underlying. ``None`` (the default) preserves the
    adapter's own default, unchanged from before this parameter existed.

    ``receive_candles=False`` registers a tick-only worker: the queue is still
    created (see :attr:`WorkerChannel.queue`) but the hub never publishes to
    it. The default keeps every caller that predates this parameter exactly as
    it was.
    """
    q = (
        BoundedWorkerQueue.in_process(strategy_id, max_depth)
        if in_process
        else BoundedWorkerQueue(name=strategy_id, max_depth=max_depth)
    )
    tick_q: BoundedWorkerQueue | None = None
    if tick_channel:
        tick_name = f"{strategy_id}:ticks"
        tick_q = (
            BoundedWorkerQueue.in_process(tick_name, tick_max_depth)
            if in_process
            else BoundedWorkerQueue(name=tick_name, max_depth=tick_max_depth)
        )
    return WorkerChannel(
        strategy_id=strategy_id,
        security_ids=frozenset(security_ids),
        queue=q,
        tick_queue=tick_q,
        segment=segment,
        mode=mode,
        receive_candles=receive_candles,
    )
