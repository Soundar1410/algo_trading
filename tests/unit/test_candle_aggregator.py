"""Candle aggregation: completeness, determinism and the no-rewrite rule."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.candles import CandleAggregator, SessionWindow, floor_to_interval
from common.models import Tick

IST = ZoneInfo("Asia/Kolkata")


def _tick(minute: int, second: int, price: float, *, security_id: str = "S1") -> Tick:
    moment = datetime(2026, 7, 29, 9, minute, second, tzinfo=IST)
    return Tick(
        security_id=security_id,
        instrument="NIFTY",
        last_price=price,
        exchange_time=moment,
        received_at=moment,
        last_quantity=10,
    )


# ------------------------------------------------------------- flooring
def test_flooring_uses_local_wall_clock_not_utc_epoch():
    """India is UTC+05:30, so epoch flooring would put bars on :30 boundaries."""
    moment = datetime(2026, 7, 29, 9, 16, 42, tzinfo=IST)
    assert floor_to_interval(moment, 60) == datetime(2026, 7, 29, 9, 16, 0, tzinfo=IST)


def test_flooring_rejects_a_non_positive_interval():
    with pytest.raises(ValueError, match="must be positive"):
        floor_to_interval(datetime(2026, 7, 29, 9, 15, tzinfo=IST), 0)


# ------------------------------------------------------------ completion
def test_no_candle_is_published_before_the_interval_closes():
    aggregator = CandleAggregator(interval_seconds=60)
    assert aggregator.add(_tick(15, 0, 100.0)) is None
    assert aggregator.add(_tick(15, 30, 101.0)) is None


def test_crossing_the_boundary_publishes_the_completed_bar():
    aggregator = CandleAggregator(interval_seconds=60)
    aggregator.add(_tick(15, 0, 100.0))
    aggregator.add(_tick(15, 30, 102.0))
    aggregator.add(_tick(15, 45, 99.0))

    candle = aggregator.add(_tick(16, 1, 105.0))

    assert candle is not None
    assert (candle.open, candle.high, candle.low, candle.close) == (100.0, 102.0, 99.0, 99.0)
    assert candle.start_at == datetime(2026, 7, 29, 9, 15, tzinfo=IST)
    assert candle.end_at == datetime(2026, 7, 29, 9, 16, tzinfo=IST)
    assert candle.tick_count == 3


def test_the_completing_tick_belongs_to_the_next_bar_not_the_published_one():
    """105.0 opened the 09:16 bar; it must not appear in the 09:15 bar's high."""
    aggregator = CandleAggregator(interval_seconds=60)
    aggregator.add(_tick(15, 0, 100.0))
    candle = aggregator.add(_tick(16, 1, 105.0))

    assert candle is not None
    assert candle.high == 100.0

    next_candle = aggregator.add(_tick(17, 1, 90.0))
    assert next_candle is not None
    assert next_candle.open == 105.0


def test_volume_accumulates_across_the_bar():
    aggregator = CandleAggregator(interval_seconds=60)
    aggregator.add(_tick(15, 0, 100.0))
    aggregator.add(_tick(15, 30, 100.0))
    candle = aggregator.add(_tick(16, 1, 100.0))
    assert candle is not None
    assert candle.volume == 20


# ------------------------------------------------------- the no-rewrite rule
def test_a_late_tick_cannot_modify_an_already_published_candle():
    """The spec forbids using a future tick to change a published bar."""
    aggregator = CandleAggregator(interval_seconds=60)
    aggregator.add(_tick(15, 0, 100.0))
    published = aggregator.add(_tick(16, 1, 105.0))
    assert published is not None

    assert aggregator.add(_tick(15, 59, 999.0)) is None
    assert aggregator.rejected_late == 1

    later = aggregator.add(_tick(17, 1, 106.0))
    assert later is not None
    assert later.high == 105.0  # the 999.0 never entered any bar


def test_duplicate_publication_of_one_bucket_is_impossible():
    aggregator = CandleAggregator(interval_seconds=60)
    aggregator.add(_tick(15, 0, 100.0))
    aggregator.add(_tick(16, 1, 101.0))

    # Force the internal replay path a crash could otherwise expose.
    with pytest.raises(RuntimeError, match="already published"):
        aggregator._complete(
            aggregator._new_bar(
                _tick(15, 0, 100.0),
                datetime(2026, 7, 29, 9, 15, tzinfo=IST),
                datetime(2026, 7, 29, 9, 16, tzinfo=IST),
            )
        )


# ------------------------------------------------------------ session rules
def test_a_pre_open_tick_is_rejected_not_folded_into_the_first_bar():
    aggregator = CandleAggregator(interval_seconds=60)
    early = datetime(2026, 7, 29, 9, 10, tzinfo=IST)
    tick = Tick(
        security_id="S1",
        instrument="NIFTY",
        last_price=95.0,
        exchange_time=early,
        received_at=early,
    )
    assert aggregator.add(tick) is None
    assert aggregator.rejected_out_of_session == 1


def test_a_post_close_tick_is_rejected():
    aggregator = CandleAggregator(interval_seconds=60)
    late = datetime(2026, 7, 29, 15, 31, tzinfo=IST)
    tick = Tick(
        security_id="S1",
        instrument="NIFTY",
        last_price=95.0,
        exchange_time=late,
        received_at=late,
    )
    assert aggregator.add(tick) is None
    assert aggregator.rejected_out_of_session == 1


def test_session_bounds_are_configurable():
    window = SessionWindow(start=time(10, 0), end=time(11, 0))
    assert not window.contains(datetime(2026, 7, 29, 9, 59, tzinfo=IST))
    assert window.contains(datetime(2026, 7, 29, 10, 0, tzinfo=IST))
    assert not window.contains(datetime(2026, 7, 29, 11, 0, tzinfo=IST))


# ------------------------------------------------------- multiple instruments
def test_instruments_are_aggregated_independently():
    aggregator = CandleAggregator(interval_seconds=60)
    aggregator.add(_tick(15, 0, 100.0, security_id="A"))
    aggregator.add(_tick(15, 0, 200.0, security_id="B"))

    candle_a = aggregator.add(_tick(16, 1, 101.0, security_id="A"))
    assert candle_a is not None and candle_a.security_id == "A"
    assert candle_a.open == 100.0

    candle_b = aggregator.add(_tick(16, 1, 201.0, security_id="B"))
    assert candle_b is not None and candle_b.security_id == "B"
    assert candle_b.open == 200.0


def test_flush_closes_the_open_bar_at_session_end():
    aggregator = CandleAggregator(interval_seconds=60)
    aggregator.add(_tick(15, 0, 100.0))
    flushed = list(aggregator.flush())
    assert len(flushed) == 1
    assert flushed[0].close == 100.0
    assert list(aggregator.flush()) == []


# ------------------------------------------------------------- determinism
def test_the_same_ticks_always_produce_the_same_bars():
    ticks = [_tick(15, s, 100.0 + s / 10) for s in range(0, 60, 5)]
    ticks.append(_tick(16, 1, 120.0))

    def run() -> list[tuple[float, float, float, float]]:
        aggregator = CandleAggregator(interval_seconds=60)
        out = []
        for tick in ticks:
            candle = aggregator.add(tick)
            if candle is not None:
                out.append((candle.open, candle.high, candle.low, candle.close))
        return out

    assert run() == run()


def test_interval_is_configurable():
    aggregator = CandleAggregator(interval_seconds=300)
    aggregator.add(_tick(15, 0, 100.0))
    assert aggregator.add(_tick(18, 0, 101.0)) is None  # same 5-minute bucket
    candle = aggregator.add(_tick(21, 0, 102.0))
    assert candle is not None
    assert candle.end_at - candle.start_at == timedelta(minutes=5)
