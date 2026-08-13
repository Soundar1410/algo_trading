"""The broker contract.

``PaperBroker`` and ``DhanLiveBroker`` must return the *same* internal order
and fill models (spec section 8). That is the property that makes paper
forward-testing evidence about live behaviour rather than a parallel universe,
so the models live in :mod:`common.models` and this contract only describes
verbs.

Phase 1 implemented the submit/status/health subset the walking skeleton
needed and deliberately deferred modify/cancel/order-book/trades/positions
"until their first consumer" — adding unused abstract methods then would only
have produced stubs that lied about being supported. Phase 10's
``DhanLiveBroker`` is that first consumer for the reconciliation and
controlled-shutdown paths (spec section 8's full interface), so the full set
below is real on both adapters now.

**"Exit position or basket"** (spec's own wording for this interface) is
deliberately *not* a distinct method here. Every existing strategy already
exits by submitting an ordinary closing order through :meth:`Broker.submit` —
a SELL intent is not a different kind of call from a BUY one — and Phase 10
keeps that: a live exit reuses exactly the same idempotency, correlation,
rate-limit and preflight machinery as a live entry, rather than a parallel
"exit" code path that could silently skip one of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from common.models import Fill, Order, OrderIntent


class BrokerError(RuntimeError):
    """Raised when a broker call fails in a way the caller must handle."""


@dataclass(frozen=True, slots=True)
class Quote:
    """A point-in-time quote used to price a fill.

    ``bid``/``ask`` are optional because Phase 1's recorded tape carries last
    price only. The full bid/ask fill model is Phase 4; until then a fill that
    fell back to LTP records that fact in ``Fill.fill_method`` rather than
    pretending it had depth.
    """

    security_id: str
    last_price: float
    quoted_at: datetime
    bid: float | None = None
    ask: float | None = None

    @property
    def has_depth(self) -> bool:
        return self.bid is not None and self.ask is not None


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """One broker-reported open position — the reconciliation snapshot's
    unit (spec section 12, "fetch broker positions"). Deliberately not
    :class:`~common.models.Position`: that type carries local strategy
    intent (stops, targets, roll history) a broker has no concept of. This
    is only what the broker itself reports."""

    security_id: str
    quantity: int
    average_price: float
    product_type: str


@runtime_checkable
class Broker(Protocol):
    """What execution code is allowed to ask of any broker."""

    @property
    def name(self) -> str: ...

    def submit(self, intent: OrderIntent, quote: Quote) -> Order:
        """Submit an intent and return the resulting order.

        Implementations must be idempotent on ``intent.correlation_id``: a
        repeated submission of the same correlation ID is a duplicate, not a
        second order.
        """
        ...

    def order_by_correlation_id(self, correlation_id: str) -> Order | None:
        """Look up an order by correlation ID — the recovery path after a timeout."""
        ...

    def modify(
        self, correlation_id: str, *, quantity: int | None = None, limit_price: float | None = None
    ) -> Order:
        """Modify a still-resting order. Raises :class:`BrokerError` if the
        order named by ``correlation_id`` is not currently modifiable
        (unknown, or already in a terminal state)."""
        ...

    def cancel(self, correlation_id: str) -> Order:
        """Cancel a still-resting order. Raises :class:`BrokerError` under
        the same conditions as :meth:`modify`.

        Implementations must apply the same ambiguous-outcome discipline as
        :meth:`submit`: a cancel confirmation is not trusted until verified,
        never assumed from a response that did not clearly confirm it.
        """
        ...

    def fetch_order_book(self) -> tuple[Order, ...]:
        """Every order this broker knows about, for reconciliation."""
        ...

    def fetch_trades(self) -> tuple[Fill, ...]:
        """Every fill this broker knows about, for reconciliation."""
        ...

    def fetch_positions(self) -> tuple[BrokerPosition, ...]:
        """The broker's own view of open positions — authoritative for
        quantity/existence in live mode (spec's broker-authoritative
        principle). Paper mode has no broker-side position concept (that is
        the local :class:`~common.models.Position` table's job) and always
        returns an empty tuple, documented rather than simulated."""
        ...

    def is_healthy(self) -> bool: ...
