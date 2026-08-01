"""The candle-continuity policy — runbook limitation 4 (Phase 4 Part 3).

`CandleAggregator.mark_feed_gap` used to end with "Full candle-continuity policy
is Phase 4; this is the conservative floor that cannot emit bad data." This is
that policy, and it keeps the floor — what changes is that the floor is now held
by **decision** rather than by deferral, and that the two builders in the tree
differ deliberately rather than by omission:

* the **hub's** aggregator *discards* a bar that spanned a known feed outage. It
  can: another bar will come, and the hub fans out to every worker, so silently
  emitting a stitched bar would corrupt every one of them at once.
* the **engine's** `CandleBuilder` (D23) *emits and marks*. It has no discard
  path, and dropping a bar there would starve an indicator with no signal that
  it happened — the failure limitation 14 already calls "worse in kind".

Nothing is ever forward-filled. A forward-filled bar is a fabricated print; on an
option premium series it invents a price that never traded and every indicator
downstream consumes it as real.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.candles.aggregator import CandleAggregator
from common.candles.builder import CandleBuilder, to_ohlc
from common.indicators import ADX, ATR, EMA, RSI, VWAP, SuperTrend, reset_session_local
from common.indicators.base import OHLC
from common.models import Candle, Tick

IST = ZoneInfo("Asia/Kolkata")
OPEN = datetime(2026, 8, 3, 9, 15, tzinfo=IST)


def _builder(interval: int = 5) -> CandleBuilder:
    return CandleBuilder(interval, security_id="X", instrument="X")


def _at(minutes: float) -> datetime:
    return OPEN + timedelta(minutes=minutes)


# ------------------------------------------------------------------ the marker
def test_a_candle_is_clean_by_default():
    """Defaulting False is what keeps every pre-Part-3 construction site valid."""
    candle = Candle(
        security_id="X",
        instrument="X",
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=0,
        start_at=OPEN,
        end_at=OPEN + timedelta(minutes=5),
    )
    assert candle.spans_gap is False


def test_the_marker_survives_the_conversion_to_the_indicator_shape():
    """`to_ohlc` is the only bridge to the indicator layer; a flag it dropped
    would be a flag no indicator rule could ever act on."""
    builder = _builder()
    builder.add(100.0, _at(1))
    completed = builder.add(101.0, _at(21))
    assert completed is not None and completed.spans_gap is True
    assert to_ohlc(completed).spans_gap is True


# --------------------------------------------------------------- the detection
def test_a_dense_stream_produces_no_marked_bars():
    builder = _builder()
    marked = []
    for minute in range(0, 30):
        candle = builder.add(100.0 + minute, _at(minute))
        if candle is not None:
            marked.append(candle.spans_gap)
    assert marked and not any(marked)


def test_a_silence_that_crosses_a_boundary_but_empties_no_bucket_is_not_a_gap():
    """The case that made the first implementation wrong. 09:16 and 09:22 are six
    minutes apart — longer than the five-minute interval — but they land in
    consecutive buckets, so no bar is missing and nothing was stitched.
    Measuring elapsed silence flags this, and would mark most bars on any
    legitimately sparse stream. An illiquid option leg is exactly that."""
    builder = _builder()
    builder.add(100.0, _at(1))  # 09:16 -> bucket 09:15
    completed = builder.add(101.0, _at(7))  # 09:22 -> bucket 09:20
    assert completed is not None
    assert completed.spans_gap is False


def test_an_entirely_empty_bucket_is_a_gap():
    builder = _builder()
    builder.add(100.0, _at(1))  # bucket 09:15
    completed = builder.add(101.0, _at(11))  # bucket 09:25 — 09:20 is empty
    assert completed is not None
    assert completed.spans_gap is True


def test_the_mark_lands_on_the_bar_that_was_open_when_the_stream_went_quiet():
    """The empty interval sits between the two ticks, so it is the *closing* bar
    that got stitched — not its successor, which is clean from its first tick."""
    builder = _builder()
    builder.add(100.0, _at(1))
    stitched = builder.add(101.0, _at(21))
    assert stitched is not None and stitched.spans_gap is True

    following = builder.add(102.0, _at(26))
    assert following is not None
    assert following.spans_gap is False, "the mark leaked onto a clean successor"


@pytest.mark.parametrize("interval", [1, 5, 15])
def test_the_threshold_scales_with_the_interval(interval):
    """One interval of silence is normal; two means a bar went missing."""
    builder = _builder(interval)
    builder.add(100.0, _at(0))
    ok = builder.add(101.0, _at(interval))
    assert ok is not None and ok.spans_gap is False

    builder2 = _builder(interval)
    builder2.add(100.0, _at(0))
    gapped = builder2.add(101.0, _at(interval * 3))
    assert gapped is not None and gapped.spans_gap is True


def test_a_reset_clears_the_gap_state_with_everything_else():
    builder = _builder()
    builder.add(100.0, _at(1))
    builder.reset()
    builder.add(100.0, _at(21))
    completed = builder.add(101.0, _at(26))
    assert completed is not None
    assert completed.spans_gap is False, "a gap was inferred across a reset boundary"


# --------------------------------------------------- the hub still discards
def test_the_hub_still_discards_rather_than_marking():
    """The two builders differ deliberately. If the hub ever starts emitting
    marked bars instead, D9's guarantee that every worker sees byte-identical
    bars is what breaks."""
    aggregator = CandleAggregator(interval_seconds=60)
    first = Tick(
        security_id="X",
        instrument="X",
        last_price=100.0,
        exchange_time=_at(1),
        received_at=_at(1),
    )
    aggregator.add(first)
    aggregator.mark_feed_gap()

    later = Tick(
        security_id="X",
        instrument="X",
        last_price=101.0,
        exchange_time=_at(3),
        received_at=_at(3),
    )
    assert aggregator.add(later) is None, "the hub published a bar that spanned a gap"
    assert aggregator.dropped_gap_candles == 1


def test_nothing_is_ever_forward_filled():
    """The policy decision, asserted. Intervals inside an outage produce no bar
    at all — not an invented one carrying the last known price."""
    aggregator = CandleAggregator(interval_seconds=60)
    for minute in (0, 10):
        aggregator.add(
            Tick(
                security_id="X",
                instrument="X",
                last_price=100.0 + minute,
                exchange_time=_at(minute),
                received_at=_at(minute),
            )
        )
    # Ten minutes, one-minute bars: at most one bar closed, never ten.
    assert aggregator.rejected_out_of_session == 0
    published = [
        c
        for c in [
            aggregator.add(
                Tick(
                    security_id="X",
                    instrument="X",
                    last_price=120.0,
                    exchange_time=_at(20),
                    received_at=_at(20),
                )
            )
        ]
        if c is not None
    ]
    assert len(published) <= 1, "bars were fabricated for the empty intervals"


# ------------------------------------------------------------ the indicator rule
def test_session_local_indicators_are_reset_and_others_are_not():
    """VWAP is session-cumulative, so missing volume is never recovered. The rest
    are exponentially forgetting and self-correct — resetting one would throw
    away far more history than the hole cost."""
    vwap, ema, rsi, atr, adx, st = VWAP(), EMA(3), RSI(3), ATR(3), ADX(3), SuperTrend(period=1)
    for i in range(10):
        bar = OHLC(high=101 + i, low=99 + i, close=100 + i, open=100 + i, volume=10.0)
        for indicator in (vwap, ema, rsi, atr, adx, st):
            indicator.update(bar)
    assert all(i.is_ready for i in (vwap, ema, rsi, atr, adx, st))

    was_reset = reset_session_local([vwap, ema, rsi, atr, adx, st])

    assert [type(i).__name__ for i in was_reset] == ["VWAP"]
    assert vwap.is_ready is False
    assert all(i.is_ready for i in (ema, rsi, atr, adx, st))


def test_an_indicator_whose_scope_cannot_be_read_is_reset():
    """The conservative direction: the alternative is carrying state that may be
    corrupt because nobody could tell whether it was cumulative."""

    class _Broken(VWAP):
        def warmup_requirement(self):  # type: ignore[override]
            raise RuntimeError("no idea what I am")

    broken = _Broken()
    broken.update(OHLC(high=100, low=100, close=100, volume=5.0))
    assert reset_session_local([broken]) == [broken]
    assert broken.is_ready is False


def test_resetting_nothing_is_harmless():
    assert reset_session_local([]) == []


def test_the_default_strategy_hook_is_a_no_op():
    """A strategy holding only session-spanning indicators genuinely has nothing
    to do, so the base implementation must not force one."""
    from strategies.intraday_options.engine_fixture_strategy import EngineFixtureStrategy

    strategy = EngineFixtureStrategy(enter_on_candle=1)
    bar = OHLC(high=101, low=99, close=100, spans_gap=True)
    assert strategy.on_candle_gap(bar, OPEN) is None
