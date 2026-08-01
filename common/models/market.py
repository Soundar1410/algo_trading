"""Market-data value types.

Plain frozen dataclasses rather than pydantic models: these cross a
``multiprocessing`` queue on every tick and candle, and pydantic validation on
the hot path would cost more than it catches. Validation belongs at the feed
boundary, where untrusted broker bytes are decoded — once a :class:`Tick` exists
it has already been checked.

Frozen also means a worker cannot mutate a candle it received and hand a
different object to the next component, which is how "every worker sees
identical bars" stops being a convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Tick:
    """One normalised quote for one instrument.

    ``exchange_time`` is the exchange's own clock and is preferred for all
    session and candle logic; ``received_at`` is ours. Keeping both separate is
    what lets an incident distinguish "the exchange was late" from "we were".
    """

    security_id: str
    instrument: str
    last_price: float
    exchange_time: datetime
    received_at: datetime
    last_quantity: int = 0
    bid_price: float | None = None
    ask_price: float | None = None
    sequence: int | None = None

    def __post_init__(self) -> None:
        if self.last_price <= 0:
            raise ValueError(f"tick last_price must be positive, got {self.last_price}")
        if self.exchange_time.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("tick timestamps must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Candle:
    """One completed OHLC bar.

    Only completed candles are published. ``start_at`` is inclusive and
    ``end_at`` exclusive, so consecutive bars tile the session without a
    one-second gap or overlap at the boundary.

    There is no ``is_complete`` flag on purpose: an incomplete candle is never
    represented by this type at all, so no consumer can forget to check it.

    ``spans_gap`` does not contradict that rule. Completeness is about *whether*
    the interval closed, which is still never in question here; ``spans_gap`` is
    about *how* — whether the ticks that built it had a hole in the middle. See
    the field's own note below.
    """

    security_id: str
    instrument: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    start_at: datetime
    end_at: datetime
    tick_count: int = 0
    #: Latest exchange timestamp among the ticks that built this bar.
    last_tick_at: datetime | None = None
    #: True when this bar's interval contained a known hole in the tick stream —
    #: its OHLC is stitched across ticks either side of missing data.
    #:
    #: **The hub never sets this**: :class:`~common.candles.aggregator.CandleAggregator`
    #: *discards* a gap-spanning bar rather than publishing one, and that stays
    #: the policy. It is set by :class:`~common.candles.builder.CandleBuilder`,
    #: the engine's own per-chart builder (**D23**), which has no discard path —
    #: dropping a bar there would starve an indicator silently, which runbook
    #: limitation 14 already calls "worse in kind" than a visible loss.
    #:
    #: Defaults False, so every pre-Part-3 construction site is unchanged. It
    #: travels with the bar across the IPC queue, so no consumer can receive a
    #: stitched bar without also receiving the fact that it is one.
    spans_gap: bool = False

    def __post_init__(self) -> None:
        if self.end_at <= self.start_at:
            raise ValueError("candle end_at must be after start_at")
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("candle timestamps must be timezone-aware")
        if self.high < self.low:
            raise ValueError(f"candle high {self.high} is below low {self.low}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"candle open {self.open} outside [{self.low}, {self.high}]")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"candle close {self.close} outside [{self.low}, {self.high}]")
