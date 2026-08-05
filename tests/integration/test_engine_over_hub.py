"""The real engine, driven entirely off the hub's tick channel.

Phase 3 Part 2b-ii-A's acceptance gate. Every part is real: a real
:class:`~common.feed.hub.SharedFeedHub` fanning out to a real bounded queue, a
real :class:`~common.engine.hub_feed.HubTickFeed` draining it, a real
:class:`~common.engine.engine.TradingEngine`, and a real Part 2a exit policy
closing the position. Nothing is monkeypatched.

What this gate does **not** cover, deliberately: the persisted
:class:`~common.models.Position`. Execution here still goes through
``InMemoryGateway``, because ``LifecycleGateway`` is Part 2b-ii-B — and so is the
real-`Position`-vs-real-exit-engine gate that needs it.

Why the hub and the engine run on separate threads
--------------------------------------------------
Because that is the real topology — the supervisor drives the feed on its own
thread while each worker consumes — and because the causality only exists that
way round. The engine picks its option contract *at runtime*; until that
subscription has travelled upstream and been applied, a real feed delivers no
ticks for it. Replaying the whole tape into the queue first and consuming
afterwards would test a stream that cannot happen.

``_PacedAdapter`` encodes exactly that causality with no sleeps: it holds the
option ticks back until the subscription has actually been applied, then releases
them. A timeout on that wait is what turns "the round trip never completed" into
a failed assertion rather than a hung suite.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.engine.config import EngineConfig, SessionConfig
from common.engine.engine import TradingEngine
from common.engine.hub_feed import HubTickFeed
from common.engine.models import ExitReason, OptionType, OrderSide
from common.engine.positions import InMemoryGateway, PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.feed import SharedFeedHub
from common.feed.hub import build_channel
from common.market_data.adapter import TickCallback
from common.models import Tick

IST = ZoneInfo("Asia/Kolkata")
UNDERLYING = "INDEX"
LOT_SIZE = 65
CE_CONTRACT = "SIM:NIFTY:WEEKLY:24000:CE"
STRATEGY_ID = "engine01"

#: How long the feed thread waits for a runtime subscription to be applied, and
#: how long the test waits for either thread. Generous: both are in-process, so
#: exceeding these means something is wedged, not slow.
ROUND_TRIP_TIMEOUT = 10.0


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


#: Two underlying ticks; the second closes the 09:15 five-minute bar and produces
#: the ENTER that makes the engine choose — and subscribe to — its contract.
_ENTRY_TAPE = [
    _tick(UNDERLYING, 24000.0, _ts(9, 16)),
    _tick(UNDERLYING, 24010.0, _ts(9, 21)),
]

#: Fills the pending entry at 100, walks the premium up (105, 108) and then down
#: (85), so exactly one adverse *closed* premium bar exists. Same series the
#: Part 2b-i premium-candle suite uses, so a divergence here is about the hub.
_PREMIUM_WALK = [
    (_ts(9, 21, 30), 100.0),
    (_ts(9, 23), 105.0),
    (_ts(9, 26), 110.0),
    (_ts(9, 28), 108.0),
    (_ts(9, 31), 90.0),
    (_ts(9, 33), 85.0),
    (_ts(9, 36), 80.0),
]


#: Ceiling on keepalive ticks emitted while waiting for the round trip. Reaching
#: it means the subscription never arrived, which fails the assertion below
#: rather than hanging the suite. With the interval below that is a ~4 s budget.
_MAX_KEEPALIVES = 400
_KEEPALIVE_INTERVAL = 0.01


class _PacedAdapter:
    """Replays ``first``, waits for the runtime subscription, then replays ``then``.

    Satisfies :class:`~common.market_data.adapter.MarketFeedAdapter` structurally.
    The wait is the point: it is what a real feed does implicitly by having
    nothing to send for an instrument nobody subscribed to.

    While it waits it keeps emitting **underlying** ticks, because that is what a
    live index feed does — and because the hub applies a pending subscription at a
    tick boundary, so a double that went silent here would be reproducing runbook
    limitation 13 rather than testing the round trip. The keepalives advance by a
    second inside the same five-minute bucket, so they close no extra candle and
    change no engine decision.
    """

    def __init__(
        self,
        first: Sequence[Tick],
        then: Sequence[Tick],
        *,
        released_by: threading.Event,
    ) -> None:
        self._first = list(first)
        self._then = list(then)
        self._released_by = released_by
        self._subscribed: set[str] = set()
        self._running = False
        self.released = False
        self.keepalives_sent = 0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def subscribed(self) -> frozenset[str]:
        return frozenset(self._subscribed)

    def subscribe(
        self,
        security_ids: Sequence[str],
        *,
        segment: int | None = None,
        mode: int | None = None,
    ) -> None:
        self._subscribed.update(security_ids)

    def request_stop(self) -> None:
        self._running = False

    def stop(self) -> None:
        self._running = False

    def start(self, on_tick: TickCallback) -> None:
        self._running = True
        for tick in self._first:
            if not self._running:
                break
            if tick.security_id in self._subscribed:
                on_tick(tick)

        # The engine has now had its ENTER and asked for a contract. Keep the
        # underlying alive until that request has completed the round trip.
        #
        # Paced by the wait itself: the round trip crosses to the engine's thread
        # and back, so a tight loop here would exhaust the keepalive budget before
        # that thread was ever scheduled. Timestamps advance by a microsecond so
        # they stay strictly increasing without leaving the five-minute bucket.
        last = self._first[-1]
        while self._running and self.keepalives_sent < _MAX_KEEPALIVES:
            if self._released_by.wait(timeout=_KEEPALIVE_INTERVAL):
                break
            self.keepalives_sent += 1
            on_tick(
                _tick(
                    last.security_id,
                    last.last_price,
                    last.exchange_time + timedelta(microseconds=self.keepalives_sent),
                )
            )
        self.released = self._released_by.is_set()

        for tick in self._then:
            if not self._running:
                break
            if tick.security_id in self._subscribed:
                on_tick(tick)
        self._running = False


def _build_engine(feed: HubTickFeed) -> tuple[TradingEngine, PositionManager]:
    from strategies.intraday_options.engine_fixture_strategy import EngineFixtureStrategy

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
        strategy=EngineFixtureStrategy(enter_on_candle=1, premium_exit=True),
        position_manager=positions,
        underlying_security_id=UNDERLYING,
    )
    return engine, positions


@pytest.fixture
def wired():
    """Hub + channel + HubTickFeed + engine, wired as the runtime wires them."""
    subscribed = threading.Event()

    adapter = _PacedAdapter(
        _ENTRY_TAPE,
        [_tick(CE_CONTRACT, price, ts) for ts, price in _PREMIUM_WALK],
        released_by=subscribed,
    )
    hub = SharedFeedHub(adapter, interval_seconds=60)
    channel = build_channel(STRATEGY_ID, [UNDERLYING], in_process=True, tick_channel=True)
    hub.register(channel)

    def _request(security_id: str) -> None:
        # In the deployed shape this hop crosses a process boundary via the
        # supervisor's control queue; in-process it is the same call.
        hub.request_subscription(STRATEGY_ID, security_id)

    assert channel.tick_queue is not None
    feed = HubTickFeed(
        channel.tick_queue,
        request_subscription=_request,
        idle_timeout_seconds=2.0,
        poll_seconds=0.05,
    )
    engine, positions = _build_engine(feed)

    original_apply = hub._apply_pending_subscriptions

    def _apply_and_signal() -> None:
        original_apply()
        if CE_CONTRACT in adapter.subscribed:
            subscribed.set()

    # Observation only — it calls straight through, and exists so the paced
    # adapter can release on the real event rather than on a timer.
    hub._apply_pending_subscriptions = _apply_and_signal  # type: ignore[method-assign]

    return hub, channel, feed, engine, positions, adapter


def _run(hub: SharedFeedHub, engine: TradingEngine) -> None:
    """Feed on its own thread, engine on this one — the deployed topology."""
    feed_thread = threading.Thread(target=hub.start, name="hub", daemon=True)
    feed_thread.start()
    try:
        engine.run()
    finally:
        hub.request_stop()
        feed_thread.join(timeout=ROUND_TRIP_TIMEOUT)
        assert not feed_thread.is_alive(), "the feed thread did not finish"


def test_the_engine_trades_end_to_end_off_the_hubs_tick_channel(wired):
    hub, _channel, _feed, engine, positions, adapter = wired
    _run(hub, engine)

    assert adapter.released, "the runtime subscription never completed its round trip"
    assert len(positions.trades) == 1

    trade = positions.trades[0]
    assert trade.contract.security_id == CE_CONTRACT
    assert trade.entry_price == 100.0
    assert trade.exit_price == 80.0
    # A real Part 2a policy made this decision, on the traded option's own bars.
    assert trade.exit_reason is ExitReason.MOMENTUM_LOW
    assert trade.contract.option_type is OptionType.CE
    assert trade.side is OrderSide.BUY


def test_the_contract_the_engine_chose_was_subscribed_at_runtime(wired):
    hub, channel, _feed, engine, _positions, adapter = wired
    _run(hub, engine)

    # It was not in the configured union — the engine picked it mid-session.
    assert CE_CONTRACT not in channel.security_ids
    assert CE_CONTRACT in channel.dynamic_ids
    assert CE_CONTRACT in adapter.subscribed
    assert hub.subscriptions_applied == 1


def test_both_streams_reached_the_engine_through_one_queue(wired):
    hub, channel, feed, engine, _positions, adapter = wired
    _run(hub, engine)

    assert channel.tick_queue is not None
    assert channel.tick_queue.dropped == 0
    # Every underlying and premium tick, plus the keepalives, over one channel.
    expected = len(_ENTRY_TAPE) + len(_PREMIUM_WALK) + adapter.keepalives_sent
    assert feed.ticks_received == expected
    assert channel.tick_queue.published == expected


def test_the_run_ends_cleanly_with_the_ownership_rule_intact(wired):
    """Part 1's rule, unbroken by the new plumbing.

    The stream runs out rather than a shutdown being requested, so the tick feed
    ends itself on its idle timeout — on its own thread, which is the only one
    that ever ends it.
    """
    hub, _channel, feed, engine, _positions, _adapter = wired
    _run(hub, engine)

    assert feed.stopped_by_idle_timeout is True
    assert feed.stopped_by_sentinel is False
    assert engine.wait_until_stopped(timeout=1.0) is True
    assert engine.stopped_by_request is False
