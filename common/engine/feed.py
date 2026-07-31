"""The tick source the engine consumes.

Ported from the reference repository's ``framework/market_data/feed.py``
(Phase 3 Part 2b-i). The engine does not care *where* ticks come from — only that
it receives :class:`~common.models.Tick` objects for the instruments it
subscribed to. This module defines:

* :class:`MarketDataFeed` — the callback interface the engine consumes.
* :class:`SimulatedFeed`  — replays a fixed tick sequence, for offline runs and
  tests. No external dependencies.

Relationship to :class:`~common.market_data.adapter.MarketFeedAdapter`
----------------------------------------------------------------------
This repository already has a feed abstraction, but a different one: an *adapter*
owns a broker connection, is driven by the supervisor, and fans completed candles
out to worker processes (deviation D9). This one is the *consumer* side inside a
single worker: ``run()`` blocks, delivering ticks to one registered handler.

They meet in Part 2b-ii, where the hub's new tick channel is wrapped in a
``MarketDataFeed`` implementation reading the worker's queue. That is also why
:meth:`stop` here stays a plain flag flip: under the Part 1 ownership rule the
thread that called ``run()`` is the one permitted to end it, and every caller in
this module is on that thread.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum

from common.logging import get_logger
from common.models import Tick

log = get_logger(__name__)

TickHandler = Callable[[Tick], None]


class SubscriptionMode(IntEnum):
    TICKER = 15
    QUOTE = 17
    FULL = 21


@dataclass(frozen=True)
class FeedSubscription:
    exchange_segment: int
    security_id: str
    mode: SubscriptionMode = SubscriptionMode.TICKER


class MarketDataStatus(StrEnum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"
    QUEUE_OVERFLOW = "queue_overflow"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class MarketDataEvent:
    status: MarketDataStatus
    timestamp: datetime
    detail: str = ""
    attempt: int = 0


StatusHandler = Callable[[MarketDataEvent], None]


class MarketDataFeed(ABC):
    """Interface for a live or simulated market-data source."""

    def __init__(self) -> None:
        self._on_tick: TickHandler | None = None
        self._on_status: StatusHandler | None = None
        self._subscribed: set[str] = set()
        self._running = False

    def on_tick(self, handler: TickHandler) -> None:
        """Register the callback invoked for every received tick."""
        self._on_tick = handler

    def on_status(self, handler: StatusHandler) -> None:
        """Register an optional lifecycle callback; existing consumers ignore it."""
        self._on_status = handler

    def subscribe(self, security_id: str) -> None:
        """Subscribe to live updates for an instrument (idempotent)."""
        if security_id and security_id not in self._subscribed:
            self._subscribed.add(security_id)
            self._do_subscribe(security_id)

    def unsubscribe(self, security_id: str) -> None:
        if security_id in self._subscribed:
            self._subscribed.discard(security_id)
            self._do_unsubscribe(security_id)

    @property
    def subscriptions(self) -> frozenset[str]:
        return frozenset(self._subscribed)

    def _emit(self, tick: Tick) -> None:
        if self._on_tick is not None:
            self._on_tick(tick)

    def _emit_status(self, event: MarketDataEvent) -> None:
        if self._on_status is not None:
            self._on_status(event)

    @abstractmethod
    def run(self) -> None:
        """Start delivering ticks. Blocks until stopped or exhausted."""

    def stop(self) -> None:
        self._running = False

    # Subclass hooks (no-ops by default so simple feeds need not override).
    # Deliberately not @abstractmethod, and the empty bodies are the contract:
    # a replay feed has nothing to subscribe *to*, and forcing every such feed to
    # write an empty override would change the ported contract for no gain. Same
    # judgement, and the same suppression, as `common/exit/base.py::reset` in
    # Part 2a.
    def _do_subscribe(self, security_id: str) -> None:  # noqa: B027
        pass

    def _do_unsubscribe(self, security_id: str) -> None:  # noqa: B027
        pass


class SimulatedFeed(MarketDataFeed):
    """Replays a fixed sequence of ticks. Used for offline runs and tests.

    Ticks are delivered in order regardless of subscription (the engine routes by
    ``security_id``), which keeps replay simple and deterministic.
    """

    def __init__(self, ticks: Iterable[Tick], *, realtime: bool = False) -> None:
        super().__init__()
        self._ticks = list(ticks)
        self._realtime = realtime  # if True, sleep between ticks by timestamp gap

    def run(self) -> None:
        self._running = True
        # Announce CONNECTED exactly as a live feed does, timestamped at the first
        # tick. A replayed list has complete, gap-free coverage of what it was
        # given, starting there — and consumers that reason about coverage must be
        # able to establish that offline too, rather than assuming it. Stating it
        # from the first tick rather than "now" also keeps replay deterministic.
        if self._ticks:
            self._emit_status(
                MarketDataEvent(MarketDataStatus.CONNECTED, self._ticks[0].exchange_time)
            )
        prev_ts: datetime | None = None
        for tick in self._ticks:
            if not self._running:
                break
            if self._realtime and prev_ts is not None:
                gap = (tick.exchange_time - prev_ts).total_seconds()
                if gap > 0:
                    time.sleep(gap)
            prev_ts = tick.exchange_time
            self._emit(tick)
        self._running = False
