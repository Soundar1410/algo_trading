"""Shared market-data feed hub, bounded worker queues, and reconnection."""

from __future__ import annotations

from .hub import SharedFeedHub, WorkerChannel, build_channel
from .queues import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_TICK_MAX_DEPTH,
    DROP_NOTICE_EVERY,
    BoundedWorkerQueue,
    QueueStats,
    TickDropNotice,
    drop_notice_cadence,
)
from .reconnect import (
    FeedHealth,
    FeedHealthEvent,
    FeedUnavailableError,
    ReconnectingFeed,
    ReconnectPolicy,
)

__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_TICK_MAX_DEPTH",
    "DROP_NOTICE_EVERY",
    "BoundedWorkerQueue",
    "FeedHealth",
    "FeedHealthEvent",
    "FeedUnavailableError",
    "QueueStats",
    "ReconnectPolicy",
    "ReconnectingFeed",
    "SharedFeedHub",
    "TickDropNotice",
    "WorkerChannel",
    "build_channel",
    "drop_notice_cadence",
]
