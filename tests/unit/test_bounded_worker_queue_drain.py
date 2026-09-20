"""``BoundedWorkerQueue.drain()`` — discard whatever is queued, without
blocking and without counting it as an overflow drop.

Added for the supervisor's worker-restart path: whatever was queued for a
worker that has died is stale by the time a respawned process could read
it, and must not be fed to it — see
``IntradayOptionsSupervisor._restart_worker``.
"""

from __future__ import annotations

import queue
import time

import pytest

from common.feed.queues import BoundedWorkerQueue


def test_drain_discards_everything_and_returns_the_count():
    q = BoundedWorkerQueue.in_process("s1", max_depth=8)
    for i in range(5):
        assert q.publish(i) is True

    discarded = q.drain()

    assert discarded == 5
    assert q.depth() == 0


def test_drain_does_not_count_as_a_dropped_overflow():
    """A drain is a deliberate supervisor decision, not the feed callback
    path measuring a queue that could not keep up — the two must stay
    distinguishable in the heartbeat's ``dropped_events``."""
    q = BoundedWorkerQueue.in_process("s1", max_depth=8)
    q.publish("x")

    q.drain()

    assert q.dropped == 0


def test_drain_on_an_empty_queue_is_a_harmless_no_op():
    q = BoundedWorkerQueue.in_process("s1", max_depth=8)
    assert q.drain() == 0
    assert q.dropped == 0


def test_the_queue_is_usable_again_immediately_after_a_drain():
    q = BoundedWorkerQueue.in_process("s1", max_depth=2)
    q.publish("stale")
    q.drain()

    assert q.publish("fresh") is True
    assert q.get(timeout=0.1) == "fresh"


# ============================================ the feeder-thread race (D92)
class _FeederBufferedQueue:
    """A queue double that models ``multiprocessing.Queue``'s feeder thread.

    The real thing does **not** write to its pipe on ``put_nowait``: it appends
    to an in-process buffer and wakes a background feeder thread, which pickles
    and writes later. ``get_nowait`` reads the *pipe*. So there is a window,
    entirely ordinary and entirely invisible, in which an item has been
    published, is not yet readable, and has certainly not been discarded.

    That window is what :meth:`~common.feed.queues.BoundedWorkerQueue.drain`
    used to lose to. Modelling it here rather than racing the real feeder is
    the difference between a proof and a coin toss: against a real
    ``mp.Queue`` the stale item survived a drain in 197 of 200 trials, which is
    emphatic but still not 200. :meth:`flush` is the feeder waking up, under
    the test's control instead of the scheduler's.
    """

    def __init__(self, maxsize: int = 0) -> None:
        self.maxsize = maxsize
        self._visible: list[object] = []
        self._buffer: list[object] = []
        self.cancelled = False
        self.closed = False

    # -- the producer side: buffered, exactly like the real feeder
    def put_nowait(self, item: object) -> None:
        if self.maxsize and len(self._visible) + len(self._buffer) >= self.maxsize:
            raise queue.Full
        self._buffer.append(item)

    # -- the feeder thread, made explicit
    def flush(self) -> None:
        self._visible.extend(self._buffer)
        self._buffer.clear()

    # -- the consumer side: sees only what the feeder has already written
    def get_nowait(self) -> object:
        if not self._visible:
            raise queue.Empty
        return self._visible.pop(0)

    def get(self, timeout: float | None = None) -> object:
        self.flush()  # any real timeout is long enough for the feeder
        return self.get_nowait()

    def qsize(self) -> int:
        return len(self._visible) + len(self._buffer)

    # -- the disposal surface BoundedWorkerQueue uses
    def cancel_join_thread(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        self.closed = True


def _buffered_queue(max_depth: int = 8) -> BoundedWorkerQueue:
    return BoundedWorkerQueue(
        name="s1",
        max_depth=max_depth,
        _queue=_FeederBufferedQueue(maxsize=max_depth),
        _queue_factory=lambda: _FeederBufferedQueue(maxsize=max_depth),
    )


def test_an_item_still_in_the_feeder_buffer_does_not_survive_a_drain():
    """D92, the regression itself — deterministic, with no timing anywhere.

    A worker died; something was published for it moments before. ``drain()``
    saw an empty pipe, stopped, and the item then reached the *respawned*
    worker — stale market data fed to a fresh engine, which
    ``BoundedWorkerQueue.drain``'s own docstring says "would corrupt candle
    building and could trip an elapsed-time gate meant for real ticks".

    Reported as a 1-in-10 flake in
    ``test_supervisor_worker_liveness.py::test_a_dead_worker_is_respawned_
    reaped_drained_and_resumed`` ("DID NOT RAISE Empty"). It flaked rather
    than failed only because the death and liveness check in between usually
    gave the feeder time to flush; the defect itself is near-certain.
    """
    q = _buffered_queue()
    q.publish("stale")

    q.drain()

    # ``get`` on the double flushes first — the feeder waking up. Whatever it
    # was holding must be gone with the queue that held it, never delivered
    # into the fresh one.
    with pytest.raises(queue.Empty):
        q.get(timeout=0.1)


def test_a_drain_disposes_of_the_old_queue_rather_than_leaking_its_feeder():
    """The old queue is abandoned deliberately: ``cancel_join_thread`` before
    ``close``, never ``join_thread``.

    ``join_thread`` would flush the buffer into a pipe nobody is reading and
    block when it fills — measured in this repository at ~65 KB of undelivered
    ticks, "a few hundred events" (``IntradayOptionsSupervisor.
    _abandon_undelivered_events``). A restart path that can hang is exactly
    what this must not become.
    """
    q = _buffered_queue()
    old = q.raw
    q.publish("stale")

    q.drain()

    assert q.raw is not old, "drain must replace the queue, not merely empty it"
    assert old.cancelled, "the old feeder must be cancelled, never joined"
    assert old.closed


def test_the_replacement_queue_is_usable_and_keeps_the_channels_counters():
    q = _buffered_queue()
    q.publish("stale")
    published_before = q.published

    q.drain()

    assert q.publish("fresh") is True
    assert q.get(timeout=0.1) == "fresh"
    # published/dropped are the channel's cumulative health counters, not the
    # queue object's — a drain replaces the queue without rewriting history.
    assert q.published == published_before + 1
    assert q.dropped == 0


# ------------------------------------------- against the real mp.Queue
def test_drain_beats_a_real_multiprocessing_feeder_every_time():
    """The same property against the genuine article, no double involved.

    Pre-fix this failed 197 times in 200; a handful of trials is therefore
    already conclusive, and 50 leaves no room at all for luck.
    """
    for _ in range(50):
        q = BoundedWorkerQueue(name="s1", max_depth=64)
        q.publish(object())

        q.drain()

        with pytest.raises(queue.Empty):
            q.get(timeout=0.05)


def test_draining_a_deep_queue_full_of_undelivered_items_returns_promptly():
    """2048 items is the deployed tick-channel depth, far past the ~65 KB
    pipe buffer where a ``join_thread`` would wedge. This must not hang."""
    q = BoundedWorkerQueue(name="s1:ticks", max_depth=2048)
    for i in range(2000):
        q.publish(("x" * 200, i))

    started = time.monotonic()
    q.drain()
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"drain took {elapsed:.2f}s — it is waiting on the feeder"
    with pytest.raises(queue.Empty):
        q.get(timeout=0.05)
