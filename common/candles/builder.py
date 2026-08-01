"""Per-engine candle aggregation from ticks.

Ported from the reference repository's ``framework/market_data/candle.py``
(Phase 3 Part 2b-i). Strategies work on fixed-interval candles, but the feed
delivers irregular ticks. :class:`CandleBuilder` groups them into buckets aligned
to clock boundaries and emits a completed candle the moment a tick belonging to a
*later* bucket arrives.

Invariant, carried over verbatim: a strategy's ``on_candle()`` must only ever be
called with a *closed* candle, never the in-progress one — this is what prevents
lookahead bias. :meth:`add` enforces it by returning a candle only on bucket
rollover.

Two adaptations, both to avoid duplicating something this repository already has
(deviation D19):

* It emits :class:`common.models.Candle`, not a second candle type. That model is
  frozen, so the in-progress bar is held in a private mutable :class:`_Bar` and
  frozen on completion — the same shape :class:`~common.candles.aggregator.
  CandleAggregator` uses. It also means the bar the engine hands a strategy is
  byte-comparable with the bars the shared feed hub fans out.
* Bucket flooring reuses :func:`common.candles.aggregator.floor_to_interval`
  rather than porting the reference's minute-keyed copy. Two flooring rules that
  could disagree about where 09:20 starts is not a risk worth taking for a
  four-line function.

This sits beside :class:`~common.candles.aggregator.CandleAggregator` rather than
replacing it. The aggregator is the hub's: one per instrument, session-aware, with
feed-gap invalidation and duplicate-publication guards. This is the engine's: one
per chart, no session logic, cheap to reset on every entry — which is what the
per-position premium stream needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from common.indicators.base import OHLC
from common.models import Candle

from .aggregator import floor_to_interval


@dataclass
class _Bar:
    """The in-progress bar. Mutable; frozen into a :class:`Candle` on close."""

    start_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    tick_count: int
    last_tick_at: datetime
    #: Set when a hole was detected while this bar was open. See
    #: :meth:`CandleBuilder.add`.
    spans_gap: bool = False

    def absorb(self, price: float, ts: datetime, volume_delta: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume_delta
        self.tick_count += 1
        self.last_tick_at = ts


class CandleBuilder:
    """Aggregates ticks into fixed-interval candles for one instrument.

    Usage::

        builder = CandleBuilder(5, security_id="13", instrument="NIFTY")
        completed = builder.add(price, ts)   # -> Candle when a bar closes, else None
        builder.flush()                      # -> the final partial candle (or None)
    """

    def __init__(
        self,
        interval_minutes: int,
        *,
        security_id: str = "",
        instrument: str = "",
    ) -> None:
        if interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive")
        self.interval = interval_minutes
        self._security_id = security_id
        self._instrument = instrument
        self._current: _Bar | None = None

    @property
    def current(self) -> _Bar | None:
        """The in-progress (not yet closed) bar, if any."""
        return self._current

    def retarget(self, *, security_id: str, instrument: str = "") -> None:
        """Point the builder at a different instrument, discarding any open bar.

        The premium stream needs this: a new entry, re-entry or strike change must
        never inherit the previous contract's candle. Kept explicit rather than
        inferred from the first tick, so a mislabelled bar is impossible.
        """
        self._security_id = security_id
        self._instrument = instrument or security_id
        self._current = None

    def reset(self) -> None:
        """Clear state for a fresh trading day."""
        self._current = None

    def add(self, price: float, ts: datetime, *, volume_delta: float = 0.0) -> Candle | None:
        """Add a tick and return a candle when one closes.

        ``volume_delta`` is the incremental volume attributable to this update,
        not the exchange's cumulative session volume. It is keyword-only so every
        existing two-argument call remains source-compatible. Callers receiving
        cumulative volume must calculate its delta before passing it here.
        """
        if volume_delta < 0:
            raise ValueError("volume_delta must be non-negative")
        bucket = floor_to_interval(ts, self.interval * 60)

        if self._current is None:
            self._current = self._new_bar(bucket, price, ts, volume_delta)
            return None

        # Gap detection (Phase 4 Part 3, runbook limitation 4). The bar that spans
        # a hole is still emitted — unlike the hub, this builder has no discard
        # path, and dropping a bar here would starve an indicator with no signal
        # that it happened — but it is *marked*, so a consumer can tell stitched
        # data from clean data.
        #
        # **The test is bucket distance, not elapsed silence**, and the difference
        # matters. Ticks at 09:16 and 09:22 are six minutes apart, which is longer
        # than a five-minute interval — but they land in the 09:15 and 09:20
        # buckets, so no bar is missing and nothing was stitched. Measuring
        # elapsed time flags that as a gap and would mark most bars on any
        # legitimately sparse stream, which an illiquid option leg certainly is.
        #
        # A whole bar is missing only when the buckets are more than one interval
        # apart. That is what "a hole" means here, and it is the condition that
        # cannot be true for a merely quiet-but-continuous stream.
        previous_bucket = floor_to_interval(self._current.last_tick_at, self.interval * 60)
        gapped = (bucket - previous_bucket) > timedelta(minutes=self.interval)

        if bucket > self._current.start_at:
            # New bucket -> the previous bar is now closed. The empty interval sits
            # between the two ticks, so it is the bar that was open when the stream
            # went quiet that got stitched — mark that one, not its successor.
            self._current.spans_gap = self._current.spans_gap or gapped
            completed = self._freeze(self._current)
            self._current = self._new_bar(bucket, price, ts, volume_delta)
            return completed

        # Same bucket -> update the running bar. (Unreachable via a gap: two ticks
        # in one bucket cannot have a whole bucket between them. Kept for the
        # `bucket < start_at` late-tick case, which this builder absorbs.)
        self._current.absorb(price, ts, volume_delta)
        return None

    def flush(self) -> Candle | None:
        """Return and clear the final partial candle (e.g. at square-off)."""
        bar, self._current = self._current, None
        return None if bar is None else self._freeze(bar)

    def _freeze(self, bar: _Bar) -> Candle:
        return Candle(
            security_id=self._security_id,
            instrument=self._instrument or self._security_id,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            # This repository's Candle carries an integer volume; the reference
            # accumulated a float. Rounded rather than truncated so a sequence of
            # fractional deltas cannot lose a whole contract.
            volume=round(bar.volume),
            start_at=bar.start_at,
            end_at=bar.start_at + timedelta(minutes=self.interval),
            tick_count=bar.tick_count,
            last_tick_at=bar.last_tick_at,
            spans_gap=bar.spans_gap,
        )

    @staticmethod
    def _new_bar(start_at: datetime, price: float, ts: datetime, volume: float) -> _Bar:
        return _Bar(
            start_at=start_at,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=volume,
            tick_count=1,
            last_tick_at=ts,
        )


def candle_end(start: datetime, interval_minutes: int) -> datetime:
    """Convenience: the close time of a candle that opened at ``start``."""
    return start + timedelta(minutes=interval_minutes)


def to_ohlc(candle: Candle) -> OHLC:
    """View a completed :class:`Candle` as the :class:`OHLC` indicators consume.

    Lives here rather than on the model: :class:`common.models.Candle` crosses a
    multiprocessing queue on every bar and is deliberately a plain frozen record,
    while :class:`OHLC` is the indicator layer's shape. Keeping the conversion
    beside the builder keeps the model free of a dependency on the indicators.
    """
    return OHLC(
        spans_gap=candle.spans_gap,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        open=candle.open,
        volume=float(candle.volume),
        start=candle.start_at,
    )
