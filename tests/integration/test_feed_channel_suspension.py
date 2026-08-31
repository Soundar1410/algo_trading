"""A suspended channel receives nothing — candles or ticks — until resumed.

The mechanism the 31 August 2026 incident's fix depends on: once a worker's
process has died, its queue must stop being fed *immediately*, not after the
supervisor gets around to ending the whole run. Before ``suspend_channel``/
``resume_channel`` existed, ``_fan_out``/``_fan_out_tick`` kept publishing
into a dead worker's queue regardless — drop-oldest, one warning per tick,
for as long as the run continued (measured against the real incident:
~90,000 warnings over six hours).

Mirrors ``tests/integration/test_feed_candle_channel_opt_out.py``'s shape —
that file pins the sibling mechanism (``WorkerChannel.receive_candles``,
fixed at registration); this one pins its general, toggleable form on both
channels.
"""

from __future__ import annotations

import logging
import queue as queue_module
from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from common.feed.hub import SharedFeedHub, build_channel
from common.feed.queues import BoundedWorkerQueue
from common.models import Candle, Tick

IST = ZoneInfo("Asia/Kolkata")
UNDERLYING = "INDEX"
INTERVAL_SECONDS = 60

#: Comfortably past a typical queue depth, so a channel that were still
#: published to would be deep into drop-oldest and warning on every item.
TICK_COUNT = 200

OVERFLOW_WARNING = "worker queue overflow"
TICK_OVERFLOW_WARNING = "worker tick queue overflow"


def _tick(price: float, ts: datetime, *, security_id: str = UNDERLYING) -> Tick:
    return Tick(
        security_id=security_id, instrument=security_id, last_price=price,
        exchange_time=ts, received_at=ts,
    )


def _tape(count: int, *, security_id: str = UNDERLYING) -> list[Tick]:
    start = datetime(2026, 7, 16, 9, 20, tzinfo=IST)
    return [
        _tick(100.0 + i, start + timedelta(seconds=INTERVAL_SECONDS * i), security_id=security_id)
        for i in range(count)
    ]


class _ScriptedAdapter:
    """A feed that publishes a fixed tape and returns. Holds no connection."""

    def __init__(self, ticks: Sequence[Tick]) -> None:
        self._ticks = list(ticks)
        self.subscribed: list[str] = []
        self.is_running = False

    def subscribe(self, security_ids: Sequence[str], *, segment: int | None = None) -> None:
        self.subscribed.extend(security_ids)

    def request_stop(self) -> None:
        self.is_running = False

    def stop(self) -> None:
        self.is_running = False

    def start(self, on_tick, *, on_idle=None) -> None:
        self.is_running = True
        try:
            for tick in self._ticks:
                on_tick(tick)
        finally:
            self.is_running = False


def _drain(q: BoundedWorkerQueue) -> list[object]:
    items: list[object] = []
    while True:
        try:
            items.append(q.get(timeout=0.01))
        except queue_module.Empty:
            return items


def _run(hub: SharedFeedHub) -> None:
    hub.start()
    hub.stop()


# --------------------------------------------------------------- suspension
def test_a_suspended_channel_is_never_published_to(caplog):
    """The regression itself: far past any queue depth, still zero drops,
    zero publishes, zero warnings — for both channels."""
    channel = build_channel(
        "dead01", [UNDERLYING], in_process=True, max_depth=16, tick_channel=True,
        tick_max_depth=16,
    )
    hub = SharedFeedHub(_ScriptedAdapter(_tape(TICK_COUNT)), interval_seconds=INTERVAL_SECONDS)
    hub.register(channel)
    hub.suspend_channel("dead01")

    with caplog.at_level(logging.WARNING, logger="common.feed.hub"):
        _run(hub)

    # The hub still aggregated every bar and counted every tick — it simply
    # routed none of them to this suspended channel.
    assert hub.candle_count >= 1
    assert hub.tick_count == TICK_COUNT
    assert channel.queue.published == 0
    assert channel.queue.dropped == 0
    assert channel.tick_queue is not None
    assert channel.tick_queue.published == 0
    assert channel.tick_queue.dropped == 0
    assert _drain(channel.queue) == []
    assert _drain(channel.tick_queue) == []
    assert OVERFLOW_WARNING not in caplog.text
    assert TICK_OVERFLOW_WARNING not in caplog.text


def test_suspending_mid_session_stops_further_delivery():
    """A channel that was receiving normally stops the instant it is
    suspended — proving the gate is live, not only effective when set
    before the first tick arrives."""
    tape = _tape(TICK_COUNT)
    channel = build_channel(
        "flaky01", [UNDERLYING], in_process=True, max_depth=TICK_COUNT, tick_channel=True,
        tick_max_depth=TICK_COUNT,
    )
    hub = SharedFeedHub(_ScriptedAdapter(tape), interval_seconds=INTERVAL_SECONDS)
    hub.register(channel)
    assert channel.tick_queue is not None

    # Half the tape delivered normally, then suspend, then the rest.
    half = TICK_COUNT // 2
    for tick in tape[:half]:
        hub.on_tick(tick)
    published_before = channel.tick_queue.published
    hub.suspend_channel("flaky01")
    for tick in tape[half:]:
        hub.on_tick(tick)

    assert channel.tick_queue.published == published_before
    assert published_before > 0


def test_resuming_restores_delivery():
    tape = _tape(TICK_COUNT)
    channel = build_channel(
        "recovered01", [UNDERLYING], in_process=True, max_depth=TICK_COUNT, tick_channel=True,
        tick_max_depth=TICK_COUNT,
    )
    hub = SharedFeedHub(_ScriptedAdapter(tape), interval_seconds=INTERVAL_SECONDS)
    hub.register(channel)
    hub.suspend_channel("recovered01")

    for tick in tape[: TICK_COUNT // 2]:
        hub.on_tick(tick)
    assert channel.tick_queue is not None
    assert channel.tick_queue.published == 0

    hub.resume_channel("recovered01")
    for tick in tape[TICK_COUNT // 2 :]:
        hub.on_tick(tick)

    assert channel.tick_queue.published == TICK_COUNT - TICK_COUNT // 2


def test_is_suspended_reflects_the_current_state():
    hub = SharedFeedHub(_ScriptedAdapter([]))
    assert hub.is_suspended("s1") is False
    hub.suspend_channel("s1")
    assert hub.is_suspended("s1") is True
    hub.resume_channel("s1")
    assert hub.is_suspended("s1") is False


def test_resuming_a_never_suspended_channel_is_a_harmless_no_op():
    hub = SharedFeedHub(_ScriptedAdapter([]))
    hub.resume_channel("never-suspended")
    assert hub.is_suspended("never-suspended") is False


def test_suspension_is_per_channel_not_per_hub(caplog):
    """Two workers, one instrument: suspending one must not starve the other
    — the same isolation ``receive_candles`` already guarantees for its own
    gate, proven here for its general form."""
    healthy = build_channel("healthy01", [UNDERLYING], in_process=True, max_depth=TICK_COUNT + 4)
    dead = build_channel(
        "dead01", [UNDERLYING], in_process=True, max_depth=16, tick_channel=True, tick_max_depth=16
    )
    hub = SharedFeedHub(_ScriptedAdapter(_tape(TICK_COUNT)), interval_seconds=INTERVAL_SECONDS)
    hub.register(healthy)
    hub.register(dead)
    hub.suspend_channel("dead01")

    with caplog.at_level(logging.WARNING, logger="common.feed.hub"):
        _run(hub)

    candles = _drain(healthy.queue)
    assert len(candles) >= 1
    assert all(isinstance(c, Candle) for c in candles)
    assert healthy.queue.dropped == 0
    assert dead.queue.published == 0
    assert OVERFLOW_WARNING not in caplog.text
    assert TICK_OVERFLOW_WARNING not in caplog.text


# --------------------------------------------- genuine overflow is intact
def test_an_unsuspended_channel_still_overflows_and_warns_normally(caplog):
    """The fail-safe this change must not weaken: an ordinary, never-
    suspended channel that genuinely falls behind still drops, counts and
    warns exactly as before."""
    depth = 4
    channel = build_channel("consumer01", [UNDERLYING], in_process=True, max_depth=depth)
    hub = SharedFeedHub(_ScriptedAdapter(_tape(80)), interval_seconds=INTERVAL_SECONDS)
    hub.register(channel)

    with caplog.at_level(logging.WARNING, logger="common.feed.hub"):
        _run(hub)

    assert channel.queue.dropped > 0
    assert OVERFLOW_WARNING in caplog.text
