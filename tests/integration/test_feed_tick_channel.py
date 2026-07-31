"""The hub's opt-in tick channel, and runtime subscription.

Phase 3 Part 2b-ii-A. Two properties matter here and neither is about candles:

* a worker that opts in receives the **raw ticks** as well as the bars, and a
  worker that does not is completely unaffected — including the bars it already
  received, which must stay exactly what D9 produced before this channel existed;
* an instrument the engine picks at runtime becomes subscribed and routed, and
  the adapter is only ever touched from the thread that owns it.

The sizing tests are deliberately grounded in the one tick-rate measurement this
repository actually has — Phase 2 Block 2's live capture, 121 ticks over 30 s for
one instrument (~4 ticks/s) — rather than in the candle channel's assumptions.
"""

from __future__ import annotations

import queue as queue_module
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from common.feed import (
    DEFAULT_TICK_MAX_DEPTH,
    BoundedWorkerQueue,
    SharedFeedHub,
    TickDropNotice,
)
from common.feed.hub import build_channel
from common.market_data import RecordedFeedAdapter, load_tick_tape
from common.models import Candle, Tick

#: Phase 2 Block 2 measured 121 ticks over 30 s for one instrument in ticker
#: mode. Rounded up, and applied per instrument.
MEASURED_TICKS_PER_SECOND = 4

UNDERLYING = "99926000"
OPTION = "45678"


@pytest.fixture
def tape(tick_tape_path: Path):
    return load_tick_tape(tick_tape_path)


def _drain(q: BoundedWorkerQueue) -> list:
    out: list = []
    while True:
        try:
            out.append(q.get(timeout=0.05))
        except queue_module.Empty:
            return out


def _ticks(security_id: str, count: int, *, start: datetime | None = None) -> list[Tick]:
    """A steady stream at the measured live rate."""
    base = start or datetime(2026, 7, 29, 9, 15, tzinfo=UTC)
    step = timedelta(seconds=1 / MEASURED_TICKS_PER_SECOND)
    return [
        Tick(
            security_id=security_id,
            instrument="NIFTY",
            last_price=100.0 + i * 0.05,
            exchange_time=base + step * i,
            received_at=base + step * i,
        )
        for i in range(count)
    ]


# --------------------------------------------------------------- opt-in
def test_an_opted_in_worker_receives_the_raw_ticks_in_order(tape):
    hub = SharedFeedHub(RecordedFeedAdapter(tape))
    channel = build_channel("st01", [UNDERLYING], in_process=True, tick_channel=True)
    hub.register(channel)
    hub.start()
    hub.stop()

    assert channel.tick_queue is not None
    ticks = _drain(channel.tick_queue)
    assert len(ticks) == len(tape)
    assert [t.exchange_time for t in ticks] == [t.exchange_time for t in tape]


def test_the_candle_channel_is_unchanged_by_the_tick_channel(tape):
    """D9 still holds: opting into ticks must not alter a single bar."""
    without = build_channel("plain", [UNDERLYING], in_process=True)
    with_ticks = build_channel("ticky", [UNDERLYING], in_process=True, tick_channel=True)
    hub = SharedFeedHub(RecordedFeedAdapter(tape))
    hub.register(without)
    hub.register(with_ticks)
    hub.start()
    hub.stop()

    plain_bars = _drain(without.queue)
    ticky_bars = _drain(with_ticks.queue)
    assert len(plain_bars) == 6
    assert plain_bars == ticky_bars
    assert all(isinstance(c, Candle) for c in plain_bars)


def test_a_worker_that_did_not_opt_in_has_no_tick_queue(tape):
    hub = SharedFeedHub(RecordedFeedAdapter(tape))
    channel = build_channel("st01", [UNDERLYING], in_process=True)
    hub.register(channel)
    hub.start()
    hub.stop()

    assert channel.tick_queue is None
    assert hub.ticks_published == 0


def test_ticks_are_routed_only_to_workers_that_want_the_instrument(tape):
    hub = SharedFeedHub(RecordedFeedAdapter(tape))
    wanted = build_channel("wants", [UNDERLYING], in_process=True, tick_channel=True)
    other = build_channel("other", ["55555"], in_process=True, tick_channel=True)
    hub.register(wanted)
    hub.register(other)
    hub.start()
    hub.stop()

    assert wanted.tick_queue is not None
    assert other.tick_queue is not None
    assert len(_drain(wanted.tick_queue)) == len(tape)
    assert _drain(other.tick_queue) == []


# ------------------------------------------------- runtime subscription
def test_a_subscription_request_is_not_applied_until_the_next_tick(tape):
    """It enqueues and returns — the adapter is the feed thread's alone."""
    adapter = RecordedFeedAdapter(tape)
    hub = SharedFeedHub(adapter)
    channel = build_channel("st01", [UNDERLYING], in_process=True, tick_channel=True)
    hub.register(channel)

    hub.request_subscription("st01", OPTION)
    assert OPTION not in adapter.subscribed
    assert OPTION not in channel.dynamic_ids
    assert hub.subscriptions_applied == 0

    hub.start()  # drains once before the loop, then on every tick
    hub.stop()

    assert OPTION in adapter.subscribed
    assert OPTION in channel.dynamic_ids
    assert hub.subscriptions_applied == 1


def test_a_dynamically_subscribed_instrument_is_routed(tape):
    option_ticks = _ticks(OPTION, 8)
    adapter = RecordedFeedAdapter([*tape, *option_ticks])
    hub = SharedFeedHub(adapter)
    channel = build_channel("st01", [UNDERLYING], in_process=True, tick_channel=True)
    hub.register(channel)
    hub.request_subscription("st01", OPTION)
    hub.start()
    hub.stop()

    assert channel.tick_queue is not None
    received = _drain(channel.tick_queue)
    assert [t.security_id for t in received].count(OPTION) == len(option_ticks)


def test_without_the_request_the_same_ticks_never_arrive(tape):
    """The control for the test above.

    The recorded adapter delivers only what it is subscribed to, so this proves
    the previous test's option ticks arrived *because of* the runtime
    subscription rather than because the tape happened to contain them.
    """
    option_ticks = _ticks(OPTION, 8)
    adapter = RecordedFeedAdapter([*tape, *option_ticks])
    hub = SharedFeedHub(adapter)
    channel = build_channel("st01", [UNDERLYING], in_process=True, tick_channel=True)
    hub.register(channel)
    hub.start()  # no request_subscription this time
    hub.stop()

    assert channel.tick_queue is not None
    assert [t.security_id for t in _drain(channel.tick_queue)].count(OPTION) == 0


def test_requesting_an_instrument_twice_subscribes_the_adapter_once(tape):
    adapter = RecordedFeedAdapter(tape)
    hub = SharedFeedHub(adapter)
    channel = build_channel("st01", [UNDERLYING], in_process=True, tick_channel=True)
    hub.register(channel)

    hub.request_subscription("st01", OPTION)
    hub.request_subscription("st01", OPTION)
    hub.start()
    hub.stop()

    assert hub.subscriptions_applied == 1


def test_requesting_an_already_registered_instrument_is_a_no_op(tape):
    """Union semantics: the configured subscription is not re-sent."""
    adapter = RecordedFeedAdapter(tape)
    hub = SharedFeedHub(adapter)
    channel = build_channel("st01", [UNDERLYING], in_process=True, tick_channel=True)
    hub.register(channel)

    hub.request_subscription("st01", UNDERLYING)
    hub.start()
    hub.stop()

    assert hub.subscriptions_applied == 0
    assert UNDERLYING not in channel.dynamic_ids


def test_a_request_for_an_unregistered_worker_is_ignored(tape):
    adapter = RecordedFeedAdapter(tape)
    hub = SharedFeedHub(adapter)
    hub.register(build_channel("st01", [UNDERLYING], in_process=True, tick_channel=True))

    hub.request_subscription("nobody", OPTION)
    hub.start()
    hub.stop()

    assert OPTION not in adapter.subscribed
    assert hub.subscriptions_applied == 0


def test_a_subscription_requested_while_no_ticks_flow_is_never_applied():
    """The residual, asserted so it is a tested fact rather than a comment.

    This is the shape of limitation 13: the request is applied at a tick
    boundary, and a feed delivering nothing offers none. ``start()`` drains once
    beforehand, so the exposure is only a request made *after* the feed went
    quiet — which is already an incident on its own.
    """
    adapter = RecordedFeedAdapter([])
    hub = SharedFeedHub(adapter)
    channel = build_channel("st01", [UNDERLYING], in_process=True, tick_channel=True)
    hub.register(channel)
    hub.start()

    hub.request_subscription("st01", OPTION)  # after the (empty) tape finished

    assert OPTION not in adapter.subscribed
    assert hub.subscriptions_applied == 0


def test_dropping_a_subscription_stops_routing_without_unsubscribing_the_group(tape):
    adapter = RecordedFeedAdapter(tape)
    hub = SharedFeedHub(adapter)
    channel = build_channel("st01", [UNDERLYING], in_process=True, tick_channel=True)
    hub.register(channel)
    hub.request_subscription("st01", OPTION)
    hub.start()

    hub.drop_subscription("st01", OPTION)

    assert OPTION not in channel.dynamic_ids
    # The group keeps the subscription: another worker may still hold it.
    assert OPTION in adapter.subscribed


# --------------------------------------------------------------- sizing
def test_a_minute_at_the_measured_live_rate_drops_nothing():
    """Sized from tick arrival, not candle arrival.

    Two instruments (the underlying plus one option leg, which is what an engine
    worker holds) for four minutes at the measured rate, against the real default
    depth. If this ever drops, the constant is wrong — not the test.
    """
    seconds = 240
    per_instrument = MEASURED_TICKS_PER_SECOND * seconds
    stream = _ticks(UNDERLYING, per_instrument) + _ticks(OPTION, per_instrument)
    assert len(stream) == 2 * per_instrument

    hub = SharedFeedHub(RecordedFeedAdapter(stream))
    channel = build_channel(
        "st01",
        [UNDERLYING, OPTION],
        in_process=True,
        tick_channel=True,
        tick_max_depth=DEFAULT_TICK_MAX_DEPTH,
    )
    hub.register(channel)
    hub.start()
    hub.stop()

    assert channel.tick_queue is not None
    assert channel.tick_queue.dropped == 0
    assert channel.tick_queue.published == len(stream)


def test_the_default_tick_depth_buffers_more_than_a_minute_of_peak_arrival():
    """The constant's stated justification, pinned as arithmetic.

    ~10 ticks/s/instrument peak across two instruments is the sizing assumption
    in :data:`common.feed.queues.DEFAULT_TICK_MAX_DEPTH`; a change that quietly
    drops the buffer below a minute should fail here.
    """
    peak_ticks_per_second = 10 * 2
    assert DEFAULT_TICK_MAX_DEPTH / peak_ticks_per_second >= 60


def test_an_undersized_tick_queue_drops_the_oldest_and_counts_it():
    stream = _ticks(UNDERLYING, 50)
    hub = SharedFeedHub(RecordedFeedAdapter(stream))
    channel = build_channel(
        "st01", [UNDERLYING], in_process=True, tick_channel=True, tick_max_depth=10
    )
    hub.register(channel)
    hub.start()
    hub.stop()

    assert channel.tick_queue is not None
    assert channel.tick_queue.dropped > 0
    kept = _drain(channel.tick_queue)
    assert len(kept) <= 10
    # Since Part 2b-ii-B-1 the channel carries two item types, so the drop-oldest
    # property is asserted over the ticks rather than over everything on the queue
    # — the same narrowing the candle channel has always needed for its ``None``
    # sentinel. The property itself is unchanged: the freshest tick survives.
    ticks = [item for item in kept if isinstance(item, Tick)]
    assert ticks[-1].exchange_time == stream[-1].exchange_time
    # And the drop is now reported *to the worker*, not only to the supervisor's log.
    assert any(isinstance(item, TickDropNotice) for item in kept)


def test_publishing_ticks_never_blocks_the_feed_callback():
    """A full queue must not stall the hub — every other worker depends on it."""
    stream = _ticks(UNDERLYING, 500)
    hub = SharedFeedHub(RecordedFeedAdapter(stream))
    channel = build_channel(
        "st01", [UNDERLYING], in_process=True, tick_channel=True, tick_max_depth=5
    )
    hub.register(channel)

    finished = threading.Event()

    def _run() -> None:
        hub.start()
        finished.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert finished.wait(timeout=5.0), "the feed callback blocked on a full tick queue"
    thread.join(timeout=1.0)


# ---------------------------------------------------------------- health
def test_queue_stats_report_the_candle_and_tick_queues_separately(tape):
    hub = SharedFeedHub(RecordedFeedAdapter(tape))
    hub.register(build_channel("st01", [UNDERLYING], in_process=True, tick_channel=True))
    hub.start()
    hub.stop()

    names = [s.name for s in hub.queue_stats()]
    assert names == ["st01", "st01:ticks"]


def test_a_plain_worker_still_reports_exactly_one_queue(tape):
    hub = SharedFeedHub(RecordedFeedAdapter(tape))
    hub.register(build_channel("st01", [UNDERLYING], in_process=True))
    hub.start()
    hub.stop()

    assert [s.name for s in hub.queue_stats()] == ["st01"]
