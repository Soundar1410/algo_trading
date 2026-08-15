"""A direct, single-process :class:`~common.engine.feed.MarketDataFeed` over
one :class:`~common.market_data.adapter.MarketFeedAdapter`.

Sibling to :class:`~common.engine.hub_feed.HubTickFeed`, for a process that
owns its adapter outright rather than sharing it across several worker
*processes* via :class:`~common.feed.hub.SharedFeedHub`. ``positional_options``
is the first user: it drives exactly one strategy per process (see
``runtimes/positional_options/worker.py``'s own module docstring for why), so
there is nothing to fan the feed out to, and the hub's candle-aggregation
machinery — built for intraday's per-minute-bar consumers — has no role to
play for a tick/quote-driven positional engine that only ever wants raw
ticks.

Threading contract is exactly the adapter's own (see
:class:`~common.market_data.adapter.MarketFeedAdapter`'s own docstring): the
thread that calls :meth:`run` owns the connection and is the only thread
permitted to close it; every other thread may only call :meth:`stop`, which
merely asks.
"""

from __future__ import annotations

from collections.abc import Callable

from common.logging import get_logger
from common.market_data.adapter import MarketFeedAdapter

from .feed import MarketDataFeed

log = get_logger(__name__)


class AdapterFeed(MarketDataFeed):
    """Wraps one adapter directly — no hub, no candle aggregation."""

    def __init__(
        self,
        adapter: MarketFeedAdapter,
        *,
        segment_for: Callable[[str], int | None] | None = None,
        mode_for: Callable[[str], int | None] | None = None,
        on_idle: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._adapter = adapter
        #: Per-instrument segment/mode resolvers. ``feed.subscribe(security_id)``
        #: (the base :class:`MarketDataFeed` contract) carries no segment or
        #: mode of its own, so something has to know which exchange segment
        #: and which subscription mode each id needs — an options runtime's
        #: underlying and its option legs never share either. ``None`` for
        #: either keeps the adapter's own default, exactly as an unset
        #: ``WorkerChannel`` segment/mode does for the hub's own *initial*
        #: subscriptions.
        self._segment_for = segment_for or (lambda _sid: None)
        self._mode_for = mode_for or (lambda _sid: None)
        #: Called on the adapter's own thread whenever it wakes idle. Nothing
        #: dynamic needs draining here — unlike the hub, :meth:`subscribe`
        #: calls the adapter directly and synchronously, since this feed owns
        #: its connection outright — so this exists only for a caller that
        #: wants a periodic liveness tick on the feed's own thread.
        self._on_idle = on_idle

    def run(self) -> None:
        self._running = True
        self._adapter.start(self._emit, on_idle=self._on_idle)

    def stop(self) -> None:
        super().stop()
        # Never adapter.stop(): that closes the socket, and only the thread
        # that called start() is allowed to (see the module docstring).
        # request_stop() only sets a flag start()'s own loop already polls.
        request_stop = getattr(self._adapter, "request_stop", None)
        if callable(request_stop):
            request_stop()

    def _do_subscribe(self, security_id: str) -> None:
        segment = self._segment_for(security_id)
        mode = self._mode_for(security_id)
        if segment is None and mode is None:
            self._adapter.subscribe([security_id])
        else:
            self._adapter.subscribe([security_id], segment=segment, mode=mode)
