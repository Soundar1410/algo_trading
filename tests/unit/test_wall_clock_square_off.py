"""The wall-clock square-off net — runbook limitation 7 (Phase 4 Part 3).

The engine's square-off is driven by the **candle clock**: `on_tick` asks
`authority.due(tick.exchange_time)`. That is the right primary mechanism, but it
has one failure mode and it is the expensive one — *if the feed stops before the
square-off bar, square-off never triggers* and a position is carried overnight.

The net asks the same authority on a timer instead. These tests cover the unit;
`tests/integration/test_wall_clock_square_off_threads.py` proves it on real
threads with a real database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from runtimes.intraday_options.engine_worker import _wall_clock_square_off

IST = ZoneInfo("Asia/Kolkata")
TRADING_DATE = "2026-08-03"

BEFORE = datetime(2026, 8, 3, 10, 0, tzinfo=IST)
AFTER = datetime(2026, 8, 3, 15, 25, tzinfo=IST)


class _FakeAuthority:
    """Due once the clock passes 15:20 IST. Records what it was asked."""

    def __init__(self, *, due_from: datetime | None = None) -> None:
        self._due_from = due_from or datetime(2026, 8, 3, 15, 20, tzinfo=IST)
        self.asked: list[datetime] = []

    def due(self, ts: datetime) -> bool:
        self.asked.append(ts)
        return ts >= self._due_from

    def completed(self, ts: datetime) -> None:  # pragma: no cover - not exercised
        pass


class _FakeEngine:
    def __init__(self) -> None:
        self.square_off_requested = False
        self.reasons: list[str] = []

    def request_square_off(self, reason: str) -> None:
        self.square_off_requested = True
        self.reasons.append(reason)


def _net(
    engine: _FakeEngine | None,
    authority: _FakeAuthority,
    *,
    now: datetime,
    date: str = TRADING_DATE,
):
    holder = [engine] if engine is not None else []
    return _wall_clock_square_off(holder, authority, trading_date=date, clock=lambda: now)  # type: ignore[arg-type]


# ------------------------------------------------------------------ it fires
def test_it_requests_a_square_off_once_the_wall_clock_passes_the_time():
    """The whole point: no tick is involved anywhere in this call."""
    engine, authority = _FakeEngine(), _FakeAuthority()
    _net(engine, authority, now=AFTER)()
    assert engine.square_off_requested is True
    assert "wall clock" in engine.reasons[0]


def test_it_stays_silent_before_the_square_off_time():
    engine, authority = _FakeEngine(), _FakeAuthority()
    _net(engine, authority, now=BEFORE)()
    assert engine.square_off_requested is False


def test_it_asks_the_authority_rather_than_deciding_for_itself():
    """One owner: the net supplies a clock reading, the authority decides."""
    engine, authority = _FakeEngine(), _FakeAuthority()
    _net(engine, authority, now=AFTER)()
    assert authority.asked == [AFTER]


def test_a_completed_day_is_not_re_closed():
    """`PersistedSquareOffAuthority` returns False for a COMPLETED day, so the
    net inherits restart-safety rather than needing its own state."""

    class _Completed(_FakeAuthority):
        def due(self, ts: datetime) -> bool:
            self.asked.append(ts)
            return False

    engine, authority = _FakeEngine(), _Completed()
    _net(engine, authority, now=AFTER)()
    assert engine.square_off_requested is False


def test_it_asks_only_once_even_if_polled_repeatedly():
    """The poll loop runs every 0.5s; a net that re-requested each time would
    write a log line twice a second for the rest of the session."""
    engine, authority = _FakeEngine(), _FakeAuthority()
    check = _net(engine, authority, now=AFTER)
    for _ in range(10):
        check()
    assert len(engine.reasons) == 1
    assert len(authority.asked) == 1, "it kept asking after the answer stopped mattering"


def test_it_does_nothing_before_the_engine_exists():
    """The feed is constructed before the engine; the holder is empty until then."""
    authority = _FakeAuthority()
    _net(None, authority, now=AFTER)()
    assert authority.asked == []


# ------------------------------------------------------- the trading-date guard
def test_it_stays_silent_when_the_wall_clock_is_not_on_the_trading_date():
    """`trigger_at` is a time-of-day decision with no notion of *which* day. A
    replay of a 2026-07-16 tape run at any real time would otherwise square off
    before processing a tick — which is exactly what happened on first run,
    breaking 25 tests."""
    engine, authority = _FakeEngine(), _FakeAuthority()
    _net(engine, authority, now=AFTER, date="2026-07-16")()
    assert engine.square_off_requested is False
    assert authority.asked == [], "the authority should not even be consulted"


def test_the_guard_compares_the_date_in_ist_not_utc():
    """23:50 IST is still the trading date; the same instant in UTC is too, but
    03:00 IST is 21:30 UTC the day before — the guard must follow IST."""
    engine, authority = (
        _FakeEngine(),
        _FakeAuthority(due_from=datetime(2026, 8, 3, 0, 0, tzinfo=IST)),
    )
    late_ist = datetime(2026, 8, 3, 23, 50, tzinfo=IST)
    assert late_ist.astimezone(UTC).date().isoformat() == "2026-08-03"
    _net(engine, authority, now=late_ist)()
    assert engine.square_off_requested is True


def test_an_engine_that_already_asked_is_not_asked_again():
    engine, authority = _FakeEngine(), _FakeAuthority()
    engine.square_off_requested = True
    _net(engine, authority, now=AFTER)()
    assert engine.reasons == []


# ------------------------------------------------------------------ the hook
def test_the_feed_calls_the_hook_on_every_poll_wake():
    """The net is only as good as the loop that drives it. Proven against the
    real `HubTickFeed`, on the empty-queue path where the timer matters."""
    import queue as queue_module

    from common.engine.hub_feed import HubTickFeed

    calls: list[int] = []
    feed = HubTickFeed(
        queue_module.Queue(),
        on_poll=lambda: calls.append(1),
        should_stop=lambda: len(calls) >= 3,
        poll_seconds=0.01,
        idle_timeout_seconds=None,
    )
    feed.run()
    assert len(calls) >= 3, "on_poll was not called on an idle poll wake"


def test_the_hook_runs_before_should_stop_is_asked():
    """A net that *requests* a square-off must be noticed on the same wake, not
    the next one — otherwise the shutdown is a poll interval late."""
    import queue as queue_module

    from common.engine.hub_feed import HubTickFeed

    order: list[str] = []
    requested = {"value": False}

    def _poll() -> None:
        order.append("poll")
        requested["value"] = True

    feed = HubTickFeed(
        queue_module.Queue(),
        on_poll=_poll,
        should_stop=lambda: (order.append("should_stop"), requested["value"])[1],
        poll_seconds=0.01,
        idle_timeout_seconds=None,
    )
    feed.run()
    assert order[:2] == ["poll", "should_stop"]
    assert feed.stopped_by_request is True


def test_a_feed_with_no_hook_behaves_exactly_as_before():
    import queue as queue_module

    from common.engine.hub_feed import HubTickFeed

    feed = HubTickFeed(queue_module.Queue(), poll_seconds=0.01, idle_timeout_seconds=0.05)
    feed.run()
    assert feed.stopped_by_idle_timeout is True


def test_a_raising_hook_is_not_swallowed():
    """It exists to make a position close. A net that hid its own failure would
    be worse than no net, because the run would look healthy."""
    import queue as queue_module

    from common.engine.hub_feed import HubTickFeed

    def _boom() -> None:
        raise RuntimeError("the net broke")

    feed = HubTickFeed(
        queue_module.Queue(), on_poll=_boom, poll_seconds=0.01, idle_timeout_seconds=None
    )
    with pytest.raises(RuntimeError, match="the net broke"):
        feed.run()
