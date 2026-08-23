"""A tick-only worker's candle queue must stay empty, not overflow.

The defect this pins: ``build_channel`` always creates a candle queue, and
``SharedFeedHub._fan_out`` published into it for any channel that ``wants()``
the instrument — whether or not the worker on the other end ever drains it. A
tick-driven worker (its child process is handed only ``tick_queue.raw``) never
does, so the queue filled at ``max_depth`` completed candles and every candle
after that became one drop-oldest event plus one ``worker queue overflow``
warning, for the rest of the session. Those drops were then summed into the
heartbeat's ``dropped_events``, so a queue that does not exist for its owner was
degrading reported health.

``WorkerChannel.receive_candles`` is the generic correction: a capability, not a
special case. Nothing here names a strategy — the property under test is that a
channel which declares it does not consume candles is never published to, while
every genuine overflow path stays exactly as loud as it was.
"""

from __future__ import annotations

import logging
import queue as queue_module
from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from common.feed.hub import SharedFeedHub, WorkerChannel, build_channel
from common.feed.queues import BoundedWorkerQueue, TickDropNotice
from common.models import Candle, Tick

IST = ZoneInfo("Asia/Kolkata")
UNDERLYING = "INDEX"
INTERVAL_SECONDS = 60

#: Comfortably past the positional runtime's own candle-queue depth (64), so a
#: channel that were still published to would be deep into drop-oldest.
CANDLE_COUNT = 80

OVERFLOW_WARNING = "worker queue overflow"
TICK_OVERFLOW_WARNING = "worker tick queue overflow"


def _tick(price: float, ts: datetime, *, security_id: str = UNDERLYING) -> Tick:
    return Tick(
        security_id=security_id,
        instrument=security_id,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


def _tape(candles: int, *, security_id: str = UNDERLYING) -> list[Tick]:
    """One tick per minute bucket, plus a trailing one to close the last bar.

    A bucket completes when the *next* bucket's first tick arrives, so N+1 ticks
    produce N completed candles during ``start()``; the open N+1th bar is flushed
    by ``stop()``, which routes through the same ``_fan_out``.
    """
    start = datetime(2026, 7, 16, 9, 20, tzinfo=IST)
    return [
        _tick(100.0 + i, start + timedelta(seconds=INTERVAL_SECONDS * i), security_id=security_id)
        for i in range(candles + 1)
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
    """A full session: start to tape exhaustion, then stop's partial-bar flush."""
    hub.start()
    hub.stop()


# ------------------------------------------------- the opted-out channel
def test_a_tick_only_channel_is_never_published_a_candle(caplog):
    """The regression itself: far past the queue depth, still zero drops."""
    channel = build_channel(
        "tickonly01",
        [UNDERLYING],
        in_process=True,
        max_depth=64,
        tick_channel=True,
        receive_candles=False,
    )
    hub = SharedFeedHub(_ScriptedAdapter(_tape(CANDLE_COUNT)), interval_seconds=INTERVAL_SECONDS)
    hub.register(channel)

    with caplog.at_level(logging.WARNING, logger="common.feed.hub"):
        _run(hub)

    # The hub still aggregated every bar — it simply routed none of them here.
    assert hub.candle_count == CANDLE_COUNT + 1
    assert channel.queue.published == 0
    assert channel.queue.dropped == 0
    assert _drain(channel.queue) == []
    assert OVERFLOW_WARNING not in caplog.text


def test_a_tick_only_channel_still_receives_every_tick():
    """Opting out of candles must not cost the channel its ticks."""
    tape = _tape(CANDLE_COUNT)
    channel = build_channel(
        "tickonly01",
        [UNDERLYING],
        in_process=True,
        max_depth=64,
        tick_channel=True,
        tick_max_depth=len(tape) + 8,
        receive_candles=False,
    )
    hub = SharedFeedHub(_ScriptedAdapter(tape), interval_seconds=INTERVAL_SECONDS)
    hub.register(channel)
    _run(hub)

    assert channel.tick_queue is not None
    delivered = _drain(channel.tick_queue)
    assert delivered == list(tape)
    assert channel.tick_queue.dropped == 0


def test_the_stop_flush_also_skips_a_tick_only_channel():
    """``stop()`` flushes partial bars through ``_fan_out`` too — same gate."""
    channel = build_channel(
        "tickonly01", [UNDERLYING], in_process=True, receive_candles=False, tick_channel=True
    )
    hub = SharedFeedHub(_ScriptedAdapter(_tape(2)), interval_seconds=INTERVAL_SECONDS)
    hub.register(channel)

    hub.start()
    published_before_flush = channel.queue.published
    hub.stop()

    assert published_before_flush == 0
    assert channel.queue.published == 0


def test_one_channel_opting_out_does_not_starve_another(caplog):
    """Two workers, one instrument: the gate is per channel, not per hub."""
    consumer = build_channel(
        "consumer01", [UNDERLYING], in_process=True, max_depth=CANDLE_COUNT + 4
    )
    tick_only = build_channel(
        "tickonly01",
        [UNDERLYING],
        in_process=True,
        max_depth=64,
        tick_channel=True,
        receive_candles=False,
    )
    hub = SharedFeedHub(_ScriptedAdapter(_tape(CANDLE_COUNT)), interval_seconds=INTERVAL_SECONDS)
    hub.register(consumer)
    hub.register(tick_only)

    with caplog.at_level(logging.WARNING, logger="common.feed.hub"):
        _run(hub)

    candles = _drain(consumer.queue)
    assert len(candles) == CANDLE_COUNT + 1
    assert all(isinstance(c, Candle) for c in candles)
    assert consumer.queue.dropped == 0
    assert tick_only.queue.published == 0
    assert OVERFLOW_WARNING not in caplog.text


# ------------------------------------------- default behaviour preserved
def test_a_candle_consuming_channel_is_unchanged_by_default():
    """``build_channel`` with no new argument behaves exactly as before."""
    channel = build_channel(
        "consumer01", [UNDERLYING], in_process=True, max_depth=CANDLE_COUNT + 4
    )
    assert channel.receive_candles is True

    hub = SharedFeedHub(_ScriptedAdapter(_tape(CANDLE_COUNT)), interval_seconds=INTERVAL_SECONDS)
    hub.register(channel)
    _run(hub)

    assert len(_drain(channel.queue)) == CANDLE_COUNT + 1
    assert channel.queue.dropped == 0


def test_a_hand_built_worker_channel_still_defaults_to_receiving_candles():
    """The many fixtures that construct ``WorkerChannel`` directly keep working."""
    channel = WorkerChannel(
        strategy_id="consumer01",
        security_ids=frozenset({UNDERLYING}),
        queue=BoundedWorkerQueue.in_process("consumer01", max_depth=8),
    )
    assert channel.receive_candles is True


# --------------------------------------------- genuine overflow is intact
def test_a_genuinely_overflowing_candle_channel_still_counts_and_warns(caplog):
    """The fail-safe this change must not weaken."""
    depth = 4
    channel = build_channel("consumer01", [UNDERLYING], in_process=True, max_depth=depth)
    hub = SharedFeedHub(_ScriptedAdapter(_tape(CANDLE_COUNT)), interval_seconds=INTERVAL_SECONDS)
    hub.register(channel)

    with caplog.at_level(logging.WARNING, logger="common.feed.hub"):
        _run(hub)

    assert channel.queue.dropped == (CANDLE_COUNT + 1) - depth
    assert OVERFLOW_WARNING in caplog.text
    assert channel.queue.stats().is_overflowing


def test_a_genuinely_overflowing_tick_channel_still_drops_and_notices(caplog):
    """A tick-only channel's *tick* queue keeps every drop-detection guarantee.

    Opting out of candles must not quietly opt out of the tick-loss signal that
    blocks entries in the child — the two are independent capabilities.
    """
    depth = 4
    tape = _tape(CANDLE_COUNT)
    channel = build_channel(
        "tickonly01",
        [UNDERLYING],
        in_process=True,
        tick_channel=True,
        tick_max_depth=depth,
        receive_candles=False,
    )
    hub = SharedFeedHub(_ScriptedAdapter(tape), interval_seconds=INTERVAL_SECONDS)
    hub.register(channel)

    with caplog.at_level(logging.WARNING, logger="common.feed.hub"):
        _run(hub)

    assert channel.tick_queue is not None
    assert channel.tick_queue.dropped > 0
    assert TICK_OVERFLOW_WARNING in caplog.text
    # The in-band notice the child turns into `engine.block_entries(...)`.
    assert any(isinstance(item, TickDropNotice) for item in _drain(channel.tick_queue))
    # ...and none of that noise came from the candle side.
    assert channel.queue.dropped == 0
    assert OVERFLOW_WARNING not in caplog.text


# ------------------------------------------------------------- health
def test_queue_stats_omits_a_candle_queue_the_worker_never_consumes():
    tick_only = build_channel(
        "tickonly01", [UNDERLYING], in_process=True, tick_channel=True, receive_candles=False
    )
    consumer = build_channel("consumer01", [UNDERLYING], in_process=True)
    hub = SharedFeedHub(_ScriptedAdapter(_tape(2)), interval_seconds=INTERVAL_SECONDS)
    hub.register(tick_only)
    hub.register(consumer)

    names = {stat.name for stat in hub.queue_stats()}
    assert names == {"tickonly01:ticks", "consumer01"}
    assert "tickonly01" not in names


def test_queue_stats_is_empty_rather_than_wrong_for_a_bare_opted_out_channel():
    """An opted-out channel with no tick queue contributes nothing at all.

    Degenerate, but the supervisors' ``max(..., default=0)``/``sum(...)`` must
    survive it — that is the only reason this shape is pinned.
    """
    channel = build_channel("tickonly01", [UNDERLYING], in_process=True, receive_candles=False)
    hub = SharedFeedHub(_ScriptedAdapter(_tape(2)), interval_seconds=INTERVAL_SECONDS)
    hub.register(channel)

    stats = hub.queue_stats()
    assert stats == ()
    assert max((s.depth for s in stats), default=0) == 0
    assert sum(s.dropped for s in stats) == 0
