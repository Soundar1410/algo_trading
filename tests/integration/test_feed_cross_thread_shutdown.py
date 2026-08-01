"""Cross-thread shutdown of the reconnecting feed, against a *blocking* adapter.

Why this file exists separately from ``test_feed_reconnect.py``
--------------------------------------------------------------
Every test in that file drives :class:`~common.feed.reconnect.ReconnectingFeed`
with ``_ScriptedAdapter``, whose ``start()`` delivers its batch and returns
immediately, on the caller's own thread. No test there has ever called ``stop()``
from a *different* thread than the one running ``start()`` — which is exactly why
the suite never caught the cross-thread hang that a real Dhan connection
reproduced during Phase 2 Block 2 (runbook limitation 1).

The double here is built to be realistic in the one dimension that matters:
``start()`` blocks, on one thread, for as long as the feed is live. That is the
shape of ``dhanhq``'s ``MarketFeed`` — a private ``asyncio`` loop driven
synchronously by whichever thread called in, sitting in ``await ws.recv()``
between frames — and it is the shape under which ``close_connection()`` from a
foreign thread takes its ``run_coroutine_threadsafe(...).result()`` branch and
waits, unbounded, on a loop that only the blocked owner thread can advance.

The ownership rule under test
-----------------------------
The thread that called ``start()`` owns the adapter's loop and is the only thread
permitted to close the connection. Every other thread may only signal intent.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest

from common.feed import ReconnectingFeed, ReconnectPolicy
from common.market_data.adapter import TickCallback
from common.models import Tick

SECURITY_ID = "49081"
SESSION_START = datetime(2026, 7, 30, 9, 15, 0, tzinfo=UTC) - timedelta(hours=5, minutes=30)

#: Interval between synthesised frames — a live feed during market hours.
FRAME_INTERVAL = 0.005
#: How long the double's cross-thread close waits before giving up. The real
#: ``future.result()`` has no timeout at all; this one is bounded so a *failing*
#: run reports a failure instead of hanging the whole suite.
DEADLOCK_TIMEOUT = 2.0
#: Generous ceiling for any wait that should resolve in milliseconds.
PATIENCE = 5.0


def _wait_for(predicate, timeout: float = PATIENCE) -> bool:
    """Poll until ``predicate`` holds. Returns False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class _BlockingFeedAdapter:
    """A feed double that blocks on one thread the way a live socket does.

    Models ``dhanhq``'s threading contract rather than its wire format:

    * ``start()`` records the calling thread as the loop's owner and does not
      return until the loop is told to finish.
    * ``stop()`` called from that owner thread closes immediately — the safe,
      same-thread branch.
    * ``stop()`` called from *any other* thread while the loop is live records the
      violation and waits on something the blocked loop can never produce. That is
      the observed hang, reproduced deterministically and without a socket.
    * ``request_stop()`` is the thread-safe half: it sets a flag and returns. The
      owner notices at the next frame boundary and closes on its own thread.
    """

    def __init__(self, *, silent: bool = False) -> None:
        #: ``silent=True`` models a connected socket that delivers nothing —
        #: out of hours, or an instrument with no trades. Nothing can unblock a
        #: real ``recv()`` in that state from outside the owning thread.
        self._silent = silent
        self._frames: queue.Queue[Tick | None] = queue.Queue(maxsize=1024)
        self._security_ids: set[str] = set()
        self._running = False
        self._producer: threading.Thread | None = None
        self._producing = threading.Event()

        self.owner_thread: int | None = None
        self.closed_by: int | None = None
        self.start_calls = 0
        self.stop_calls = 0
        self.request_stop_calls = 0
        #: Every ``stop()`` that reached this adapter from a non-owner thread
        #: while the loop was live. The number the fix must drive to zero.
        self.cross_thread_stop_calls = 0
        self.stop_deadlocked = False
        self.delivered = 0
        #: Set once the loop is up and blocking, i.e. genuinely mid-``recv``.
        self.looping = threading.Event()
        self.loop_exited = threading.Event()
        self.last_disconnect_code: int | None = None

    # ----------------------------------------------------------- the contract
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def subscribed(self) -> frozenset[str]:
        return frozenset(self._security_ids)

    def subscribe(self, security_ids: Sequence[str], *, segment: int | None = None) -> None:
        self._security_ids.update(str(s) for s in security_ids)

    def start(self, on_tick: TickCallback) -> None:
        self.start_calls += 1
        self.owner_thread = threading.get_ident()
        self._running = True
        self.loop_exited.clear()
        self._start_producing()
        try:
            self.looping.set()
            while True:
                # Blocks with no timeout, exactly as ``await ws.recv()`` does. A
                # frame is therefore the *only* thing that gives this loop a
                # chance to notice anything — including a stop request. That is
                # the property the timeout-polling version of this double failed
                # to model, and it is the whole reason a silent feed is a
                # genuinely hard case rather than a slow one.
                tick = self._frames.get()
                if tick is None or not self._running:
                    break
                self.delivered += 1
                on_tick(tick)
        finally:
            self._running = False
            self._stop_producing()
            self.looping.clear()
            self.loop_exited.set()

    def stop(self) -> None:
        caller = threading.get_ident()
        self.stop_calls += 1
        if self._running and caller != self.owner_thread:
            # The SDK's cross-thread branch: schedule the close onto the loop and
            # wait for it. The owner is blocked in recv() and cannot advance the
            # loop, so nothing ever completes it.
            self.cross_thread_stop_calls += 1
            if not self.loop_exited.wait(timeout=DEADLOCK_TIMEOUT):
                self.stop_deadlocked = True
            return
        self._running = False
        self.closed_by = caller

    def request_stop(self) -> None:
        """Signal intent. Safe from any thread; never touches the connection."""
        self.request_stop_calls += 1
        self._running = False

    # ------------------------------------------------------ test-harness only
    def abandon(self) -> None:
        """Deliver one last frame so a blocked loop can reach a boundary.

        Test-harness only, not part of the adapter contract. Stands in for the
        one thing that genuinely does release a wedged live feed: the process
        exiting, or the exchange finally sending something.
        """
        self._running = False
        self._frames.put(None)

    # ---------------------------------------------------------------- frames
    def _start_producing(self) -> None:
        if self._silent:
            return
        self._producing.set()
        self._producer = threading.Thread(target=self._produce, name="frames", daemon=True)
        self._producer.start()

    def _stop_producing(self) -> None:
        self._producing.clear()
        producer, self._producer = self._producer, None
        if producer is not None and producer.ident != threading.get_ident():
            producer.join(timeout=PATIENCE)

    def _produce(self) -> None:
        sequence = 0
        while self._producing.is_set():
            # A dropped frame is a slow consumer, not a broken test: the exchange
            # would not have waited either.
            with contextlib.suppress(queue.Full):
                self._frames.put_nowait(_tick(sequence))
            sequence += 1
            time.sleep(FRAME_INTERVAL)


def _tick(offset_seconds: int, price: float = 100.0) -> Tick:
    moment = SESSION_START + timedelta(seconds=offset_seconds)
    return Tick(
        security_id=SECURITY_ID,
        instrument="NIFTY",
        last_price=price,
        exchange_time=moment,
        received_at=moment,
        last_quantity=1,
    )


@pytest.fixture
def adapters() -> Iterator[list[_BlockingFeedAdapter]]:
    """Hands out doubles and guarantees none is left looping after a failure."""
    made: list[_BlockingFeedAdapter] = []
    yield made
    for adapter in made:
        adapter.abandon()


@pytest.fixture
def blocking_feed(adapters):
    """Build a ``ReconnectingFeed`` over a blocking double, subscribed and ready."""

    def _build(*, silent: bool = False) -> tuple[_BlockingFeedAdapter, ReconnectingFeed]:
        adapter = _BlockingFeedAdapter(silent=silent)
        adapters.append(adapter)
        feed = ReconnectingFeed(
            adapter,  # type: ignore[arg-type]
            policy=ReconnectPolicy(max_attempts=2, initial_backoff=0.001, max_backoff=0.002),
            sleep=lambda _s: None,
            rng=lambda: 0.0,
        )
        feed.subscribe([SECURITY_ID])
        return adapter, feed

    return _build


def _run_on_thread(feed: ReconnectingFeed, on_tick: TickCallback) -> threading.Thread:
    thread = threading.Thread(target=lambda: feed.start(on_tick), name="feed", daemon=True)
    thread.start()
    return thread


# ------------------------------------------------------- the regression itself
def test_stopping_from_another_thread_returns_and_closes_on_the_feed_thread(blocking_feed):
    """The deployment shape: ``start()`` on a worker thread, ``stop()`` from main.

    This is the test the original suite lacked. Against the pre-fix code
    ``ReconnectingFeed.stop()`` delegates straight to ``adapter.stop()`` on the
    calling thread, which is the hang a real Dhan connection reproduced.
    """
    adapter, feed = blocking_feed()
    ticks: list[Tick] = []

    thread = _run_on_thread(feed, ticks.append)
    assert adapter.looping.wait(timeout=PATIENCE), "the feed loop never started"
    assert _wait_for(lambda: len(ticks) >= 3), "the feed loop never delivered a tick"

    feed.stop()  # from the main thread, while the feed thread is blocked in recv

    thread.join(timeout=PATIENCE)
    assert not thread.is_alive(), "the feed thread never returned after a cross-thread stop()"
    assert adapter.cross_thread_stop_calls == 0, (
        "stop() reached the adapter from a thread that does not own its loop"
    )
    assert not adapter.stop_deadlocked
    assert adapter.closed_by == adapter.owner_thread, (
        "the connection was closed by a thread other than the one that opened it"
    )
    assert not feed.is_running
    assert feed.stopped()


def test_a_cross_thread_stop_signals_intent_rather_than_closing(blocking_feed):
    """The mechanism, not just the outcome: intent crosses the thread, the close does not."""
    adapter, feed = blocking_feed()

    thread = _run_on_thread(feed, lambda _tick: None)
    assert adapter.looping.wait(timeout=PATIENCE)

    feed.stop()
    thread.join(timeout=PATIENCE)

    assert not thread.is_alive()
    assert adapter.request_stop_calls >= 1, "no thread-safe stop request was ever issued"
    assert adapter.cross_thread_stop_calls == 0


def test_the_feed_reports_whether_it_actually_came_back(blocking_feed):
    """``wait_until_stopped`` is what a supervisor joins on, so it must not lie."""
    adapter, feed = blocking_feed()

    thread = _run_on_thread(feed, lambda _tick: None)
    assert adapter.looping.wait(timeout=PATIENCE)

    feed.stop()

    assert feed.wait_until_stopped(timeout=PATIENCE) is True
    thread.join(timeout=PATIENCE)
    assert not thread.is_alive()


def test_a_silent_feed_cannot_be_closed_from_another_thread(blocking_feed):
    """The honest limitation, asserted as behaviour rather than left to a comment.

    A connected socket delivering nothing leaves its owner blocked in ``recv()``
    with no frame boundary at which to notice the stop request. Nothing outside
    that thread can close it safely, so the feed reports that it did **not** come
    back — and still refuses to reach across and close the connection itself.
    The supervisor treats that report as an unclean feed shutdown and carries on;
    it never escalates to the cross-thread close.
    """
    adapter, feed = blocking_feed(silent=True)

    thread = _run_on_thread(feed, lambda _tick: None)
    assert adapter.looping.wait(timeout=PATIENCE)

    feed.stop()

    assert feed.wait_until_stopped(timeout=0.2) is False, (
        "a feed that is still blocked must not report a completed shutdown"
    )
    assert thread.is_alive(), "the double is not modelling an indefinitely blocked recv"
    assert adapter.cross_thread_stop_calls == 0, "gave up safety to force a silent feed closed"
    assert adapter.closed_by is None, "nothing closed the connection, which is correct"
    assert feed.stopped(), "the stop request itself must still be recorded"

    # Give the loop the frame boundary a live feed would eventually supply. Even
    # then the close still happens on the owning thread — the ownership rule is
    # not relaxed by the wait, only deferred by it.
    adapter.abandon()
    thread.join(timeout=PATIENCE)
    assert not thread.is_alive()
    assert adapter.closed_by == adapter.owner_thread


def test_stopping_from_inside_the_callback_still_closes_on_the_feed_thread(blocking_feed):
    """The capture-script pattern, on a blocking adapter rather than a scripted one."""
    adapter, feed = blocking_feed()
    seen: list[Tick] = []

    def stop_after_three(tick: Tick) -> None:
        seen.append(tick)
        if len(seen) == 3:
            feed.stop()

    thread = _run_on_thread(feed, stop_after_three)
    thread.join(timeout=PATIENCE)

    assert not thread.is_alive()
    assert len(seen) == 3
    assert adapter.cross_thread_stop_calls == 0
    assert adapter.closed_by == adapter.owner_thread


def test_stopping_before_the_feed_starts_prevents_it_from_running(blocking_feed):
    """No loop is in flight, so the caller owns the adapter and may close it."""
    adapter, feed = blocking_feed()

    feed.stop()
    feed.start(lambda _tick: None)  # returns immediately; a stopped feed stays stopped

    assert adapter.start_calls == 0
    assert adapter.cross_thread_stop_calls == 0
    assert adapter.closed_by == threading.get_ident()
    assert feed.wait_until_stopped(timeout=0) is True


def test_a_second_stop_from_another_thread_is_harmless(blocking_feed):
    """Idempotent, and still never closes from the wrong thread."""
    adapter, feed = blocking_feed()

    thread = _run_on_thread(feed, lambda _tick: None)
    assert adapter.looping.wait(timeout=PATIENCE)

    feed.stop()
    thread.join(timeout=PATIENCE)
    feed.stop()  # after the loop has gone: nothing owns the adapter now

    assert not thread.is_alive()
    assert adapter.cross_thread_stop_calls == 0
    assert feed.stopped()
