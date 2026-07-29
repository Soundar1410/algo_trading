"""Market-data adapters. Strategy code never imports a broker SDK directly."""

from __future__ import annotations

from .adapter import MarketFeedAdapter, TickCallback
from .recorded import RecordedFeedAdapter, load_tick_tape

__all__ = [
    "MarketFeedAdapter",
    "RecordedFeedAdapter",
    "TickCallback",
    "load_tick_tape",
]
