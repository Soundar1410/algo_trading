"""Fetch and normalise broker-side state (spec 2189-2212's
``fetch broker orders → fetch broker trades → fetch broker positions →
normalise broker snapshot`` steps).

Deliberately thin: :class:`~common.broker.base.Broker`'s
``fetch_order_book``/``fetch_trades``/``fetch_positions`` already return
the shared internal models (:class:`~common.models.Order`,
:class:`~common.models.Fill`, :class:`~common.broker.base.BrokerPosition`)
— "normalise" here means nothing more than calling all three and bundling
the result, because the normalisation work already happened once, in the
broker adapter itself (spec section 8: "PaperBroker and DhanLiveBroker must
return the same internal order and fill models").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from common.broker.base import Broker, BrokerPosition
from common.models import Fill, Order


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    fetched_at: datetime
    orders: tuple[Order, ...]
    trades: tuple[Fill, ...]
    positions: tuple[BrokerPosition, ...]


def fetch_broker_snapshot(broker: Broker) -> BrokerSnapshot:
    """One consistent-enough read of everything the broker reports right
    now. Not a single atomic broker-side transaction (Dhan offers no such
    thing) — three separate calls, timestamped once at the start, which is
    the best any REST-polled broker snapshot can honestly claim to be.
    """
    return BrokerSnapshot(
        fetched_at=datetime.now(UTC),
        orders=broker.fetch_order_book(),
        trades=broker.fetch_trades(),
        positions=broker.fetch_positions(),
    )
