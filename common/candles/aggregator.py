"""Tick-to-candle aggregation.

Candle generation is production code, not a test convenience, and the spec's
rules are strict:

* completed candles only — a bar is published when a tick proves the interval
  has closed, never on a timer;
* a later tick may never modify an already published candle;
* no duplicate publication;
* exchange timestamps drive session logic, in ``Asia/Kolkata``.

The interval boundary is computed by flooring the exchange timestamp, so the
same tape always yields the same bars regardless of when it is replayed. That
determinism is the whole reason aggregation happens once, in the hub, rather
than independently in each worker.

Ticks outside the configured session are dropped rather than folded into the
first or last bar of the day: a pre-open print in the 09:15 candle would quietly
corrupt every indicator built on it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from common.models import Candle, Tick

DEFAULT_TIMEZONE = "Asia/Kolkata"
DEFAULT_INTERVAL_SECONDS = 60


def floor_to_interval(moment: datetime, interval_seconds: int) -> datetime:
    """Floor a timestamp to the start of its interval, in its own timezone.

    Flooring is done on the wall clock of the exchange timezone rather than on a
    Unix epoch, so a 1-minute bar starts at :00 seconds local time even where
    the offset is not a whole number of hours (India is UTC+05:30).
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((moment - midnight).total_seconds())
    return midnight + timedelta(seconds=(elapsed // interval_seconds) * interval_seconds)


@dataclass(frozen=True)
class SessionWindow:
    """Exchange session bounds used to accept or reject a tick."""

    start: time = time(9, 15)
    end: time = time(15, 30)
    timezone: str = DEFAULT_TIMEZONE

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def contains(self, moment: datetime) -> bool:
        local = moment.astimezone(self.zone).timetz().replace(tzinfo=None)
        return self.start <= local < self.end


@dataclass
class _Bar:
    """Mutable accumulator. Converted to a frozen Candle only on completion."""

    security_id: str
    instrument: str
    start_at: datetime
    end_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    tick_count: int = 0
    last_tick_at: datetime | None = None

    def absorb(self, tick: Tick) -> None:
        self.high = max(self.high, tick.last_price)
        self.low = min(self.low, tick.last_price)
        self.close = tick.last_price
        self.volume += tick.last_quantity
        self.tick_count += 1
        self.last_tick_at = tick.exchange_time

    def freeze(self) -> Candle:
        return Candle(
            security_id=self.security_id,
            instrument=self.instrument,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            start_at=self.start_at,
            end_at=self.end_at,
            tick_count=self.tick_count,
            last_tick_at=self.last_tick_at,
        )


@dataclass
class CandleAggregator:
    """Builds completed candles per instrument from a tick stream."""

    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    session: SessionWindow = field(default_factory=SessionWindow)
    _open_bars: dict[str, _Bar] = field(default_factory=dict, init=False)
    _published: set[tuple[str, datetime]] = field(default_factory=set, init=False)
    rejected_out_of_session: int = field(default=0, init=False)
    rejected_late: int = field(default=0, init=False)

    def add(self, tick: Tick) -> Candle | None:
        """Feed one tick. Returns a candle only when this tick *completed* one.

        The returned candle is the bar the tick closed, never the bar the tick
        belongs to — that one is still open.
        """
        moment = tick.exchange_time.astimezone(self.session.zone)
        if not self.session.contains(moment):
            self.rejected_out_of_session += 1
            return None

        bucket_start = floor_to_interval(moment, self.interval_seconds)
        bucket_end = bucket_start + timedelta(seconds=self.interval_seconds)
        current = self._open_bars.get(tick.security_id)

        if current is None:
            self._open_bars[tick.security_id] = self._new_bar(tick, bucket_start, bucket_end)
            return None

        if bucket_start == current.start_at:
            current.absorb(tick)
            return None

        if bucket_start < current.start_at:
            # A tick for an interval we have already closed. Publishing a
            # correction would violate "never use a future tick to modify an
            # already published candle", so it is counted and dropped.
            self.rejected_late += 1
            return None

        completed = self._complete(current)
        self._open_bars[tick.security_id] = self._new_bar(tick, bucket_start, bucket_end)
        return completed

    def flush(self, security_id: str | None = None) -> Iterator[Candle]:
        """Close open bars — used at session end and on shutdown.

        A flushed bar is a real completed bar for the interval it covers; it is
        published only once, like any other.
        """
        keys = [security_id] if security_id is not None else list(self._open_bars)
        for key in keys:
            bar = self._open_bars.pop(key, None)
            if bar is not None:
                yield self._complete(bar)

    def _new_bar(self, tick: Tick, start_at: datetime, end_at: datetime) -> _Bar:
        return _Bar(
            security_id=tick.security_id,
            instrument=tick.instrument,
            start_at=start_at,
            end_at=end_at,
            open=tick.last_price,
            high=tick.last_price,
            low=tick.last_price,
            close=tick.last_price,
            volume=tick.last_quantity,
            tick_count=1,
            last_tick_at=tick.exchange_time,
        )

    def _complete(self, bar: _Bar) -> Candle:
        key = (bar.security_id, bar.start_at)
        if key in self._published:
            raise RuntimeError(
                f"Candle for {bar.security_id} at {bar.start_at.isoformat()} "
                "was already published — duplicate publication is forbidden"
            )
        self._published.add(key)
        return bar.freeze()
