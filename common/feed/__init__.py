"""Shared market-data feed hub, bounded worker queues, and reconnection."""

from __future__ import annotations

from .hub import SharedFeedHub, WorkerChannel, build_channel
from .queues import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_TICK_MAX_DEPTH,
    BoundedWorkerQueue,
    QueueStats,
    TickDropNotice,
)
from .reconnect import (
    FeedHealth,
    FeedUnavailableError,
    ReconnectingFeed,
    ReconnectPolicy,
)

__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_TICK_MAX_DEPTH",
    "BoundedWorkerQueue",
    "FeedHealth",
    "FeedUnavailableError",
    "QueueStats",
    "ReconnectPolicy",
    "ReconnectingFeed",
    "SharedFeedHub",
    "TickDropNotice",
    "WorkerChannel",
    "build_channel",
]
