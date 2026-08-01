"""``HubTickFeed``: the worker-side end of the tick channel.

Phase 3 Part 2b-ii-A. The property that carries the most weight here is the
**sentinel**: the supervisor publishes ``None`` per worker at shutdown, and this
loop is the thread that invokes ``engine.on_tick``. Mapping the sentinel to
``request_square_off`` *here* is what makes a ``SIGTERM`` delivered only to the
supervisor reach each worker's engine in-band, on the thread that owns it — the
D18 boundary, one layer out.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from common.engine.hub_feed import HubTickFeed
from common.feed import BoundedWorkerQueue
from common.models import Tick

UNDERLYING = "99926000"
OPTION = "45678"


def _tick(security_id: str, index: int) -> Tick:
    at = datetime(2026, 7, 29, 9, 15, tzinfo=UTC) + timedelta(seconds=index)
    return Tick(
        security_id=security_id,
        instrument="NIFTY",
        last_price=100.0 + index,
        exchange_time=at,
        received_at=at,
    )


def _queue(items: list) -> BoundedWorkerQueue:
    q = BoundedWorkerQueue.in_process("st01:ticks", max_depth=64)
    for item in items:
        q.publish(item)
    return q


# ---------------------------------------------------------------- delivery
def test_queued_ticks_reach_the_handler_in_order():
    ticks = [_tick(UNDERLYING, i) for i in range(5)]
    feed = HubTickFeed(_queue(ticks), idle_timeout_seconds=0.5, poll_seconds=0.05)

    seen: list[Tick] = []
    feed.on_tick(seen.append)
    feed.run()

    assert [t.exchange_time for t in seen] == [t.exchange_time for t in ticks]
    assert feed.ticks_received == 5


def test_an_exhausted_stream_ends_the_run_by_idle_timeout():
    feed = HubTickFeed(_queue([]), idle_timeout_seconds=0.2, poll_seconds=0.05)
    feed.on_tick(lambda _t: None)
    feed.run()

    assert feed.stopped_by_idle_timeout is True
    assert feed.stopped_by_sentinel is False


# ---------------------------------------------------------------- sentinel
def test_the_shutdown_sentinel_asks_the_engine_to_square_off():
    """Runbook item 3: the supervisor's ``None`` must reach the engine."""
    ticks = [_tick(UNDERLYING, 0), None]
    reasons: list[str] = []
    feed = HubTickFeed(
        _queue(ticks),
        on_square_off=reasons.append,
        idle_timeout_seconds=5.0,
        poll_seconds=0.05,
    )
    feed.on_tick(lambda _t: None)
    feed.run()

    assert reasons == ["supervisor sentinel"]
    assert feed.stopped_by_sentinel is True
    assert feed.stopped_by_idle_timeout is False


def test_the_sentinel_ends_the_run_even_with_no_square_off_callback():
    feed = HubTickFeed(_queue([None]), idle_timeout_seconds=5.0, poll_seconds=0.05)
    feed.on_tick(lambda _t: None)
    feed.run()

    assert feed.stopped_by_sentinel is True


def test_ticks_queued_after_the_sentinel_are_not_delivered():
    """Shutdown means shutdown: nothing is traded on after the sentinel."""
    items = [_tick(UNDERLYING, 0), None, _tick(UNDERLYING, 1)]
    feed = HubTickFeed(_queue(items), idle_timeout_seconds=5.0, poll_seconds=0.05)

    seen: list[Tick] = []
    feed.on_tick(seen.append)
    feed.run()

    assert len(seen) == 1


# ------------------------------------------------------------------- stop
def test_stop_from_inside_a_tick_callback_ends_the_run():
    """The Part 1 ownership rule: the thread that runs it is the one that ends it.

    This is exactly how the engine stops itself — ``_handle_square_off`` calls
    ``feed.stop()`` from within ``on_tick``.
    """
    ticks = [_tick(UNDERLYING, i) for i in range(10)]
    feed = HubTickFeed(_queue(ticks), idle_timeout_seconds=5.0, poll_seconds=0.05)

    seen: list[Tick] = []

    def _handler(tick: Tick) -> None:
        seen.append(tick)
        if len(seen) == 3:
            feed.stop()

    feed.on_tick(_handler)
    feed.run()

    assert len(seen) == 3


def test_the_run_does_not_hang_when_the_queue_stays_empty():
    feed = HubTickFeed(_queue([]), idle_timeout_seconds=0.2, poll_seconds=0.05)
    feed.on_tick(lambda _t: None)

    done = threading.Event()
    thread = threading.Thread(target=lambda: (feed.run(), done.set()), daemon=True)
    thread.start()

    assert done.wait(timeout=5.0), "HubTickFeed.run() did not return on an idle queue"
    thread.join(timeout=1.0)


# ---------------------------------------------------------- subscriptions
def test_subscribing_forwards_the_instrument_upstream_once():
    requested: list[str] = []
    feed = HubTickFeed(
        _queue([]),
        request_subscription=requested.append,
        idle_timeout_seconds=0.1,
        poll_seconds=0.05,
    )

    feed.subscribe(OPTION)
    feed.subscribe(OPTION)  # idempotent, per the MarketDataFeed contract

    assert requested == [OPTION]
    assert feed.subscriptions == frozenset({OPTION})


def test_unsubscribing_does_not_travel_upstream():
    """The subscription belongs to the group; dropping it could starve a peer."""
    requested: list[str] = []
    feed = HubTickFeed(
        _queue([]),
        request_subscription=requested.append,
        idle_timeout_seconds=0.1,
        poll_seconds=0.05,
    )
    feed.subscribe(OPTION)
    feed.unsubscribe(OPTION)

    assert requested == [OPTION]
    assert feed.subscriptions == frozenset()


def test_subscribing_with_no_channel_wired_does_not_raise():
    """Degrades loudly in the log rather than killing the worker."""
    feed = HubTickFeed(_queue([]), idle_timeout_seconds=0.1, poll_seconds=0.05)
    feed.subscribe(OPTION)

    assert feed.subscriptions == frozenset({OPTION})


def test_a_live_run_can_be_configured_never_to_time_out():
    """``idle_timeout_seconds=None`` is what a real session uses."""
    q = BoundedWorkerQueue.in_process("st01:ticks", max_depth=8)
    feed = HubTickFeed(q, idle_timeout_seconds=None, poll_seconds=0.05)
    feed.on_tick(lambda _t: None)

    done = threading.Event()
    thread = threading.Thread(target=lambda: (feed.run(), done.set()), daemon=True)
    thread.start()

    assert not done.wait(timeout=0.4), "an untimed feed ended itself on an empty queue"
    q.publish(None)  # only the sentinel ends it
    assert done.wait(timeout=5.0)
    thread.join(timeout=1.0)
    assert feed.stopped_by_sentinel is True


def test_a_tick_arriving_mid_wait_is_delivered_without_restarting_the_run():
    """The queue is polled, not blocked on, so the loop stays responsive."""
    q = BoundedWorkerQueue.in_process("st01:ticks", max_depth=8)
    feed = HubTickFeed(q, idle_timeout_seconds=None, poll_seconds=0.05)

    seen: list[Tick] = []
    feed.on_tick(seen.append)

    thread = threading.Thread(target=feed.run, daemon=True)
    thread.start()
    q.publish(_tick(UNDERLYING, 0))
    q.publish(None)
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert len(seen) == 1
    assert feed.stopped_by_sentinel is True


# ------------------------------------------------- shutdown while the feed is silent
def test_a_silent_feed_still_honours_a_square_off_request():
    """Phase 3 Part 2b-ii-B-2, and the reason ``should_stop`` exists.

    A live session runs with ``idle_timeout_seconds=None``, so before this check a
    ``SIGTERM`` arriving during a quiet stretch set the engine's flag and then waited
    for a tick to carry it into ``on_tick`` — a wait with no upper bound on a
    connected socket that has gone silent. This is the engine-level half of runbook
    limitation 13, and it is fixed rather than alarmed about: the loop already wakes
    every ``poll_seconds``, so it can simply ask.

    Returning from ``run()`` is the sanctioned boundary, not a shortcut — it hands
    control to ``TradingEngine.run()``'s ``finally``, which is the second of the two
    square-off boundaries deviation D18 names.
    """
    q = BoundedWorkerQueue.in_process("st01:ticks", max_depth=8)
    requested = threading.Event()
    feed = HubTickFeed(
        q,
        should_stop=requested.is_set,
        idle_timeout_seconds=None,
        poll_seconds=0.05,
    )
    feed.on_tick(lambda _t: None)

    done = threading.Event()
    thread = threading.Thread(target=lambda: (feed.run(), done.set()), daemon=True)
    thread.start()

    # Nothing on the queue and no timeout: without the check this never ends.
    assert not done.wait(timeout=0.3), "the feed ended before anything asked it to"

    requested.set()
    assert done.wait(timeout=5.0), "a square-off request did not end a silent feed"
    thread.join(timeout=1.0)
    assert feed.stopped_by_request is True
    assert feed.stopped_by_sentinel is False
    assert feed.stopped_by_idle_timeout is False


def test_the_request_check_does_not_discard_ticks_already_queued():
    """It is asked before the blocking get, so it must not pre-empt real work.

    Ticks already delivered stay delivered; the check only decides whether to wait
    for more.
    """
    ticks = [_tick(UNDERLYING, i) for i in range(3)]
    q = _queue(ticks)
    feed = HubTickFeed(q, should_stop=lambda: False, idle_timeout_seconds=0.3, poll_seconds=0.05)

    seen: list[Tick] = []
    feed.on_tick(seen.append)
    feed.run()

    assert [t.security_id for t in seen] == [UNDERLYING] * 3
    assert feed.stopped_by_request is False


def test_a_feed_with_no_should_stop_behaves_exactly_as_before():
    """The parameter is optional, and every existing caller passes nothing."""
    ticks = [_tick(UNDERLYING, i) for i in range(2)]
    feed = HubTickFeed(_queue(ticks), idle_timeout_seconds=0.2, poll_seconds=0.05)

    seen: list[Tick] = []
    feed.on_tick(seen.append)
    feed.run()

    assert len(seen) == 2
    assert feed.stopped_by_idle_timeout is True
    assert feed.stopped_by_request is False
