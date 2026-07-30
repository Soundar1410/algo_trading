"""The shared feed hub.

One Dhan WebSocket per strategy group, never one per strategy. The hub owns that
single connection, subscribes once for the union of every worker's instruments,
aggregates ticks into candles, and fans the **completed candles** out to bounded
per-worker queues.

Fanning out candles rather than raw ticks is deviation **D9** from the spec,
which describes distributing normalised ticks. Aggregating once, centrally,
means every worker provably sees the same bars: two workers cannot disagree
about the 09:16 close because they each rebuilt it from their own view of the
tick stream. It also satisfies the spec's "prevent duplicate candle publication"
rule structurally, since there is exactly one aggregator per instrument in the
group. The cost is that a worker cannot pick its own timeframe off the raw
stream; it aggregates further from completed bars instead, and a tick channel
can be added alongside this one without reshaping the queues.

The hub deliberately does no strategy work on the callback path. It validates,
aggregates and publishes — anything slower belongs in a worker.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from common.candles import CandleAggregator
from common.logging import get_logger
from common.market_data.adapter import MarketFeedAdapter
from common.models import Candle, Tick

from .queues import BoundedWorkerQueue, QueueStats

_log = get_logger(__name__)


@dataclass(frozen=True)
class WorkerChannel:
    """One registered worker: which instruments it wants, and where to send them."""

    strategy_id: str
    security_ids: frozenset[str]
    queue: BoundedWorkerQueue

    def wants(self, security_id: str) -> bool:
        return security_id in self.security_ids


class SharedFeedHub:
    """Owns one feed adapter and fans completed candles out to workers."""

    def __init__(
        self,
        adapter: MarketFeedAdapter,
        *,
        interval_seconds: int = 60,
    ) -> None:
        self._adapter = adapter
        self._interval_seconds = interval_seconds
        self._channels: list[WorkerChannel] = []
        #: One aggregator per instrument — the single source of every bar.
        self._aggregators: dict[str, CandleAggregator] = {}
        self.tick_count = 0
        self.candle_count = 0
        self.gap_candles_discarded = 0
        self.last_tick_at: datetime | None = None

    # ------------------------------------------------------------ wiring
    def register(self, channel: WorkerChannel) -> None:
        if any(c.strategy_id == channel.strategy_id for c in self._channels):
            raise ValueError(f"Worker {channel.strategy_id!r} is already registered with the hub")
        self._channels.append(channel)

    @property
    def channels(self) -> tuple[WorkerChannel, ...]:
        return tuple(self._channels)

    def subscription_union(self) -> frozenset[str]:
        """Every instrument any registered worker needs — subscribed once."""
        union: set[str] = set()
        for channel in self._channels:
            union.update(channel.security_ids)
        return frozenset(union)

    # ------------------------------------------------------------ running
    def start(self) -> None:
        """Subscribe the union and run the adapter until it stops."""
        union = self.subscription_union()
        if not union:
            raise RuntimeError("Refusing to start the feed hub with no registered workers")
        self._adapter.subscribe(sorted(union))
        _log.info(
            "feed hub starting instruments=%d workers=%d",
            len(union),
            len(self._channels),
        )
        self._adapter.start(self.on_tick)

    def stop(self) -> None:
        """Stop the adapter and flush every partial bar to its subscribers."""
        self._adapter.stop()
        for security_id, aggregator in self._aggregators.items():
            for candle in aggregator.flush(security_id):
                self._fan_out(candle)

    def mark_feed_gap(self) -> int:
        """Invalidate every open bar because the feed was lost.

        Wired to :class:`~common.feed.reconnect.ReconnectingFeed` as its
        ``on_feed_gap`` hook. Any bar open during the outage is missing the ticks
        that occurred while the socket was down, so publishing it would emit a
        bar stitched across a hole — see the aggregator for the full argument.

        Note that the aggregators are **not** rebuilt here, and must not be: each
        one's record of what it has already published is what makes duplicate
        publication across a reconnect structurally impossible. A fresh
        aggregator would have forgotten.
        """
        discarded = 0
        for security_id, aggregator in self._aggregators.items():
            discarded += aggregator.mark_feed_gap(security_id)
        self.gap_candles_discarded += discarded
        return discarded

    def on_tick(self, tick: Tick) -> None:
        """Feed-callback path: validate minimally, aggregate, publish. Nothing else."""
        self.tick_count += 1
        self.last_tick_at = tick.exchange_time

        aggregator = self._aggregators.get(tick.security_id)
        if aggregator is None:
            aggregator = CandleAggregator(interval_seconds=self._interval_seconds)
            self._aggregators[tick.security_id] = aggregator

        completed = aggregator.add(tick)
        if completed is not None:
            self._fan_out(completed)

    def _fan_out(self, candle: Candle) -> None:
        self.candle_count += 1
        for channel in self._channels:
            if not channel.wants(candle.security_id):
                continue
            if not channel.queue.publish(candle):
                # Overflow is a health event, never a silent drop.
                _log.warning(
                    "worker queue overflow strategy_id=%s dropped=%d depth=%d",
                    channel.strategy_id,
                    channel.queue.dropped,
                    channel.queue.depth(),
                )

    # ------------------------------------------------------------- health
    def queue_stats(self) -> tuple[QueueStats, ...]:
        return tuple(channel.queue.stats() for channel in self._channels)


def build_channel(
    strategy_id: str,
    security_ids: Sequence[str],
    *,
    max_depth: int = 1000,
    in_process: bool = False,
) -> WorkerChannel:
    """Create a worker channel with its own bounded queue."""
    q = (
        BoundedWorkerQueue.in_process(strategy_id, max_depth)
        if in_process
        else BoundedWorkerQueue(name=strategy_id, max_depth=max_depth)
    )
    return WorkerChannel(
        strategy_id=strategy_id,
        security_ids=frozenset(security_ids),
        queue=q,
    )
