"""Dynamic subscriptions are applied without waiting for an unrelated tick.

Corrects the earlier (rejected) design of merely alarming on a stuck
subscription after 30 seconds. The real fix: :class:`~common.market_data.
dhan.DhanMarketFeedAdapter.start` now takes an ``on_idle`` callback, called on
the feed-owning thread on a bounded poll cadence regardless of whether any
tick arrives, and :class:`~common.feed.hub.SharedFeedHub` wires its own
``_apply_pending_subscriptions`` to it. These tests exercise that wiring
directly, using a hand-built adapter double that models a genuinely silent
market — no ticks at all — the case the earlier design could not close.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from common.feed.hub import SharedFeedHub, build_channel
from common.models import Tick

IST = ZoneInfo("Asia/Kolkata")

#: Long enough that a genuine failure reports instead of hanging the suite.
JOIN_TIMEOUT = 5.0


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 17, hour, minute, second, tzinfo=IST)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id,
        instrument=security_id,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


class _SilentPollingAdapter:
    """Models a live socket delivering **zero** ticks, that nonetheless wakes
    the owning thread on a bounded poll — exactly what
    :class:`~common.market_data.dhan.DhanMarketFeedAdapter`'s
    ``_receive_with_timeout`` now does, without needing the real SDK/asyncio
    machinery in a test double.
    """

    def __init__(self, *, poll_seconds: float = 0.02) -> None:
        self._poll_seconds = poll_seconds
        self._running = False
        self._stop_event = threading.Event()
        self.subscribed_ids: set[str] = set()
        self.subscribe_calls: list[tuple[tuple[str, ...], int | None, int | None]] = []
        self.idle_calls = 0
        self.owner_thread: int | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    def subscribe(
        self, security_ids: Sequence[str], *, segment: int | None = None, mode: int | None = None
    ) -> None:
        ids = tuple(str(s) for s in security_ids)
        self.subscribe_calls.append((ids, segment, mode))
        self.subscribed_ids.update(ids)

    def start(
        self,
        on_tick: Callable[[Tick], None],
        *,
        on_idle: Callable[[], None] | None = None,
    ) -> None:
        self.owner_thread = threading.get_ident()
        self._running = True
        try:
            while not self._stop_event.is_set():
                if self._stop_event.wait(self._poll_seconds):
                    break
                if on_idle is not None:
                    self.idle_calls += 1
                    on_idle()
        finally:
            self._running = False

    def request_stop(self) -> None:
        self._stop_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False


def _build_hub(
    adapter: _SilentPollingAdapter, *, strategy_id: str = "straddle_920"
) -> SharedFeedHub:
    hub = SharedFeedHub(adapter, interval_seconds=300)
    hub.register(build_channel(strategy_id, ["13"], segment=0, mode=15, tick_channel=False))
    return hub


# --------------------------------------------- 1. applied with zero ticks
def test_a_dynamic_subscription_is_applied_with_zero_incoming_ticks() -> None:
    """The core proof: no tick ever arrives, yet the request is applied,
    bounded by the adapter's own idle-poll cadence — not by an alarm."""
    adapter = _SilentPollingAdapter(poll_seconds=0.02)
    hub = _build_hub(adapter)

    runner = threading.Thread(target=hub.start, name="feed-run", daemon=True)
    runner.start()
    try:
        # Give the loop a moment to start idle-polling before asking for
        # anything, so the request genuinely lands mid-silence.
        time.sleep(0.05)
        hub.request_subscription("straddle_920", "49081", segment=2, mode=21)

        deadline = time.monotonic() + JOIN_TIMEOUT
        while time.monotonic() < deadline:
            if hub.subscriptions_applied >= 1:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("the subscription was never applied without a tick")
    finally:
        hub.request_stop()
        runner.join(timeout=JOIN_TIMEOUT)

    assert not runner.is_alive()
    assert adapter.idle_calls > 0, "on_idle must have fired at least once"
    assert "49081" in adapter.subscribed_ids
    assert adapter.subscribe_calls[-1] == (("49081",), 2, 21)
    # Applied by the feed-owning thread, never by the requesting one.
    assert adapter.owner_thread == runner.ident
    assert threading.current_thread().ident != adapter.owner_thread


# --------------------------------------------------- 2. concurrent requests
def test_concurrent_requests_from_multiple_threads_all_land() -> None:
    adapter = _SilentPollingAdapter(poll_seconds=0.02)
    hub = _build_hub(adapter)

    runner = threading.Thread(target=hub.start, name="feed-run", daemon=True)
    runner.start()
    try:
        time.sleep(0.05)
        ids = [f"sec_{i}" for i in range(20)]

        def _ask(security_id: str) -> None:
            hub.request_subscription("straddle_920", security_id, segment=2, mode=21)

        requesters = [threading.Thread(target=_ask, args=(sid,)) for sid in ids]
        for t in requesters:
            t.start()
        for t in requesters:
            t.join(timeout=JOIN_TIMEOUT)

        deadline = time.monotonic() + JOIN_TIMEOUT
        while time.monotonic() < deadline:
            if adapter.subscribed_ids >= set(ids):
                break
            time.sleep(0.01)
        else:
            raise AssertionError(f"not all requests landed: {adapter.subscribed_ids}")
    finally:
        hub.request_stop()
        runner.join(timeout=JOIN_TIMEOUT)

    assert adapter.subscribed_ids >= set(ids)
    assert hub.subscriptions_applied == len(ids)
    # No duplicate work: exactly one subscribe() call named each id.
    all_requested = [
        sid for call in adapter.subscribe_calls for sid in call[0] if sid != "13"
    ]
    assert sorted(all_requested) == sorted(ids)


# ----------------------------------------- 3. shutdown with a pending request
def test_shutdown_with_a_request_not_yet_applied_does_not_hang_or_crash() -> None:
    # A very slow poll, so the request is almost certainly still queued when
    # request_stop() lands.
    adapter = _SilentPollingAdapter(poll_seconds=5.0)
    hub = _build_hub(adapter)

    runner = threading.Thread(target=hub.start, name="feed-run", daemon=True)
    runner.start()
    time.sleep(0.05)

    hub.request_subscription("straddle_920", "never_applied", segment=2, mode=21)
    hub.request_stop()
    runner.join(timeout=JOIN_TIMEOUT)

    assert not runner.is_alive(), "shutdown with a pending request must not hang"


# ------------------------------------------- 4. incorrect-segment rejection
def test_a_rejected_request_is_observable_and_does_not_block_the_rest() -> None:
    """A genuine adapter-side rejection (the shape
    ``DhanMarketFeedAdapter.subscribe`` raises ``DhanFeedError`` in for a real
    conflicting-segment reassignment) must surface as an observable,
    per-request failure — counted and logged — and must not crash the feed
    thread or block an unrelated pending request queued alongside it."""
    adapter = _SilentPollingAdapter(poll_seconds=0.02)
    hub = _build_hub(adapter)

    real_subscribe = adapter.subscribe

    def _rejecting_subscribe(security_ids, *, segment=None, mode=None):  # type: ignore[no-untyped-def]
        if "conflict_id" in security_ids:
            raise RuntimeError("Cannot resubscribe ['conflict_id']: conflicting segment")
        return real_subscribe(security_ids, segment=segment, mode=mode)

    adapter.subscribe = _rejecting_subscribe  # type: ignore[method-assign]

    runner = threading.Thread(target=hub.start, name="feed-run", daemon=True)
    runner.start()
    try:
        time.sleep(0.05)
        hub.request_subscription("straddle_920", "conflict_id", segment=0, mode=15)
        hub.request_subscription("straddle_920", "unrelated_id", segment=2, mode=21)

        deadline = time.monotonic() + JOIN_TIMEOUT
        while time.monotonic() < deadline:
            if hub.subscriptions_rejected >= 1 and "unrelated_id" in adapter.subscribed_ids:
                break
            time.sleep(0.01)
        else:
            raise AssertionError(
                f"rejected={hub.subscriptions_rejected} subscribed={adapter.subscribed_ids}"
            )
    finally:
        hub.request_stop()
        runner.join(timeout=JOIN_TIMEOUT)

    assert not runner.is_alive(), "a rejected request must not crash or hang the feed thread"
    assert hub.subscriptions_rejected == 1
    assert "conflict_id" not in adapter.subscribed_ids
    assert "unrelated_id" in adapter.subscribed_ids
