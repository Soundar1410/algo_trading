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


def _session(*, start="09:15", end="15:15", square_off="15:20", holidays=()) -> MarketSession:
    return MarketSession(
        SessionConfig(start_time=start, end_time=end, square_off_time=square_off, holidays=holidays)
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
    candles = [
        _candle(datetime(2026, 8, 3, 9, 15, tzinfo=UTC)),
        _candle(datetime(2026, 8, 3, 9, 20, tzinfo=UTC)),
        _candle(datetime(2026, 8, 3, 9, 25, tzinfo=UTC)),
    ]
    fetch = _CallCountingFetch(result=candles)
    manager = WarmupManager(fetch)
    spec = StrategyWarmupSpec(min_bars=3)
    seen: list[Candle] = []
    result = manager.warm(seen.append, _SOURCE, spec, session=_session(), timeframe_minutes=5)
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


def test_lookback_sessions_is_capped_by_max_lookback_sessions() -> None:
    manager = WarmupManager(lambda *a, **k: [], max_lookback_sessions=2)
    spec = StrategyWarmupSpec(min_bars=1000)
    assert manager._lookback_sessions(spec, _session(), 5) == 2
