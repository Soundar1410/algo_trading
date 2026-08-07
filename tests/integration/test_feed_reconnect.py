"""Reconnection, resubscription, and candle integrity across a feed gap.

The property that matters most is negative: a feed outage must not be able to
produce a *wrong candle*. A missing candle is a visible, recoverable gap. A bar
silently stitched together from the ticks either side of a hole is indistinguishable
from a real one, and every indicator built on it inherits the error.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from common.candles import CandleAggregator
from common.feed import (
    FeedHealthEvent,
    FeedUnavailableError,
    ReconnectingFeed,
    ReconnectPolicy,
    SharedFeedHub,
)
from common.feed.hub import build_channel
from common.market_data.adapter import TickCallback
from common.models import Tick

SECURITY_ID = "49081"
OTHER_ID = "13"
SESSION_START = datetime(2026, 7, 30, 9, 15, 0, tzinfo=UTC) - timedelta(hours=5, minutes=30)


def _tick(offset_seconds: int, price: float = 100.0, security_id: str = SECURITY_ID) -> Tick:
    moment = SESSION_START + timedelta(seconds=offset_seconds)
    return Tick(
        security_id=security_id,
        instrument="NIFTY",
        last_price=price,
        exchange_time=moment,
        received_at=moment,
        last_quantity=1,
    )


class _ScriptedAdapter:
    """A feed driven by a script of tick batches and failures.

    Models a real socket: one ``start()`` call delivers a batch and *then* dies,
    if the next scripted item is an exception. Returning cleanly means this
    connection finished normally — which for the recorded adapter is an exhausted
    tape and for the live adapter means ``stop()`` was called.
    """

    def __init__(self, *script: Sequence[Tick] | Exception) -> None:
        self._script = list(script)
        self.subscribe_calls: list[list[str]] = []
        self.resubscribe_calls = 0
        self.stop_calls = 0
        self.start_calls = 0
        self._security_ids: set[str] = set()
        self._running = False
        self.last_disconnect_code: int | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def subscribed(self) -> frozenset[str]:
        return frozenset(self._security_ids)

    def subscribe(self, security_ids: Sequence[str], *, segment: int | None = None) -> None:
        self.subscribe_calls.append(list(security_ids))
        self._security_ids.update(str(s) for s in security_ids)

    def resubscribe_all(self) -> None:
        self.resubscribe_calls += 1

    def start(self, on_tick: TickCallback) -> None:
        self.start_calls += 1
        self._running = True
        try:
            while self._script:
                item = self._script.pop(0)
                if isinstance(item, Exception):
                    raise item
                for tick in item:
                    on_tick(tick)
                # Deliver, then die — the realistic shape of a dropped socket.
                if self._script and isinstance(self._script[0], Exception):
                    continue
                return
        finally:
            self._running = False

    def stop(self) -> None:
        self.stop_calls += 1
        self._running = False


def _feed(adapter: _ScriptedAdapter, **kwargs: object) -> ReconnectingFeed:
    defaults: dict[str, object] = {
        "policy": ReconnectPolicy(max_attempts=4, initial_backoff=0.001, max_backoff=0.002),
        "sleep": lambda _s: None,
        "rng": lambda: 0.0,
    }
    defaults.update(kwargs)
    return ReconnectingFeed(adapter, **defaults)  # type: ignore[arg-type]


# ------------------------------------------------------------------ the policy
def test_backoff_grows_and_is_capped():
    policy = ReconnectPolicy(
        initial_backoff=1.0, max_backoff=10.0, multiplier=2.0, jitter_ratio=0.0
    )
    delays = [policy.delay_for(n) for n in range(1, 7)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]


def test_jitter_only_reduces_so_the_cap_is_a_real_cap():
    """An operator reading max_backoff=60 must never observe a 75s wait."""
    policy = ReconnectPolicy(initial_backoff=8.0, max_backoff=8.0, jitter_ratio=0.25)
    for rng_value in (0.0, 0.5, 0.999):
        # Bound as a default argument: a bare closure over the loop variable is
        # a latent bug the moment delay_for stops calling rng() eagerly.
        delay = policy.delay_for(3, rng=lambda value=rng_value: value)
        assert 6.0 <= delay <= 8.0


def test_jitter_desynchronises_two_runtimes():
    """Without jitter, runtimes restarting together collide repeatedly."""
    policy = ReconnectPolicy(initial_backoff=4.0, jitter_ratio=0.5)
    first = policy.delay_for(2, rng=lambda: 0.1)
    second = policy.delay_for(2, rng=lambda: 0.9)
    assert first != second


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"initial_backoff": 0},
        {"max_backoff": -1},
        {"multiplier": 0.5},
        {"jitter_ratio": 1.5},
        {"jitter_ratio": -0.1},
    ],
)
def test_a_nonsensical_policy_is_refused(kwargs: dict[str, float]):
    with pytest.raises(ValueError):
        ReconnectPolicy(**kwargs)  # type: ignore[arg-type]


def test_a_multiplier_below_one_is_refused_because_backoff_would_shrink():
    with pytest.raises(ValueError, match="backoff would shrink"):
        ReconnectPolicy(multiplier=0.9)


# ------------------------------------------------------------- reconnect loop
def test_a_transient_failure_reconnects_and_resumes():
    adapter = _ScriptedAdapter(
        [_tick(1)],
        ConnectionResetError("socket died"),
        [_tick(61)],
    )
    feed = _feed(adapter)
    feed.subscribe([SECURITY_ID])

    received: list[Tick] = []

    def collect(tick: Tick) -> None:
        received.append(tick)

    feed.start(collect)

    assert len(received) == 2
    assert feed.health.reconnect_count >= 1
    assert feed.health.failed_attempts == 1


def test_the_full_subscription_set_is_resent_after_a_reconnect():
    """A new socket carries none of the old subscriptions, while subscribe()
    deliberately sends only the delta."""
    adapter = _ScriptedAdapter([_tick(1)], ConnectionResetError("drop"), [])
    feed = _feed(adapter)
    feed.subscribe([SECURITY_ID, OTHER_ID])

    feed.start(lambda _t: None)

    assert adapter.resubscribe_calls >= 1
    assert adapter.subscribed == {SECURITY_ID, OTHER_ID}
    assert feed.health.subscriptions_match


def test_resubscription_does_not_duplicate_instruments():
    adapter = _ScriptedAdapter(
        ConnectionResetError("a"),
        ConnectionResetError("b"),
        [],
    )
    feed = _feed(adapter)
    feed.subscribe([SECURITY_ID])
    feed.subscribe([SECURITY_ID, OTHER_ID])

    feed.start(lambda _t: None)

    assert adapter.subscribed == {SECURITY_ID, OTHER_ID}
    for call in adapter.subscribe_calls:
        assert len(call) == len(set(call)), f"duplicate instruments in {call}"


def test_the_attempt_budget_is_bounded_and_fails_closed():
    adapter = _ScriptedAdapter(*[ConnectionResetError("nope") for _ in range(10)])
    feed = _feed(adapter, policy=ReconnectPolicy(max_attempts=3, initial_backoff=0.001))
    feed.subscribe([SECURITY_ID])

    with pytest.raises(FeedUnavailableError, match="did not recover after 3"):
        feed.start(lambda _t: None)

    assert feed.health.failed_attempts == 3
    assert feed.health.connected is False


def test_starting_with_no_subscriptions_is_refused():
    feed = _feed(_ScriptedAdapter())
    with pytest.raises(FeedUnavailableError, match="no subscriptions"):
        feed.start(lambda _t: None)


def test_stopping_from_the_callback_suppresses_further_reconnection():
    """Shutdown mid-session: a stop must not be mistaken for an outage."""
    adapter = _ScriptedAdapter([_tick(1)], ConnectionResetError("would reconnect"))
    feed = _feed(adapter)
    feed.subscribe([SECURITY_ID])

    def stop_on_first(_tick: Tick) -> None:
        feed.stop()

    feed.start(stop_on_first)

    assert adapter.start_calls == 1, "no reconnect after a deliberate stop"
    assert feed.health.failed_attempts == 0


def test_a_clean_return_ends_the_run_rather_than_reconnecting():
    """A recorded tape reaching its end is the normal case, not a failure.

    The live adapter loops `while self._running` and only stop() clears that
    flag, so a clean return already means an intentional stop there too.
    Treating a clean return as an outage would make this wrapper unusable with
    the recorded feed that every automated test and the walking skeleton rely on.
    """
    adapter = _ScriptedAdapter([_tick(1)], [_tick(61)])
    feed = _feed(adapter)
    feed.subscribe([SECURITY_ID])

    received: list[Tick] = []
    feed.start(received.append)

    assert adapter.start_calls == 1, "the second batch must not be replayed"
    assert len(received) == 1
    assert feed.health.failed_attempts == 0


def test_the_attempt_budget_counts_consecutive_failures_not_lifetime_ones():
    """A runtime that recovers instantly nine times must not be killed by the
    tenth drop. The budget bounds one unrecovered outage."""
    adapter = _ScriptedAdapter(
        ConnectionResetError("1"),
        [_tick(1)],  # recovery: a real tick arrives
        ConnectionResetError("2"),
        [_tick(61)],  # recovery again
        ConnectionResetError("3"),
        [_tick(121)],
    )
    feed = _feed(adapter, policy=ReconnectPolicy(max_attempts=2, initial_backoff=0.001))
    feed.subscribe([SECURITY_ID])

    received: list[Tick] = []
    feed.start(received.append)

    assert len(received) == 3
    assert feed.health.failed_attempts == 3, "all three drops are recorded"
    assert feed.health.reconnect_count == 3


# ---------------------------------------------------------------- token expiry
def test_a_807_disconnect_refreshes_the_token():
    """Reason code 807 is access-token expiry. Backing off cannot fix it —
    reconnecting with the same dead token fails identically forever."""
    adapter = _ScriptedAdapter(ConnectionResetError("closed"), [])
    adapter.last_disconnect_code = 807
    refreshes: list[int] = []

    feed = _feed(adapter, refresh_token=lambda: refreshes.append(1))
    feed.subscribe([SECURITY_ID])
    feed.start(lambda _t: None)

    assert refreshes == [1]
    assert feed.health.token_refreshes == 1
    assert feed.health.last_disconnect_code == 807
    assert feed.health.last_disconnect_reason == "access token expired"


@pytest.mark.parametrize("code", [805, 806, 808, 809])
def test_other_disconnect_codes_do_not_refresh_the_token(code: int):
    """Minting a token needlessly spends a ~1-per-2-minute budget."""
    adapter = _ScriptedAdapter(ConnectionResetError("closed"), [])
    adapter.last_disconnect_code = code
    refreshes: list[int] = []

    feed = _feed(adapter, refresh_token=lambda: refreshes.append(1))
    feed.subscribe([SECURITY_ID])
    feed.start(lambda _t: None)

    assert refreshes == []
    assert feed.health.token_refreshes == 0


def test_a_failing_token_refresh_does_not_abort_the_reconnect_loop():
    adapter = _ScriptedAdapter(ConnectionResetError("closed"), [])
    adapter.last_disconnect_code = 807

    def _boom() -> None:
        raise RuntimeError("auth server down")

    feed = _feed(adapter, refresh_token=_boom)
    feed.subscribe([SECURITY_ID])
    feed.start(lambda _t: None)  # must not raise

    assert feed.health.token_refreshes == 0


# ------------------------------------------------------------ degraded/staleness
def test_the_feed_starts_degraded_and_clears_only_on_a_real_tick():
    """The spec requires fresh ticks before clearing degraded state: a socket
    that connects but delivers nothing is not a recovered feed."""
    adapter = _ScriptedAdapter([_tick(1)])
    feed = _feed(adapter)
    feed.subscribe([SECURITY_ID])
    assert feed.health.degraded is True

    feed.start(lambda _t: None)
    assert feed.health.degraded is False


def test_a_reconnect_marks_the_feed_degraded_again():
    adapter = _ScriptedAdapter([_tick(1)], ConnectionResetError("drop"), [])
    feed = _feed(adapter)
    feed.subscribe([SECURITY_ID])
    feed.start(lambda _t: None)

    assert feed.health.degraded is True, "no tick arrived after the reconnect"


def test_downtime_is_accumulated_across_outages():
    base = datetime(2026, 7, 30, 9, 0, 0, tzinfo=UTC)
    offsets = [0, 10, 25, 30, 40]  # connect, drop, reconnect(+15s), drop, reconnect(+10s)
    moments = [base + timedelta(seconds=o) for o in offsets]
    calls = iter(moments)

    def clock() -> datetime:
        # Returning the final value rather than raising keeps a clock-call-count
        # change from failing this test for an unrelated reason.
        return next(calls, moments[-1])

    adapter = _ScriptedAdapter(ConnectionResetError("a"), ConnectionResetError("b"), [])
    feed = _feed(adapter, clock=clock)
    feed.subscribe([SECURITY_ID])
    feed.start(lambda _t: None)

    assert feed.health.total_downtime_seconds == pytest.approx(25.0)


def test_stale_instruments_are_reported():
    adapter = _ScriptedAdapter([_tick(1, security_id=SECURITY_ID)])
    feed = _feed(adapter)
    feed.subscribe([SECURITY_ID, OTHER_ID])
    feed.start(lambda _t: None)

    much_later = SESSION_START + timedelta(seconds=600)
    stale = feed.health.stale_instruments(now=much_later, threshold_seconds=120)
    assert stale == (SECURITY_ID,)


def test_a_never_seen_instrument_is_not_reported_as_stale():
    """Conflating "quiet" with "never subscribed" hides the worse problem."""
    adapter = _ScriptedAdapter([_tick(1, security_id=SECURITY_ID)])
    feed = _feed(adapter)
    feed.subscribe([SECURITY_ID, OTHER_ID])
    feed.start(lambda _t: None)

    stale = feed.health.stale_instruments(
        now=SESSION_START + timedelta(seconds=600), threshold_seconds=120
    )
    assert OTHER_ID not in stale
    assert OTHER_ID not in feed.health.last_tick_at


def test_a_fresh_instrument_is_not_stale():
    adapter = _ScriptedAdapter([_tick(1)])
    feed = _feed(adapter)
    feed.subscribe([SECURITY_ID])
    feed.start(lambda _t: None)

    soon = SESSION_START + timedelta(seconds=30)
    assert feed.health.stale_instruments(now=soon, threshold_seconds=120) == ()


# --------------------------------------------------- feed_events (Phase 7 Part 1)
def test_connected_and_disconnected_events_are_emitted_on_a_reconnect():
    adapter = _ScriptedAdapter([_tick(1)], ConnectionResetError("socket died"), [_tick(61)])
    events: list[FeedHealthEvent] = []
    feed = _feed(adapter, on_health_event=events.append)
    feed.subscribe([SECURITY_ID])

    feed.start(lambda _t: None)

    names = [e.event for e in events]
    assert "connected" in names
    assert "disconnected" in names
    disconnected = next(e for e in events if e.event == "disconnected")
    assert disconnected.reason is not None


def test_reconnect_attempted_and_resubscribed_are_emitted():
    adapter = _ScriptedAdapter([_tick(1)], ConnectionResetError("drop"), [])
    events: list[FeedHealthEvent] = []
    feed = _feed(adapter, on_health_event=events.append)
    feed.subscribe([SECURITY_ID, OTHER_ID])

    feed.start(lambda _t: None)

    attempted = [e for e in events if e.event == "reconnect_attempted"]
    assert len(attempted) == 1
    assert attempted[0].attempt == 1
    resubscribed = [e for e in events if e.event == "resubscribed"]
    assert len(resubscribed) == 1
    assert resubscribed[0].expected_subscriptions == 2
    assert resubscribed[0].active_subscriptions == 2


def test_reconnect_exhausted_is_emitted_when_the_budget_runs_out():
    adapter = _ScriptedAdapter(*[ConnectionResetError("nope") for _ in range(10)])
    events: list[FeedHealthEvent] = []
    feed = _feed(
        adapter, on_health_event=events.append, policy=ReconnectPolicy(max_attempts=3)
    )
    feed.subscribe([SECURITY_ID])

    with pytest.raises(FeedUnavailableError):
        feed.start(lambda _t: None)

    exhausted = [e for e in events if e.event == "reconnect_exhausted"]
    assert len(exhausted) == 1
    assert exhausted[0].attempt == 3


def test_recovered_is_emitted_only_on_the_tick_that_clears_degraded():
    adapter = _ScriptedAdapter([_tick(1)], ConnectionResetError("drop"), [_tick(61), _tick(62)])
    events: list[FeedHealthEvent] = []
    feed = _feed(adapter, on_health_event=events.append)
    feed.subscribe([SECURITY_ID])

    feed.start(lambda _t: None)

    recovered = [e for e in events if e.event == "recovered"]
    # Once for the first connection's first tick, once for the reconnect's.
    assert len(recovered) == 2
    assert recovered[-1].security_id == SECURITY_ID


def test_a_raising_health_event_sink_does_not_break_the_feed():
    """The same isolation SafeNotifier gives Telegram: a failing sink must
    never be able to stop trading by taking the feed down with it."""

    def _exploding(_event: FeedHealthEvent) -> None:
        raise RuntimeError("database is down")

    adapter = _ScriptedAdapter([_tick(1)])
    feed = _feed(adapter, on_health_event=_exploding)
    feed.subscribe([SECURITY_ID])

    received: list[Tick] = []
    feed.start(received.append)

    assert len(received) == 1


def test_the_sink_can_be_replaced_after_construction():
    """The supervisor's repository does not exist at __init__ time; the sink
    must be settable later, before the feed thread starts."""
    adapter = _ScriptedAdapter([_tick(1)])
    feed = _feed(adapter)
    feed.subscribe([SECURITY_ID])

    events: list[FeedHealthEvent] = []
    feed.set_health_event_sink(events.append)
    feed.start(lambda _t: None)

    assert any(e.event == "connected" for e in events)


# ------------------------------------------- candle integrity across the gap
def test_a_gap_within_one_interval_discards_that_bar():
    """The corrupt-bar case.

    Ticks at :05 and :50 with the feed down between them would otherwise produce
    one 09:15 bar whose high, low and volume are missing the whole middle of the
    minute — a bar that looks entirely legitimate downstream.
    """
    aggregator = CandleAggregator(interval_seconds=60)
    assert aggregator.add(_tick(5, price=100.0)) is None
    assert aggregator.add(_tick(20, price=105.0)) is None

    aggregator.mark_feed_gap(SECURITY_ID)

    assert aggregator.add(_tick(50, price=101.0)) is None
    completed = aggregator.add(_tick(65, price=102.0))  # closes the 09:15 bar

    assert completed is None, "a bar spanning the gap must not be published"
    assert aggregator.dropped_gap_candles == 1


def test_a_gap_spanning_several_intervals_publishes_nothing_for_them():
    aggregator = CandleAggregator(interval_seconds=60)
    aggregator.add(_tick(5, price=100.0))
    aggregator.mark_feed_gap(SECURITY_ID)

    # Feed returns four minutes later. Intervals 09:16-09:18 saw no ticks at all.
    assert aggregator.add(_tick(245, price=110.0)) is None
    assert aggregator.dropped_gap_candles == 1

    completed = aggregator.add(_tick(305, price=111.0))
    assert completed is not None, "the first clean post-gap bar must publish"
    assert completed.start_at == SESSION_START + timedelta(seconds=240)
    assert completed.open == 110.0


def test_no_duplicate_bar_is_published_for_the_interval_that_spanned_the_gap():
    """Duplicate suppression must survive the reconnect.

    This is why the hub keeps the same aggregator instance: its record of what it
    has already published is the mechanism, and a fresh aggregator would forget.
    """
    aggregator = CandleAggregator(interval_seconds=60)
    aggregator.add(_tick(5))
    aggregator.mark_feed_gap(SECURITY_ID)
    aggregator.add(_tick(50))
    assert aggregator.add(_tick(65)) is None  # 09:15 dropped, 09:16 opens

    # A late tick for the dropped interval must not resurrect and publish it.
    assert aggregator.add(_tick(58)) is None
    assert aggregator.rejected_late == 1
    assert aggregator.dropped_gap_candles == 1


def test_a_clean_interval_after_a_gap_is_unaffected():
    aggregator = CandleAggregator(interval_seconds=60)
    aggregator.add(_tick(5))
    aggregator.mark_feed_gap(SECURITY_ID)
    aggregator.add(_tick(30))
    aggregator.add(_tick(65, price=200.0))  # drops 09:15, opens 09:16
    aggregator.add(_tick(80, price=205.0))

    completed = aggregator.add(_tick(125, price=201.0))
    assert completed is not None
    assert completed.open == 200.0
    assert completed.high == 205.0
    assert aggregator.dropped_gap_candles == 1


def test_flushing_a_gap_marked_bar_yields_nothing():
    aggregator = CandleAggregator(interval_seconds=60)
    aggregator.add(_tick(5))
    aggregator.mark_feed_gap(SECURITY_ID)

    assert list(aggregator.flush()) == []
    assert aggregator.dropped_gap_candles == 1


def test_marking_a_gap_with_no_open_bar_is_harmless():
    aggregator = CandleAggregator(interval_seconds=60)
    assert aggregator.mark_feed_gap() == 0
    assert aggregator.dropped_gap_candles == 0


def test_marking_a_gap_affects_every_instrument_by_default():
    aggregator = CandleAggregator(interval_seconds=60)
    aggregator.add(_tick(5, security_id=SECURITY_ID))
    aggregator.add(_tick(5, security_id=OTHER_ID))

    assert aggregator.mark_feed_gap() == 2

    assert aggregator.add(_tick(65, security_id=SECURITY_ID)) is None
    assert aggregator.add(_tick(65, security_id=OTHER_ID)) is None
    assert aggregator.dropped_gap_candles == 2


# ------------------------------------------------------------- hub integration
def test_the_hub_marks_every_aggregator_on_a_feed_gap():
    adapter = _ScriptedAdapter([_tick(5), _tick(5, security_id=OTHER_ID)])
    hub = SharedFeedHub(adapter, interval_seconds=60)
    hub.register(build_channel("w1", [SECURITY_ID, OTHER_ID], in_process=True))
    hub.start()

    assert hub.mark_feed_gap() == 2
    assert hub.gap_candles_discarded == 2


def test_the_hub_publishes_no_candle_for_a_gapped_interval():
    channel = build_channel("w1", [SECURITY_ID], in_process=True)
    adapter = _ScriptedAdapter([_tick(5), _tick(30)])
    hub = SharedFeedHub(adapter, interval_seconds=60)
    hub.register(channel)
    hub.start()

    hub.mark_feed_gap()
    hub.on_tick(_tick(50))
    hub.on_tick(_tick(65))  # would have closed the 09:15 bar

    assert hub.candle_count == 0
    assert channel.queue.depth() == 0


def test_the_reconnecting_feed_reports_gap_discards_in_health():
    adapter = _ScriptedAdapter([_tick(5)], ConnectionResetError("drop"), [])
    hub = SharedFeedHub(adapter, interval_seconds=60)
    hub.register(build_channel("w1", [SECURITY_ID], in_process=True))
    feed = _feed(adapter, on_feed_gap=hub.mark_feed_gap)
    feed.subscribe([SECURITY_ID])

    feed.start(hub.on_tick)

    assert feed.health.gap_candles_discarded == 1
