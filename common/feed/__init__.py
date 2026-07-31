"""Shared market-data feed hub, bounded worker queues, and reconnection."""

from __future__ import annotations

from .hub import SharedFeedHub, WorkerChannel, build_channel
from .queues import DEFAULT_MAX_DEPTH, DEFAULT_TICK_MAX_DEPTH, BoundedWorkerQueue, QueueStats
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
    "WorkerChannel",
    "build_channel",
]
