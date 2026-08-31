"""``BoundedWorkerQueue.drain()`` — discard whatever is queued, without
blocking and without counting it as an overflow drop.

Added for the supervisor's worker-restart path: whatever was queued for a
worker that has died is stale by the time a respawned process could read
it, and must not be fed to it — see
``IntradayOptionsSupervisor._restart_worker``.
"""

from __future__ import annotations

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
