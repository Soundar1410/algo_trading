"""Bounded IPC queues between the feed hub and strategy workers.

The spec's rule is narrow and important: a lagging **paper** worker is dropped
or quarantined according to policy rather than being allowed to block the feed,
and queue overflow is a *health event* — never a silent drop.

So the queue is bounded and non-blocking on put. When it is full the oldest
event is discarded to make room, because for market data the freshest bar is the
one that matters; a worker that has fallen behind gains nothing from being fed
stale candles in order. Every drop is counted and surfaced in the heartbeat.

The live policy is deliberately *not* implemented here. The spec requires a
lagging live worker to be blocked from new entries and to raise a critical
alert, which is a different behaviour, and Phase 1 has no live path at all.
"""

from __future__ import annotations

import multiprocessing as mp
import queue
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

DEFAULT_MAX_DEPTH = 1000

#: Depth for a worker's **tick** channel (Phase 3 Part 2b-ii-A). Sized from the
#: only tick-rate evidence this repository has — its own Phase 2 Block 2 live
#: capture, 121 ticks over 30 s for one instrument in ticker mode, ~4 ticks/s.
#: An engine worker holds the underlying plus at most one option contract at a
#: time; sizing generously at ~10 ticks/s/instrument peak gives ~20-30 ticks/s,
#: so this is roughly 60-70 s of buffer at that peak and ~8 minutes at the
#: measured rate (~400 KB pickled).
#:
#: The reasoning is the candle queue's, applied to a different arrival rate:
#: deep enough that a briefly busy worker loses nothing, shallow enough that a
#: wedged one is detected rather than accumulating a day of stale data. A worker
#: more than a minute behind on ticks is producing stale risk decisions anyway.
DEFAULT_TICK_MAX_DEPTH = 2048


@dataclass(frozen=True)
class TickDropNotice:
    """Sent down a worker's tick channel to say that a tick was lost upstream.

    Phase 3 Part 2b-ii-B-1. The drop is detected and counted in the **hub's**
    process, on this object's ``dropped`` counter; the consumer that needs to act
    on it — the engine, which builds its own candles from these ticks and would
    otherwise trade on a quietly wrong OHLC — runs in the child and cannot read
    that counter. The only channel between them is the tick queue itself, which is
    already how the shutdown sentinel travels, so the notice travels in band beside
    it.

    It crosses a pickle boundary, so consumers must match on **type**, not on
    identity with a module-level singleton.

    It is also subject to the drop-oldest policy itself: under sustained overflow a
    notice can age out before the consumer reaches it. That is why it is re-sent on
    a cadence (see :data:`DROP_NOTICE_EVERY`) rather than once — the guarantee is
    "at least one gets through", not "none is lost", and the latch it triggers needs
    only one.
    """

    #: The channel's running drop total at the moment the notice was raised.
    dropped: int


@dataclass(frozen=True)
class FeedGapNotice:
    """Sent down *every* worker's tick channel when the shared feed's own
    connection state changes in a way that must gate a risk-increasing
    engine decision (Phase 6A).

    Unlike :class:`TickDropNotice` (per-channel, raised only for the one
    worker whose own queue overflowed), this is broadcast to every
    registered channel unconditionally — a feed-level disconnect or
    reconnect affects every worker sharing that one feed equally (see
    :meth:`~common.feed.hub.SharedFeedHub.broadcast_feed_gap`).

    ``kind`` is ``"disconnected"`` — raised the instant
    :class:`~common.feed.reconnect.ReconnectingFeed` marks itself
    disconnected, so a worker's own engine can latch a recoverable
    market-data-degraded gate *before* any further risk-increasing
    decision — or ``"resubscribed"`` — raised once a reconnect's own
    resubscription round trip completes. ``"resubscribed"`` is
    confirmation the round trip happened, never proof of fresh data by
    itself: the consumer must still observe a genuinely fresh post-event
    tick for everything it currently needs before treating itself as
    recovered (spec-equivalent to the supervisor's own "fresh ticks before
    degraded clears" rule, applied per-worker rather than only at the
    feed's own aggregate health).

    Crosses a pickle boundary like ``TickDropNotice`` — consumers must
    match on type, not identity. Same bounded/drop-oldest delivery
    guarantee as ``TickDropNotice``: "at least one gets through", not "none
    is lost" — a low-frequency, high-importance event, not a new guarantee
    invented here.
    """

    kind: str
    #: Wall-clock instant this notice was raised — timezone-aware, the
    #: instant a consumer compares its own later tick timestamps against
    #: (never inferred fresh from a naive or missing timestamp; see
    #: :func:`~common.utils.timeutils.is_fresh`).
    at: datetime


#: Send a drop notice on the first drop, then every this-many drops.
#:
#: Publishing one per dropped tick was the first implementation and is **measured**
#: to be the wrong trade: a notice pushed into an already-full queue evicts a real
#: tick, so at the deployed depth of 2048 it cost 358 extra lost ticks per 6000
#: (6.3%) against a lagging consumer, to make the entry block engage 6.5% sooner
#: (4393 ticks vs 4699). A cadence of 8 costs 52 (0.9%) for that same latency.
#: Full comparison in the runbook, deviation D28.
DROP_NOTICE_EVERY = 8


def drop_notice_cadence(max_depth: int) -> int:
    """How many drops between notices on a queue of this depth.

    :data:`DROP_NOTICE_EVERY`, **clamped below half the depth**. The clamp is not a
    tuning knob; it enforces the invariant the cadence depends on. A notice only
    reaches the consumer if it is still inside the retained window when the consumer
    catches up, and roughly one tick is published per drop — so a cadence at or
    above the depth lets every notice be evicted before the next is sent, and the
    drop goes unreported entirely.

    Measured across depths 4-2048 and run lengths 12-4000: a fixed cadence of 8
    reported every overflow at depth 8 and above but **lost the notice at depth 4**;
    clamped, every overflowing case at every depth was reported. Deployed depth is
    ``DEFAULT_TICK_MAX_DEPTH`` (2048), where this returns 8 unchanged — the clamp
    only bites on the shallow queues that appear in tests.
    """
    return max(1, min(DROP_NOTICE_EVERY, max_depth // 2))


@dataclass(frozen=True)
class QueueStats:
    """A snapshot for the heartbeat. Cheap to compute, safe to log."""

    name: str
    depth: int
    max_depth: int
    published: int
    dropped: int
    last_published_at: datetime | None = None

    @property
    def is_overflowing(self) -> bool:
        return self.dropped > 0


@dataclass
class BoundedWorkerQueue:
    """One worker's inbound channel, bounded and drop-oldest on overflow."""

    name: str
    max_depth: int = DEFAULT_MAX_DEPTH
    #: Injected so tests can use a plain queue.Queue and the supervisor a real
    #: multiprocessing.Queue, without the hub knowing which it holds.
    _queue: Any = None
    published: int = field(default=0, init=False)
    dropped: int = field(default=0, init=False)
    last_published_at: datetime | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.max_depth <= 0:
            raise ValueError("max_depth must be positive")
        if self._queue is None:
            # Workers are always created from the spawn context.  A queue built
            # from Linux's default fork context cannot be pickled into a spawned
            # child (``SemLock created in a fork context``), even though the same
            # code happens to work on macOS where spawn is already the default.
            self._queue = mp.get_context("spawn").Queue(maxsize=self.max_depth)

    @classmethod
    def in_process(cls, name: str, max_depth: int = DEFAULT_MAX_DEPTH) -> BoundedWorkerQueue:
        """A same-process queue, for unit tests that need no child process."""
        return cls(name=name, max_depth=max_depth, _queue=queue.Queue(maxsize=max_depth))

    @property
    def raw(self) -> Any:
        """The underlying queue, to hand to a child process."""
        return self._queue

    def publish(self, item: object) -> bool:
        """Enqueue without blocking. Returns False if something had to be dropped.

        Never raises and never blocks: this runs on the feed callback path, and
        a blocked hub stalls every other worker too.
        """
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self._drop_oldest()
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                # A competing consumer refilled it. Drop the new item rather
                # than loop: the next candle is moments away.
                self.dropped += 1
                return False
            self.published += 1
            self.last_published_at = datetime.now(UTC)
            return False
        self.published += 1
        self.last_published_at = datetime.now(UTC)
        return True

    def _drop_oldest(self) -> None:
        try:
            self._queue.get_nowait()
            self.dropped += 1
        except queue.Empty:  # pragma: no cover - only under a racing consumer
            pass

    def get(self, timeout: float | None = None) -> Any:
        """Block for one item. Raises ``queue.Empty`` on timeout."""
        return self._queue.get(timeout=timeout)

    def drain(self) -> int:
        """Discard every currently-queued item without blocking. Returns how
        many were discarded.

        For a worker being respawned after a crash: whatever is still queued
        for it was addressed to a process that no longer exists, and by the
        time a new one starts it is stale market data — feeding it in would
        corrupt candle building and could trip an elapsed-time gate meant for
        real ticks. Deliberately does not touch :attr:`dropped`: a drain is a
        deliberate discard on the supervisor's own decision, not an overflow
        the feed callback path measured.
        """
        discarded = 0
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            discarded += 1
        return discarded

    def depth(self) -> int:
        """Approximate depth. ``qsize`` is unavailable on some platforms."""
        try:
            return int(self._queue.qsize())
        except (NotImplementedError, OSError):  # pragma: no cover - macOS is fine
            return -1

    def stats(self) -> QueueStats:
        return QueueStats(
            name=self.name,
            depth=self.depth(),
            max_depth=self.max_depth,
            published=self.published,
            dropped=self.dropped,
            last_published_at=self.last_published_at,
        )
