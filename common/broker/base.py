"""The broker contract.

``PaperBroker`` and the future ``DhanLiveBroker`` must return the *same* internal
order and fill models (spec section 8). That is the property that makes paper
forward-testing evidence about live behaviour rather than a parallel universe,
so the models live in :mod:`common.models` and this contract only describes
verbs.

Phase 1 implements the submit/status/health subset the walking skeleton needs.
Modify, cancel, order book, trades and basket exit are declared in the spec's
full interface and arrive with their first consumer — adding unused abstract
methods now would only produce stubs that lie about being supported.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from common.models import Order, OrderIntent


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

    def is_healthy(self) -> bool: ...
