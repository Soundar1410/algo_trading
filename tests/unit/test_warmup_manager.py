"""Phase 4 Part 4. :class:`~common.warmup.manager.WarmupManager` — the fetch
+ replay engine, exercised with a synthetic ``fetch_fn``/``sink``, no network.

Two of these are fail-first demonstrations, run against the pre-fix shape
before being confirmed against the corrected code, per CLAUDE.md's
not-weakening-tests rule and this project's established convention (see the
runbook's Phase 3/4 "fail-first" evidence sections):

* ``test_successful_replay_reports_the_last_candles_start_at`` would raise
  ``AttributeError`` if the reference's ``candles[-1].start`` field name were
  carried over unchanged, since this repository's frozen ``Candle`` has no
  ``.start`` attribute.
* ``test_fetch_exception_degrades_to_cold_start_never_propagates`` would fail
  against an accidentally-narrowed ``except`` clause (e.g. catching only
  ``ConnectionError``), which is exactly the class of regression the broad
  ``except Exception`` exists to guard against.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from common.engine.config import SessionConfig
from common.engine.session import MarketSession
from common.models import Candle
from common.warmup.manager import WarmupManager, WarmupResult
from common.warmup.requirements import StrategyWarmupSpec
from common.warmup.source import WarmupSource


def _session(
    *,
    start="09:15",
    end="15:15",
    square_off="15:20",
    holidays=(),
    timezone="Asia/Kolkata",
) -> MarketSession:
    return MarketSession(
        SessionConfig(
            timezone=timezone,
            start_time=start,
            end_time=end,
            square_off_time=square_off,
            holidays=holidays,
        )
    )


def _candle(start_at: datetime, *, close: float = 100.0) -> Candle:
    return Candle(
        security_id="13",
        instrument="NIFTY",
        open=close,
        high=close,
        low=close,
        close=close,
        volume=0,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=5),
        tick_count=0,
        last_tick_at=start_at,
    )


_SOURCE = WarmupSource(security_id="13", exchange_segment="IDX_I", instrument_type="INDEX")


@dataclass
class _CallCountingFetch:
    """A fetch_fn stub that records whether it was ever called."""

    calls: int = 0
    result: list[Candle] | None = None
    exception: Exception | None = None

    def __call__(self, source, *, session, timeframe_minutes, lookback_sessions, now=None):
        self.calls += 1
        if self.exception is not None:
            raise self.exception
        return self.result


def test_skipped_empty_when_spec_is_none() -> None:
    fetch = _CallCountingFetch(result=[])
    manager = WarmupManager(fetch)
    result = manager.warm(lambda c: None, _SOURCE, None, session=_session(), timeframe_minutes=5)
    assert result == WarmupResult("SKIPPED_EMPTY", 0, "no session-spanning indicators to warm")
    assert fetch.calls == 0


def test_skipped_empty_when_min_bars_is_zero() -> None:
    fetch = _CallCountingFetch(result=[])
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=0)
    result = manager.warm(lambda c: None, _SOURCE, spec, session=_session(), timeframe_minutes=5)
    assert result.status == "SKIPPED_EMPTY"
    assert fetch.calls == 0


def test_skipped_session_local() -> None:
    fetch = _CallCountingFetch(result=[_candle(datetime(2026, 8, 3, 9, 15, tzinfo=UTC))])
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=10, has_session_local=True)
    result = manager.warm(lambda c: None, _SOURCE, spec, session=_session(), timeframe_minutes=5)
    assert result.status == "SKIPPED_SESSION_LOCAL"
    assert fetch.calls == 0


def test_skipped_volume() -> None:
    fetch = _CallCountingFetch(result=[_candle(datetime(2026, 8, 3, 9, 15, tzinfo=UTC))])
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=10, requires_volume=True)
    result = manager.warm(lambda c: None, _SOURCE, spec, session=_session(), timeframe_minutes=5)
    assert result.status == "SKIPPED_VOLUME"
    assert fetch.calls == 0


def test_fetch_exception_degrades_to_cold_start_never_propagates() -> None:
    fetch = _CallCountingFetch(exception=RuntimeError("network exploded"))
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=10)
    # Must not raise -- warm-up must never break trading.
    result = manager.warm(lambda c: None, _SOURCE, spec, session=_session(), timeframe_minutes=5)
    assert result.status == "COLD_START"
    assert "network exploded" in result.detail
    assert result.candles_replayed == 0


def test_empty_fetch_result_is_cold_start() -> None:
    fetch = _CallCountingFetch(result=[])
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=10)
    result = manager.warm(lambda c: None, _SOURCE, spec, session=_session(), timeframe_minutes=5)
    assert result.status == "COLD_START"


def test_none_fetch_result_is_cold_start() -> None:
    fetch = _CallCountingFetch(result=None)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=10)
    result = manager.warm(lambda c: None, _SOURCE, spec, session=_session(), timeframe_minutes=5)
    assert result.status == "COLD_START"


def test_successful_replay_reports_the_last_candles_start_at() -> None:
    # 2026-08-03 is a real Monday (verified) -- a UTC-timezone session keeps
    # the arithmetic trivial (no IST-offset reasoning) while still exercising
    # the real completeness check: these 3 candles are exactly the 3 most
    # recent completed 5-minute buckets as of `now`, so this stays WARMED.
    candles = [
        _candle(datetime(2026, 8, 3, 9, 15, tzinfo=UTC)),
        _candle(datetime(2026, 8, 3, 9, 20, tzinfo=UTC)),
        _candle(datetime(2026, 8, 3, 9, 25, tzinfo=UTC)),
    ]
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=3)
    seen: list[Candle] = []
    now = datetime(2026, 8, 3, 9, 32, tzinfo=UTC)
    result = manager.warm(
        seen.append, _SOURCE, spec, session=_session(timezone="UTC"), timeframe_minutes=5, now=now
    )
    assert result.status == "WARMED"
    assert result.candles_replayed == 3
    assert seen == candles
    assert "2026-08-03 09:25" in result.detail


def test_sink_exception_yields_partial_with_replayed_count() -> None:
    candles = [
        _candle(datetime(2026, 8, 3, 9, 15, tzinfo=UTC)),
        _candle(datetime(2026, 8, 3, 9, 20, tzinfo=UTC)),
        _candle(datetime(2026, 8, 3, 9, 25, tzinfo=UTC)),
        _candle(datetime(2026, 8, 3, 9, 30, tzinfo=UTC)),
        _candle(datetime(2026, 8, 3, 9, 35, tzinfo=UTC)),
    ]
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=5)

    seen: list[Candle] = []

    def _sink(c: Candle) -> None:
        seen.append(c)
        if len(seen) == 3:
            raise ValueError("boom")

    result = manager.warm(_sink, _SOURCE, spec, session=_session(), timeframe_minutes=5)
    assert result.status == "PARTIAL"
    assert result.candles_replayed == 2
    assert len(seen) == 3  # the third call happened; it just raised
    assert "boom" in result.detail


def test_fetch_fn_receives_the_expected_keyword_arguments() -> None:
    received: dict[str, object] = {}

    def _fetch(source, *, session, timeframe_minutes, lookback_sessions, now=None):
        received.update(
            source=source,
            session=session,
            timeframe_minutes=timeframe_minutes,
            lookback_sessions=lookback_sessions,
            now=now,
        )
        return []

    manager = WarmupManager(_fetch)
    spec = StrategyWarmupSpec(min_bars=10)
    session = _session()
    now = datetime(2026, 8, 3, 9, 30, tzinfo=UTC)
    manager.warm(lambda c: None, _SOURCE, spec, session=session, timeframe_minutes=5, now=now)
    assert received["source"] is _SOURCE
    assert received["session"] is session
    assert received["timeframe_minutes"] == 5
    assert received["lookback_sessions"] >= 1
    assert received["now"] is now


@pytest.mark.parametrize(
    ("min_bars", "expected_lookback"),
    [
        (1, 1),
        # per_session is measured against session.square_off (15:20), not
        # session.end (15:15) -- the manager's own choice, ported unchanged --
        # so a 09:15-15:20 window at 5m is (920-555)//5 = 73 bars/session.
        (73, 1),
        (74, 2),
        (146, 2),
        (147, 3),
    ],
)
def test_lookback_sessions_scales_with_min_bars(min_bars: int, expected_lookback: int) -> None:
    fetch = _CallCountingFetch(result=[])
    manager = WarmupManager(fetch, max_lookback_sessions=5)
    spec = StrategyWarmupSpec(min_bars=min_bars)
    manager.warm(lambda c: None, _SOURCE, spec, session=_session(), timeframe_minutes=5)
    # Recover the lookback the manager computed by inspecting what it passed in.
    assert manager._lookback_sessions(spec, _session(), 5) == expected_lookback


# ----------------------------------------------------------------------------
# Completeness verification (Rev 3.1 implementation-gap fix). `WARMED` must
# require genuinely valid, recent, ordered, duplicate-free coverage -- not
# merely "fetch+replay didn't raise". All of these use a UTC-timezone session
# so bucket arithmetic needs no IST-offset reasoning; every date below was
# verified against the real 2026 calendar (2026-08-03 is a Monday,
# 2026-07-31 the Friday before it, 2026-08-04/05 the following Tue/Wed).
#
# Base scenario for the PARTIAL family (cases 1-5): Monday 2026-08-03,
# 5-minute timeframe, min_bars=5, now=09:47:00 -- current_bucket=09:45, so
# required_latest=09:40 and the required suffix is exactly
# [09:15, 09:20, 09:25, 09:30, 09:35, 09:40][-5:] == [09:20,09:25,09:30,09:35,09:40].
def _utc(hour: int, minute: int, *, day: int = 3, month: int = 8) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def _partial_case(candles: list[Candle]) -> WarmupResult:
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=5)
    return manager.warm(
        lambda c: None,
        _SOURCE,
        spec,
        session=_session(timezone="UTC"),
        timeframe_minutes=5,
        now=_utc(9, 47),
    )


def test_stale_replay_that_does_not_reach_the_required_boundary_is_partial() -> None:
    """Non-empty, ordered, gap-free replay -- but it stops at 09:35, short of
    the 09:40 the handoff time requires."""
    candles = [_candle(_utc(h, m)) for h, m in ((9, 15), (9, 20), (9, 25), (9, 30), (9, 35))]
    result = _partial_case(candles)
    assert result.status == "PARTIAL"
    assert result.status != "WARMED"


def test_fewer_candles_than_min_bars_is_partial() -> None:
    candles = [_candle(_utc(h, m)) for h, m in ((9, 20), (9, 25), (9, 30))]
    result = _partial_case(candles)
    assert result.status == "PARTIAL"
    assert result.status != "WARMED"


def test_a_missing_in_session_bucket_in_the_recent_suffix_is_partial() -> None:
    """Count (5) and final recency (09:40) both look right -- but 09:25 is
    missing from the middle, replaced by an out-of-window 09:15."""
    candles = [_candle(_utc(h, m)) for h, m in ((9, 15), (9, 20), (9, 30), (9, 35), (9, 40))]
    result = _partial_case(candles)
    assert result.status == "PARTIAL"
    assert result.status != "WARMED"


def test_a_duplicate_bucket_in_the_recent_suffix_is_partial() -> None:
    candles = [
        _candle(_utc(h, m)) for h, m in ((9, 20), (9, 20), (9, 25), (9, 30), (9, 35), (9, 40))
    ]
    result = _partial_case(candles)
    assert result.status == "PARTIAL"
    assert result.status != "WARMED"


def test_an_out_of_order_recent_suffix_is_partial() -> None:
    candles = [_candle(_utc(h, m)) for h, m in ((9, 20), (9, 30), (9, 25), (9, 35), (9, 40))]
    result = _partial_case(candles)
    assert result.status == "PARTIAL"
    assert result.status != "WARMED"


def test_a_legitimate_weekend_gap_is_warmed() -> None:
    """Monday 09:22 -- exactly 1 completed bucket today (09:15) -- plus
    min_bars=5 walks back across the weekend to Friday 2026-07-31's last 4
    buckets. max_lookback_sessions=1 (default) is exactly enough: 1 prior
    session beyond the current/boundary day."""
    candles = [
        _candle(_utc(15, 0, day=31, month=7)),
        _candle(_utc(15, 5, day=31, month=7)),
        _candle(_utc(15, 10, day=31, month=7)),
        _candle(_utc(15, 15, day=31, month=7)),
        _candle(_utc(9, 15)),
    ]
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=5)
    result = manager.warm(
        lambda c: None,
        _SOURCE,
        spec,
        session=_session(timezone="UTC"),
        timeframe_minutes=5,
        now=_utc(9, 22),
    )
    assert result.status == "WARMED"


def test_a_legitimate_configured_holiday_gap_is_warmed() -> None:
    """Kept separate from the weekend case. Wednesday 2026-08-05, 09:22 (1
    completed bucket today); Tuesday 2026-08-04 is a configured holiday, so
    min_bars=5 must walk past it to Monday 2026-08-03's last 4 buckets --
    still just 1 *additional* prior session, so max_lookback_sessions=1
    (default) suffices."""
    candles = [
        _candle(_utc(15, 0)),  # Monday 2026-08-03 (default day/month)
        _candle(_utc(15, 5)),
        _candle(_utc(15, 10)),
        _candle(_utc(15, 15)),
        _candle(_utc(9, 15, day=5)),  # Wednesday 2026-08-05
    ]
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=5)
    result = manager.warm(
        lambda c: None,
        _SOURCE,
        spec,
        session=_session(timezone="UTC", holidays=("2026-08-04",)),
        timeframe_minutes=5,
        now=_utc(9, 22, day=5),
    )
    assert result.status == "WARMED"


def test_current_day_plus_exactly_one_prior_session_is_warmed_with_the_default_budget() -> None:
    """Isolates the budget semantics from any gap-skip logic: plain adjacent
    weekdays (Tuesday following Monday directly, no weekend/holiday
    involved). max_lookback_sessions=1 (default) must be enough for
    "current day + one prior session" -- the budget counts prior sessions
    only, not the current/boundary day itself."""
    candles = [
        _candle(_utc(15, 0)),  # Monday 2026-08-03
        _candle(_utc(15, 5)),
        _candle(_utc(15, 10)),
        _candle(_utc(15, 15)),
        _candle(_utc(9, 15, day=4)),  # Tuesday 2026-08-04
    ]
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=5)
    result = manager.warm(
        lambda c: None,
        _SOURCE,
        spec,
        session=_session(timezone="UTC"),
        timeframe_minutes=5,
        now=_utc(9, 22, day=4),
    )
    assert result.status == "WARMED"


def test_valid_market_open_coverage_ending_at_the_previous_sessions_close_is_warmed() -> None:
    """09:10 -- before session.start (09:15) -- so today has 0 completed
    candles; valid coverage legitimately ends at Friday 2026-07-31's final
    completed bucket instead of forcing a cold start at the open."""
    candles = [
        _candle(_utc(15, 5, day=31, month=7)),
        _candle(_utc(15, 10, day=31, month=7)),
        _candle(_utc(15, 15, day=31, month=7)),
    ]
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=3)
    result = manager.warm(
        lambda c: None,
        _SOURCE,
        spec,
        session=_session(timezone="UTC"),
        timeframe_minutes=5,
        now=_utc(9, 10),
    )
    assert result.status == "WARMED"


def test_valid_mid_session_coverage_reaching_todays_latest_completed_bucket_is_warmed() -> None:
    candles = [_candle(_utc(h, m)) for h, m in ((9, 15), (9, 20), (9, 25), (9, 30))]
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=4)
    result = manager.warm(
        lambda c: None,
        _SOURCE,
        spec,
        session=_session(timezone="UTC"),
        timeframe_minutes=5,
        now=_utc(9, 37),
    )
    assert result.status == "WARMED"


def test_post_market_startup_caps_the_required_boundary_at_todays_final_bucket() -> None:
    """16:00 -- well after square_off (15:20) -- must never expect an
    out-of-session bucket like 15:55. The required boundary is capped at
    today's actual final session bucket (15:15); without the cap this would
    be permanently unsatisfiable, since no real candle could ever match a
    timestamp that was never a valid bucket."""
    candles = [_candle(_utc(h, m)) for h, m in ((15, 5), (15, 10), (15, 15))]
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=3)
    result = manager.warm(
        lambda c: None,
        _SOURCE,
        spec,
        session=_session(timezone="UTC"),
        timeframe_minutes=5,
        now=_utc(16, 0),
    )
    assert result.status == "WARMED"


def test_a_gap_outside_the_required_suffix_is_no_longer_ignored() -> None:
    """Correction (Rev 3.1 follow-up): this test previously asserted WARMED
    here, on the theory that only the final min_bars window is inspected.
    That was the bug -- the extra Friday candles (09:15 then 09:30, skipping
    09:20/09:25) still get replayed into the indicator even though they're
    outside the required window, so a gap among them must not be silently
    ignored when deciding whether the result is trusted. min_bars=3 only
    NEEDS today's own [09:15,09:20,09:25], but the whole replayed sequence
    is now validated, not just what's strictly required."""
    candles = [
        _candle(_utc(9, 15, day=31, month=7)),
        _candle(_utc(9, 30, day=31, month=7)),  # gap: skips 09:20/09:25 that Friday
        _candle(_utc(9, 15)),
        _candle(_utc(9, 20)),
        _candle(_utc(9, 25)),
    ]
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=3)
    result = manager.warm(
        lambda c: None,
        _SOURCE,
        spec,
        session=_session(timezone="UTC"),
        timeframe_minutes=5,
        now=_utc(9, 32),
    )
    assert result.status == "PARTIAL"
    assert result.status != "WARMED"
    # The candles were still replayed (indicator state isn't un-fed) -- only
    # the trust decision changed.
    assert result.candles_replayed == len(candles)


def test_a_duplicate_outside_the_required_suffix_is_not_warmed() -> None:
    """A duplicate earlier in today's own session, before the strictly
    required min_bars=3 window, must still be caught."""
    candles = [
        _candle(_utc(9, 15)),
        _candle(_utc(9, 15)),  # duplicate, outside the required [09:35,09:40,09:45] window
        _candle(_utc(9, 20)),
        _candle(_utc(9, 25)),
        _candle(_utc(9, 30)),
        _candle(_utc(9, 35)),
        _candle(_utc(9, 40)),
        _candle(_utc(9, 45)),
    ]
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=3)
    result = manager.warm(
        lambda c: None,
        _SOURCE,
        spec,
        session=_session(timezone="UTC"),
        timeframe_minutes=5,
        now=_utc(9, 52),
    )
    assert result.status == "PARTIAL"


def test_out_of_order_candles_outside_the_required_suffix_are_not_warmed() -> None:
    """09:20 and 09:15 swapped, well before the strictly required
    min_bars=3 window ([09:35,09:40,09:45]) -- still caught."""
    candles = [
        _candle(_utc(9, 20)),
        _candle(_utc(9, 15)),
        _candle(_utc(9, 25)),
        _candle(_utc(9, 30)),
        _candle(_utc(9, 35)),
        _candle(_utc(9, 40)),
        _candle(_utc(9, 45)),
    ]
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=3)
    result = manager.warm(
        lambda c: None,
        _SOURCE,
        spec,
        session=_session(timezone="UTC"),
        timeframe_minutes=5,
        now=_utc(9, 52),
    )
    assert result.status == "PARTIAL"


def test_a_gap_earlier_in_todays_own_session_is_not_warmed_even_with_a_complete_tail() -> None:
    """A provider that omits part of the CURRENT session (09:20-09:30 is
    missing) while still returning a complete, contiguous recent suffix
    ([09:35,09:40,09:45], satisfying min_bars=3 on its own) must still be
    caught -- the gap earlier in today's session was still replayed into
    the indicator."""
    candles = [
        _candle(_utc(9, 15)),
        # gap: 09:20, 09:25, 09:30 missing
        _candle(_utc(9, 35)),
        _candle(_utc(9, 40)),
        _candle(_utc(9, 45)),
    ]
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=3)
    result = manager.warm(
        lambda c: None,
        _SOURCE,
        spec,
        session=_session(timezone="UTC"),
        timeframe_minutes=5,
        now=_utc(9, 52),
    )
    assert result.status == "PARTIAL"


def test_a_valid_1_hour_replay_uses_the_production_grid_and_is_warmed() -> None:
    """1-hour timeframe on a 09:15-15:20 session: the production
    (floor_to_interval) grid's real bucket starts are 10:00, 11:00, ... --
    not the old session.start-anchored 09:15, 10:15, .... A replay using
    the real grid must be able to reach WARMED."""
    candles = [_candle(_utc(h, 0)) for h in (10, 11, 12)]
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=3)
    result = manager.warm(
        lambda c: None,
        _SOURCE,
        spec,
        session=_session(timezone="UTC"),
        timeframe_minutes=60,
        now=_utc(13, 5),
    )
    assert result.status == "WARMED"


def test_a_terminal_bucket_extending_past_square_off_is_never_warmed() -> None:
    """15-minute timeframe, square_off=15:20: a candle starting at 15:15
    would extend to 15:30, past the configured close -- production
    aggregation could never actually produce it. Its presence must never
    let a WARMED result through, even though 15:15 < 15:20 (the old,
    start-only check would have accepted it)."""
    candles = [_candle(_utc(h, m)) for h, m in ((14, 30), (14, 45), (15, 0), (15, 15))]
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=4)
    result = manager.warm(
        lambda c: None,
        _SOURCE,
        spec,
        session=_session(timezone="UTC"),
        timeframe_minutes=15,
        now=_utc(15, 22),
    )
    assert result.status != "WARMED"
    assert result.status == "PARTIAL"


def test_real_historical_aggregation_output_agrees_with_the_managers_expectations() -> None:
    """Not hand-picked timestamps: candles produced by the actual
    aggregate_candles() + the shared is_applicable_session_bucket filter
    (the same pipeline fetch_warmup_candles_range uses) must be accepted as
    WARMED by the manager -- proving the two paths haven't drifted apart."""
    from common.warmup.historical import aggregate_candles
    from common.warmup.session_buckets import is_applicable_session_bucket

    session = _session(timezone="UTC")
    now = _utc(9, 47)
    # Raw 1-minute candles spanning 09:15-09:45 (30 of them), as a real
    # intraday fetch would return before aggregation.
    one_min = [_candle(_utc(9, 15) + timedelta(minutes=i), close=100.0 + i) for i in range(30)]
    aggregated = aggregate_candles(one_min, 5, security_id="13", instrument="NIFTY")
    historical_candles = [
        c
        for c in aggregated
        if is_applicable_session_bucket(session, c.start_at, 5) and c.start_at < _utc(9, 45)
    ]

    fetch = _CallCountingFetch(result=historical_candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=6)
    result = manager.warm(
        lambda c: None, _SOURCE, spec, session=session, timeframe_minutes=5, now=now
    )
    assert result.status == "WARMED"


def test_missing_now_is_never_warmed_even_with_otherwise_complete_candles() -> None:
    """Reuses the mid-session-valid case's candles/spec but omits `now` --
    must not return WARMED, even though replay still happens (indicators
    still get warmed; only trust is withheld)."""
    candles = [_candle(_utc(h, m)) for h, m in ((9, 15), (9, 20), (9, 25), (9, 30))]
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=4)
    result = manager.warm(
        lambda c: None, _SOURCE, spec, session=_session(timezone="UTC"), timeframe_minutes=5
    )
    assert result.status != "WARMED"
    assert result.status == "PARTIAL"
    assert result.candles_replayed == len(candles)


def test_lookback_sessions_is_capped_by_max_lookback_sessions() -> None:
    manager = WarmupManager(lambda *a, **k: [], max_lookback_sessions=2)
    spec = StrategyWarmupSpec(min_bars=1000)
    assert manager._lookback_sessions(spec, _session(), 5) == 2
