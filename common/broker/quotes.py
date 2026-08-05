"""The recent-quote record the fill model prices against.

Phase 4 Part 5. Before this, :class:`~common.execution.lifecycle.OrderLifecycle`
built its :class:`~common.broker.base.Quote` from ``signal.reference_price`` alone,
so the simulator had a last price and nothing else — no bid, no ask, no way to
tell how old the number was. Depth now reaches the tick (Part 5's feed changes),
and this is what carries it the rest of the way.

Why a shared record rather than four widened signatures
-------------------------------------------------------
The obvious route was to add bid/ask to
:class:`~common.engine.positions.ExecutionGateway`'s two verbs, then to
:class:`~common.models.Signal`, then to the ``Quote`` built from it. That means
editing :class:`~common.engine.positions.PositionManager` — a Phase 3 port, whose
regression tests came with it — for no gain, because **two** consumers want the
same data and neither is on that path:

* ``OrderLifecycle`` wants *the current* quote, to price a fill against a real
  book instead of a bare last price.
* :class:`~common.broker.paper.PaperBroker` wants *a range* of recent quotes, to
  select the one at or after a simulated latency deadline and to settle resting
  limit orders against prices that arrived after submission.

One object answers both, and the ported engine keeps its signatures.

``Signal.reference_price`` is untouched and still records the price the strategy
actually decided on, so ``Fill.slippage_amount`` remains auditable against what
the strategy saw rather than against whatever the book did next.

Thread ownership
----------------
**Written and read on the feed callback thread only.** In a worker the whole
stack — ``HubTickFeed.run`` → ``TradingEngine.on_tick`` → ``PositionManager`` →
``LifecycleGateway`` → ``OrderLifecycle`` → ``PaperBroker`` — runs inside one
callback, and the wall-clock square-off net runs on that same loop's poll wake.
So there is no lock here, for the same reason the feed contract has none: the
ownership rule is the synchronisation. A future caller that needs this from
another thread must add the lock rather than assume this one is safe.

Timestamps are ``exchange_time``, not receipt time
--------------------------------------------------
Every ``quoted_at`` here is the exchange's own clock, matching what candles,
sessions and square-off already use (Part 3). It is also the right basis for both
things the broker asks of this class: a latency deadline is a statement about the
market's timeline, and a staleness check should count the age of the *price*,
which includes the time it spent reaching us.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

from common.models import Tick

from .base import Quote

#: Quotes retained per instrument. Sized from this repository's own measured tick
#: rate — the Phase 2 Block 2 capture saw ~4/s for one instrument — so 64 is
#: roughly fifteen seconds of history at that rate and a few seconds at a busy
#: open. That comfortably spans any credible submission latency (the configured
#: default is 250 ms) while staying small enough that an engine holding the
#: underlying plus one contract keeps a trivial amount of memory.
DEFAULT_DEPTH = 64


class QuoteBook:
    """Recent quotes per ``security_id``, oldest first."""

    def __init__(self, *, depth: int = DEFAULT_DEPTH) -> None:
        if depth < 1:
            raise ValueError("QuoteBook depth must be at least 1")
        self._depth = depth
        self._quotes: dict[str, deque[Quote]] = {}
        #: Ticks recorded, for the same reason the feed counts its own: a book
        #: that is empty when an order arrives is a wiring failure, and a counter
        #: is how that gets told apart from a quiet market.
        self.recorded = 0

    def record(self, tick: Tick) -> None:
        """Add one tick's quote. **Feed thread only** — see the module docstring."""
        quotes = self._quotes.get(tick.security_id)
        if quotes is None:
            quotes = deque(maxlen=self._depth)
            self._quotes[tick.security_id] = quotes
        quotes.append(quote_from_tick(tick))
        self.recorded += 1

    def latest(self, security_id: str) -> Quote | None:
        """The newest quote for this instrument, or ``None`` if none was recorded."""
        quotes = self._quotes.get(security_id)
        return quotes[-1] if quotes else None

    def after(self, security_id: str, deadline: datetime) -> Quote | None:
        """The oldest recorded quote at or after ``deadline``, or ``None``.

        "Oldest at or after", not "newest": the simulated order becomes live at
        the deadline and would have executed against the first price available
        then, not against the best one that showed up later. Taking the newest
        would be lookahead wearing a latency model's clothes.

        Returns ``None`` when nothing that new has been seen — which on a live
        feed is the ordinary case at submission time, because the future has not
        happened yet. The caller records that it fell back rather than pretending
        a latency was applied; see :class:`~common.broker.paper.PaperBroker`.
        """
        quotes = self._quotes.get(security_id)
        if not quotes:
            return None
        for quote in quotes:
            if quote.quoted_at >= deadline:
                return quote
        return None

    def instruments(self) -> frozenset[str]:
        """Every instrument with at least one recorded quote. For assertions."""
        return frozenset(sid for sid, quotes in self._quotes.items() if quotes)


def quote_from_tick(tick: Tick) -> Quote:
    """One tick as a quote. The single place the conversion happens."""
    return Quote(
        security_id=tick.security_id,
        last_price=tick.last_price,
        quoted_at=tick.exchange_time,
        bid=tick.bid_price,
        ask=tick.ask_price,
    )
