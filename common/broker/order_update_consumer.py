"""Order-update consumer: Dhan's own order-update websocket feed
(``wss://api-order-update.dhan.co``, ``dhanhq.orderupdate.OrderUpdate``).

**Correction from an earlier design**: reading only the installed SDK's
illustrative default handler (``OrderUpdate.handle_order_update``) suggested
no correlation ID was present in this feed, implying a fallback lookup via
``OrderNo`` was the primary path. The *documented* payload
(``https://dhanhq.co/docs/v2/order-update/``, verified directly, not
invented) proves otherwise: it lists 53 fields including ``CorrelationId``
explicitly. This consumer reads ``CorrelationId`` directly — ``OrderNo`` is
only a defensive fallback identity for the rare case a message arrives
without one.

The actual websocket connect/reconnect loop is not exercised by the tests
in this module (Phase 10 tests must not touch the network) — what is fully
tested is :func:`parse_order_update_message`, the part that actually
decides how a raw message becomes a typed, actionable event. Dhan's own
"poll only as fallback" guidance (spec 1583) means this feed is the primary
path and :meth:`~common.broker.dhan_live.DhanLiveBroker.order_by_correlation_id`
is the fallback, not the reverse.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from common.logging import get_logger
from common.models import OrderStatus

from .dhan_live import map_dhan_order_status

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OrderUpdateEvent:
    correlation_id: str | None
    broker_order_id: str | None
    status: OrderStatus
    raw_status: str | None


def parse_order_update_message(message: dict[str, Any]) -> OrderUpdateEvent | None:
    """Parse one raw websocket message. Returns ``None`` for anything not
    recognised as an order alert (Dhan's feed carries other message types
    too) — this is a "skip", not a failure the caller needs to handle.

    Reads ``CorrelationId``/``Status``/``OrderNo`` from the documented
    payload shape directly (case as Dhan documents it: capitalised keys).
    An unrecognised or missing status string maps to
    :data:`~common.models.OrderStatus.UNKNOWN` via
    :func:`~common.broker.dhan_live.map_dhan_order_status` — never
    silently treated as terminal or successful.
    """
    if message.get("Type") != "order_alert":
        return None
    data = message.get("Data", {})
    if not isinstance(data, dict):
        return None

    correlation_id = data.get("CorrelationId") or None
    order_no = data.get("OrderNo")
    raw_status = data.get("Status")

    return OrderUpdateEvent(
        correlation_id=correlation_id,
        broker_order_id=str(order_no) if order_no else None,
        status=map_dhan_order_status(raw_status),
        raw_status=raw_status,
    )


class OrderUpdateConsumer:
    """Translates raw websocket messages into :class:`OrderUpdateEvent`
    and hands each one to ``on_event`` — the only side effect this class
    has. Connection lifecycle (the real ``dhanhq.orderupdate.OrderUpdate``
    instance, reconnect/backoff) is the caller's responsibility; this class
    is deliberately transport-agnostic so it is testable without a socket.
    """

    def __init__(self, on_event: Callable[[OrderUpdateEvent], None]) -> None:
        self._on_event = on_event

    def handle_raw_message(self, message: dict[str, Any]) -> None:
        event = parse_order_update_message(message)
        if event is None:
            log.debug("order-update message not an order_alert, ignored")
            return
        if event.correlation_id is None:
            log.warning(
                "order-update for broker_order_id=%s carries no CorrelationId — "
                "the OrderNo-based fallback path must resolve this, not this handler alone",
                event.broker_order_id,
            )
        self._on_event(event)
