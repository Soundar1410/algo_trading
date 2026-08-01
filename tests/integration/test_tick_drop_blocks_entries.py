"""A dropped tick stops the engine taking new entries for the day.

Phase 3 Part 2b-ii-B-1, closing the open half of runbook limitation 14.

The problem the limitation names: D9's guarantee is that every worker sees
byte-identical bars, because there is exactly one aggregator per instrument. A
worker driving the ported engine off *raw* ticks (D23) builds its own bars, so
under queue overflow its OHLC quietly differs from the hub's for that interval.
That is worse in kind than a candle-channel drop, which loses a whole bar and is
obvious.

Since 2b-ii-A the drop has been counted and logged — but only in the
**supervisor's** process, on the parent's ``BoundedWorkerQueue``. The engine runs
in the child and cannot read that counter, so ``hub.py`` was claiming an entry
block that no code performed. The only channel from parent to child is the tick
queue itself, which is exactly how the shutdown sentinel already travels, so the
notice travels in band beside it.

Everything below is real: a real ``SharedFeedHub``, a real bounded queue
deliberately undersized, a real ``HubTickFeed``, a real ``TradingEngine``, and the
engine's own ``_block_entries`` latch — the same one a failed warm-up sets.
"""

from __future__ import annotations

import pickle
import queue as queue_module
from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.engine.config import EngineConfig, SessionConfig
from common.engine.engine import TradingEngine
from common.engine.hub_feed import HubTickFeed
from common.engine.positions import InMemoryGateway, PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.feed.hub import SharedFeedHub, WorkerChannel, build_channel
from common.feed.queues import (
    DEFAULT_TICK_MAX_DEPTH,
    DROP_NOTICE_EVERY,
    BoundedWorkerQueue,
    TickDropNotice,
    drop_notice_cadence,
)
from common.models import Tick
from strategies.intraday_options.engine_fixture_strategy import EngineFixtureStrategy

IST = ZoneInfo("Asia/Kolkata")
UNDERLYING = "INDEX"
LOT_SIZE = 65
CE_CONTRACT = "SIM:NIFTY:WEEKLY:24000:CE"
STRATEGY_ID = "engine01"


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 7, 16, hour, minute, second, tzinfo=IST)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id,
        instrument=security_id,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


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

    def start(self, on_tick) -> None:
        self.is_running = True
        try:
            for tick in self._ticks:
                on_tick(tick)
        finally:
            self.is_running = False


def _overflowing_run(tape: Sequence[Tick], depth: int) -> WorkerChannel:
    """Drive a real hub over a real tick channel that is deliberately too small.

    The hub's ``start()`` returns once the scripted tape is exhausted, so the
    overflow has finished by the time this returns — no race between producer and
    consumer, and therefore no flaky "did it drop?" assertion.
    """
    registered = build_channel(STRATEGY_ID, [UNDERLYING], in_process=True, tick_channel=True)
    # Same class, same drop-oldest policy — only the depth differs, so the
    # overflow is the real mechanism rather than a simulated one.
    channel = WorkerChannel(
        strategy_id=registered.strategy_id,
        security_ids=registered.security_ids,
        queue=registered.queue,
        tick_queue=BoundedWorkerQueue.in_process(f"{STRATEGY_ID}:ticks", max_depth=depth),
    )
    hub = SharedFeedHub(_ScriptedAdapter(tape), interval_seconds=60)
    hub.register(channel)
    hub.start()
    return channel


def _drain(q: BoundedWorkerQueue) -> list[object]:
    items: list[object] = []
    while True:
        try:
            items.append(q.get(timeout=0.01))
        except queue_module.Empty:
            return items


def _engine_over(
    q: BoundedWorkerQueue, *, block_on_drop: bool = True, **strategy_kwargs
) -> tuple[TradingEngine, PositionManager, HubTickFeed]:
    """A real engine over ``q``, with the drop notice wired to its entry latch.

    The engine needs its feed at construction and the feed's callback needs the
    engine, so the callback closes over a one-slot list rather than either object
    reaching into the other afterwards.
    """
    slot: list[TradingEngine] = []

    def _on_drop(notice: TickDropNotice) -> None:
        slot[0].block_entries(
            f"a tick was dropped upstream (total {notice.dropped}) — this worker's "
            "candles may be silently wrong"
        )

    feed = HubTickFeed(
        q,
        on_tick_dropped=_on_drop if block_on_drop else None,
        idle_timeout_seconds=0.3,
        poll_seconds=0.05,
    )
    positions = PositionManager(InMemoryGateway(slippage_points=0.0), lots=1)
    engine = TradingEngine(
        EngineConfig(
            timeframe="5m",
            session=SessionConfig(
                timezone="Asia/Kolkata",
                start_time="09:15",
                end_time="15:15",
                square_off_time="15:20",
            ),
        ),
        feed=feed,
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=LOT_SIZE), strike_step=50
        ),
        strategy=EngineFixtureStrategy(**{"enter_on_candle": 1, **strategy_kwargs}),
        position_manager=positions,
        underlying_security_id=UNDERLYING,
    )
    slot.append(engine)
    return engine, positions, feed


# ------------------------------------------------- the notice reaches the child


def test_a_dropped_tick_puts_a_notice_on_the_queue_the_child_reads() -> None:
    channel = _overflowing_run(
        [_tick(UNDERLYING, 24000.0 + i, _ts(9, 16, i % 60)) for i in range(20)], depth=4
    )
    assert channel.tick_queue is not None
    assert channel.tick_queue.dropped > 0, "the queue must actually have overflowed"
    assert any(isinstance(item, TickDropNotice) for item in _drain(channel.tick_queue))


def test_no_notice_appears_when_nothing_is_dropped() -> None:
    """The negative control. A notice on a healthy run would block trading for the
    day for no reason, which is a worse failure than the one being reported."""
    tape = [_tick(UNDERLYING, 24000.0 + i, _ts(9, 16, i)) for i in range(20)]
    channel = build_channel(STRATEGY_ID, [UNDERLYING], in_process=True, tick_channel=True)
    hub = SharedFeedHub(_ScriptedAdapter(tape), interval_seconds=60)
    hub.register(channel)
    hub.start()

    assert channel.tick_queue is not None
    assert channel.tick_queue.dropped == 0
    assert not any(isinstance(item, TickDropNotice) for item in _drain(channel.tick_queue))


def test_the_notice_carries_the_running_drop_count() -> None:
    channel = _overflowing_run(
        [_tick(UNDERLYING, 24000.0 + i, _ts(9, 16, i % 60)) for i in range(30)], depth=4
    )
    assert channel.tick_queue is not None
    notices = [item for item in _drain(channel.tick_queue) if isinstance(item, TickDropNotice)]
    assert notices, "sustained overflow must keep re-arming the notice"
    assert notices[-1].dropped > 0
    assert [n.dropped for n in notices] == sorted(n.dropped for n in notices)


def test_the_notice_re_arms_under_sustained_overflow() -> None:
    """Why in-band delivery is sound *under the condition it reports*.

    The queue is drop-oldest, so a notice can itself age out while the consumer is
    still behind. Every further drop enqueues another, and the latch needs only one
    to land — so the guarantee is "at least one survives", not "none is lost".
    """
    channel = _overflowing_run(
        [_tick(UNDERLYING, 24000.0 + i, _ts(9, 16, i % 60)) for i in range(200)], depth=4
    )
    assert channel.tick_queue is not None
    assert channel.tick_queue.dropped > 100
    assert any(isinstance(item, TickDropNotice) for item in _drain(channel.tick_queue))


@pytest.mark.parametrize("depth", [4, 8, 16])
def test_the_notice_arrives_whatever_the_queue_depth(depth: int) -> None:
    channel = _overflowing_run(
        [_tick(UNDERLYING, 24000.0 + i, _ts(9, 16, i % 60)) for i in range(300)], depth=depth
    )
    assert channel.tick_queue is not None
    assert any(isinstance(item, TickDropNotice) for item in _drain(channel.tick_queue))


def test_the_notice_is_sent_on_a_cadence_not_once_per_drop() -> None:
    """The notice competes for space with the ticks it reports on.

    It is pushed into a queue that is full by definition, so it evicts a real tick.
    One per drop was the first implementation and was **measured** to be the wrong
    trade — at the deployed depth it cost 358 extra lost ticks per 6000 against a
    lagging consumer (6.3%) to make the entry block engage 6.5% sooner, where a
    cadence of 8 costs 52 (0.9%) for the same latency. See D28.

    Pinned as a ratio rather than an exact count so the test says what it means: far
    fewer notices than drops, but never none.
    """
    channel = _overflowing_run(
        [_tick(UNDERLYING, 24000.0 + i, _ts(9, 16, i % 60)) for i in range(300)], depth=16
    )
    assert channel.tick_queue is not None
    notices = [item for item in _drain(channel.tick_queue) if isinstance(item, TickDropNotice)]
    # Notices seen is bounded by what survives eviction, so compare against the
    # drops that *happened*, which is the number one-per-drop would have produced.
    assert channel.tick_queue.dropped > 100
    assert len(notices) < channel.tick_queue.dropped / DROP_NOTICE_EVERY
    assert notices, "a cadence that never reports is worse than no cadence at all"


def test_the_cadence_is_clamped_below_the_queue_depth() -> None:
    """The invariant the cadence depends on, stated as an assertion.

    A notice reaches the consumer only if it is still inside the retained window,
    and roughly one tick is published per drop — so a cadence at or above the depth
    lets every notice be evicted before the next is sent. Measured: a fixed cadence
    of 8 reported every overflow at depth 8 and above but **lost the notice entirely
    at depth 4**.
    """
    assert drop_notice_cadence(DEFAULT_TICK_MAX_DEPTH) == DROP_NOTICE_EVERY
    assert drop_notice_cadence(16) == DROP_NOTICE_EVERY  # the clamp does not bite here
    assert drop_notice_cadence(4) == 2
    assert drop_notice_cadence(2) == 1
    assert drop_notice_cadence(1) == 1, "never zero — that would divide the world by nothing"


@pytest.mark.parametrize(
    ("depth", "n_ticks"),
    # Only pairs where n_ticks > depth: a case that never overflows proves nothing,
    # and the first draft of this sweep silently included three such cells.
    [
        (4, 30),
        (4, 80),
        (4, 200),
        (8, 30),
        (8, 80),
        (8, 200),
        (16, 30),
        (16, 80),
        (16, 200),
        (64, 80),
        (64, 200),
        (64, 1000),
    ],
)
def test_every_overflow_is_reported_at_every_depth(depth: int, n_ticks: int) -> None:
    """The guarantee, swept rather than spot-checked.

    The fixed cadence passed at depth 8+ and failed at 4, which is exactly the kind
    of gap a single hand-picked case misses. Every cell here overflows, so every
    cell must report.
    """
    channel = _overflowing_run(
        [_tick(UNDERLYING, 24000.0 + i, _ts(9, 16, i % 60)) for i in range(n_ticks)], depth=depth
    )
    assert channel.tick_queue is not None
    assert channel.tick_queue.dropped > 0, "this case must actually overflow to mean anything"
    assert any(isinstance(item, TickDropNotice) for item in _drain(channel.tick_queue))


def test_the_notice_survives_a_pickle_round_trip() -> None:
    """It crosses a process boundary in the deployed shape, so the consumer must
    match on type rather than on identity with a module-level sentinel."""
    restored = pickle.loads(pickle.dumps(TickDropNotice(dropped=5)))
    assert isinstance(restored, TickDropNotice)
    assert restored.dropped == 5


# --------------------------------------------------- the feed and the engine


def test_the_feed_reports_the_drop_and_keeps_delivering_ticks() -> None:
    """A notice is not a sentinel: the stream continues, only entries stop."""
    q = BoundedWorkerQueue.in_process("ticks", max_depth=32)
    seen: list[Tick] = []
    reported: list[int] = []
    feed = HubTickFeed(
        q,
        on_tick_dropped=lambda notice: reported.append(notice.dropped),
        idle_timeout_seconds=0.2,
        poll_seconds=0.05,
    )
    feed.on_tick(seen.append)
    q.publish(_tick(UNDERLYING, 24000.0, _ts(9, 16)))
    q.publish(TickDropNotice(dropped=7))
    q.publish(_tick(UNDERLYING, 24010.0, _ts(9, 17)))
    feed.run()

    assert [t.last_price for t in seen] == [24000.0, 24010.0]
    assert reported == [7]
    assert feed.ticks_received == 2, "a notice is not a tick"
    assert feed.ticks_dropped_upstream == 7


def test_a_notice_without_a_callback_is_still_counted_and_harmless() -> None:
    """``HubTickFeed`` predates this hook and is constructed without it elsewhere."""
    q = BoundedWorkerQueue.in_process("ticks", max_depth=32)
    feed = HubTickFeed(q, idle_timeout_seconds=0.2, poll_seconds=0.05)
    feed.on_tick(lambda _t: None)
    q.publish(TickDropNotice(dropped=3))
    feed.run()
    assert feed.ticks_dropped_upstream == 3


def test_a_notice_is_not_mistaken_for_the_shutdown_sentinel() -> None:
    """``None`` ends the run; a notice must not."""
    q = BoundedWorkerQueue.in_process("ticks", max_depth=32)
    feed = HubTickFeed(q, idle_timeout_seconds=0.3, poll_seconds=0.05)
    feed.on_tick(lambda _t: None)
    q.publish(TickDropNotice(dropped=1))
    q.publish(_tick(UNDERLYING, 24000.0, _ts(9, 16)))
    feed.run()
    assert feed.stopped_by_sentinel is False
    assert feed.stopped_by_idle_timeout is True
    assert feed.ticks_received == 1


def test_the_engine_refuses_new_entries_after_a_drop() -> None:
    """The end of the chain: queue → feed → ``_block_entries`` → no entry.

    The engine's latch is the one a failed warm-up already sets, reused rather
    than reinvented, so the "no new entries today" semantics are identical.
    """
    q = BoundedWorkerQueue.in_process("ticks", max_depth=64)
    engine, positions, _feed = _engine_over(q)
    q.publish(TickDropNotice(dropped=1))
    q.publish(_tick(UNDERLYING, 24000.0, _ts(9, 16)))
    q.publish(_tick(UNDERLYING, 24010.0, _ts(9, 21)))  # closes candle #1 → ENTER
    engine.run()

    assert engine.entries_blocked is not None
    assert "dropped" in engine.entries_blocked
    assert positions.positions == [], "the entry must be refused, not merely logged"
    assert positions.trades == []


def test_the_same_tape_does_enter_without_a_drop() -> None:
    """The control that makes the test above mean something."""
    q = BoundedWorkerQueue.in_process("ticks", max_depth=64)
    engine, positions, _feed = _engine_over(q)
    q.publish(_tick(UNDERLYING, 24000.0, _ts(9, 16)))
    q.publish(_tick(UNDERLYING, 24010.0, _ts(9, 21)))
    q.publish(_tick(CE_CONTRACT, 100.0, _ts(9, 21, 30)))
    engine.run()

    assert engine.entries_blocked is None
    assert len(positions.positions) == 1


def test_an_open_position_still_exits_after_a_drop() -> None:
    """Entry side only. A block that trapped an open position would convert a data
    quality problem into an unmanaged risk problem."""
    q = BoundedWorkerQueue.in_process("ticks", max_depth=256)
    engine, positions, _feed = _engine_over(q, premium_exit=True)

    for tick in (
        _tick(UNDERLYING, 24000.0, _ts(9, 16)),
        _tick(UNDERLYING, 24010.0, _ts(9, 21)),
        _tick(CE_CONTRACT, 100.0, _ts(9, 21, 30)),
    ):
        q.publish(tick)
    q.publish(TickDropNotice(dropped=1))
    for ts, price in (
        (_ts(9, 23), 105.0),
        (_ts(9, 26), 110.0),
        (_ts(9, 28), 108.0),
        (_ts(9, 31), 90.0),
        (_ts(9, 33), 85.0),
        (_ts(9, 36), 80.0),
    ):
        q.publish(_tick(CE_CONTRACT, price, ts))
    engine.run()

    assert engine.entries_blocked is not None
    assert len(positions.trades) == 1, "the position opened before the drop must still exit"
    assert positions.positions == []


def test_the_block_is_latched_for_the_day_not_re_reasoned_per_drop() -> None:
    q = BoundedWorkerQueue.in_process("ticks", max_depth=32)
    engine, _positions, feed = _engine_over(q)
    q.publish(TickDropNotice(dropped=1))
    q.publish(TickDropNotice(dropped=9))
    engine.run()

    assert feed.ticks_dropped_upstream == 9, "every notice is still observed"
    assert engine.entries_blocked is not None
    assert "total 1" in engine.entries_blocked, "the first reason is the one that sticks"


# ------------------------------------------------------- hub to engine, for real


def test_a_real_hub_overflow_blocks_a_real_engines_entries() -> None:
    """The whole path with nothing hand-published: real hub, real overflow, real
    engine, and an entry that the engine refuses as a result."""
    tape = [_tick(UNDERLYING, 24000.0 + (i % 20), _ts(9, 16, i % 60)) for i in range(400)]
    tape.append(_tick(UNDERLYING, 24010.0, _ts(9, 21)))  # would close candle #1
    channel = _overflowing_run(tape, depth=8)

    assert channel.tick_queue is not None
    assert channel.tick_queue.dropped > 0
    engine, positions, feed = _engine_over(channel.tick_queue)
    engine.run()

    assert feed.ticks_dropped_upstream > 0
    assert engine.entries_blocked is not None
    assert positions.positions == []


def test_the_default_depth_still_drops_nothing_at_the_measured_live_rate() -> None:
    """The 2b-ii-A mitigation must still hold, or this block would fire on healthy
    runs and quietly stop the day's trading."""
    q = BoundedWorkerQueue.in_process("ticks", max_depth=2048)
    for i in range(240):  # 4 ticks/s for 60 s — Block 2's observed rate
        assert q.publish(_tick(UNDERLYING, 24000.0, _ts(9, 16) + timedelta(milliseconds=250 * i)))
    assert q.dropped == 0
