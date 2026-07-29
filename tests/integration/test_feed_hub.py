"""Hub fan-out: identical bars, bounded queues, counted overflow.

This is deviation D9 under test — the hub publishes *completed candles*, not
ticks, so the property that matters is that two workers subscribed to the same
instrument receive byte-identical bars.
"""

from __future__ import annotations

import queue as queue_module
from pathlib import Path

import pytest

from common.feed import BoundedWorkerQueue, SharedFeedHub
from common.feed.hub import build_channel
from common.market_data import RecordedFeedAdapter, load_tick_tape
from common.models import Candle


@pytest.fixture
def tape(tick_tape_path: Path):
    return load_tick_tape(tick_tape_path)


def _drain(q: BoundedWorkerQueue) -> list[Candle]:
    out: list[Candle] = []
    while True:
        try:
            out.append(q.get(timeout=0.05))
        except queue_module.Empty:
            return out


# ------------------------------------------------------------- the tape
def test_the_recorded_tape_loads(tape):
    assert len(tape) == 24
    assert tape[0].security_id == "99926000"


def test_a_malformed_tape_is_rejected_with_its_index(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text('[{"security_id": "1", "instrument": "X"}]')
    with pytest.raises(ValueError, match="entry 0"):
        load_tick_tape(bad)


def test_a_tape_that_is_not_a_list_is_rejected(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"security_id": "1"}')
    with pytest.raises(ValueError, match="must contain a JSON list"):
        load_tick_tape(bad)


# ------------------------------------------------------------- fan-out
def test_completed_candles_reach_a_subscribed_worker(tape):
    hub = SharedFeedHub(RecordedFeedAdapter(tape))
    channel = build_channel("st01", ["99926000"], in_process=True)
    hub.register(channel)
    hub.start()
    hub.stop()

    candles = _drain(channel.queue)
    # Six one-minute buckets: five completed by the next bucket's first tick,
    # the sixth flushed at stop.
    assert len(candles) == 6
    assert all(isinstance(c, Candle) for c in candles)


def test_every_worker_sees_identical_bars(tape):
    """The whole point of aggregating centrally (D9)."""
    hub = SharedFeedHub(RecordedFeedAdapter(tape))
    first = build_channel("st01", ["99926000"], in_process=True)
    second = build_channel("st02", ["99926000"], in_process=True)
    hub.register(first)
    hub.register(second)
    hub.start()
    hub.stop()

    assert _drain(first.queue) == _drain(second.queue)


def test_a_worker_only_receives_instruments_it_subscribed_to(tape):
    hub = SharedFeedHub(RecordedFeedAdapter(tape))
    wanted = build_channel("st01", ["99926000"], in_process=True)
    unwanted = build_channel("st02", ["OTHER"], in_process=True)
    hub.register(wanted)
    hub.register(unwanted)
    hub.start()
    hub.stop()

    assert len(_drain(wanted.queue)) == 6
    assert _drain(unwanted.queue) == []


def test_the_first_candle_matches_the_tape(tape):
    hub = SharedFeedHub(RecordedFeedAdapter(tape))
    channel = build_channel("st01", ["99926000"], in_process=True)
    hub.register(channel)
    hub.start()
    hub.stop()

    first = _drain(channel.queue)[0]
    assert (first.open, first.high, first.low, first.close) == (100.0, 101.5, 99.5, 100.5)
    assert first.tick_count == 4


# ------------------------------------------------------- subscriptions
def test_the_hub_subscribes_the_union_once(tape):
    adapter = RecordedFeedAdapter(tape)
    hub = SharedFeedHub(adapter)
    hub.register(build_channel("st01", ["A", "B"], in_process=True))
    hub.register(build_channel("st02", ["B", "C"], in_process=True))

    assert hub.subscription_union() == frozenset({"A", "B", "C"})


def test_resubscribing_does_not_duplicate(tape):
    adapter = RecordedFeedAdapter(tape)
    adapter.subscribe(["A", "B"])
    adapter.subscribe(["B", "C"])
    assert adapter.subscribed == frozenset({"A", "B", "C"})


def test_registering_the_same_strategy_twice_is_refused(tape):
    hub = SharedFeedHub(RecordedFeedAdapter(tape))
    hub.register(build_channel("st01", ["99926000"], in_process=True))
    with pytest.raises(ValueError, match="already registered"):
        hub.register(build_channel("st01", ["99926000"], in_process=True))


def test_starting_with_no_workers_is_refused(tape):
    hub = SharedFeedHub(RecordedFeedAdapter(tape))
    with pytest.raises(RuntimeError, match="no registered workers"):
        hub.start()


# ----------------------------------------------------- duplicate ticks
def test_duplicate_and_backwards_ticks_are_suppressed_and_counted(tape):
    doubled = [tape[0], tape[0], tape[1], tape[0]]
    adapter = RecordedFeedAdapter(doubled)
    adapter.subscribe(["99926000"])
    received: list[object] = []
    adapter.start(received.append)

    assert adapter.delivered_count == 2
    assert adapter.duplicate_count == 1
    assert adapter.out_of_order_count == 1


# ------------------------------------------------------------ overflow
def test_a_full_queue_drops_the_oldest_and_counts_it():
    q = BoundedWorkerQueue.in_process("st01", max_depth=2)
    assert q.publish("a") is True
    assert q.publish("b") is True
    assert q.publish("c") is False  # overflow

    assert q.dropped == 1
    assert q.stats().is_overflowing


def test_overflow_keeps_the_freshest_data():
    q = BoundedWorkerQueue.in_process("st01", max_depth=2)
    for item in ("a", "b", "c"):
        q.publish(item)
    assert [q.get(timeout=0.05), q.get(timeout=0.05)] == ["b", "c"]


def test_publishing_never_blocks_the_feed():
    """A wedged worker must not stall every other worker in the group."""
    q = BoundedWorkerQueue.in_process("st01", max_depth=1)
    for _ in range(100):
        q.publish("candle")  # would deadlock if this blocked
    assert q.dropped == 99


def test_queue_stats_report_depth_and_counts():
    q = BoundedWorkerQueue.in_process("st01", max_depth=4)
    q.publish("a")
    stats = q.stats()
    assert stats.name == "st01"
    assert stats.published == 1
    assert stats.dropped == 0
    assert stats.max_depth == 4
    assert not stats.is_overflowing


def test_a_non_positive_queue_depth_is_refused():
    with pytest.raises(ValueError, match="must be positive"):
        BoundedWorkerQueue.in_process("st01", max_depth=0)


def test_hub_counts_ticks_and_candles(tape):
    hub = SharedFeedHub(RecordedFeedAdapter(tape))
    hub.register(build_channel("st01", ["99926000"], in_process=True))
    hub.start()
    hub.stop()

    assert hub.tick_count == 24
    assert hub.candle_count == 6
