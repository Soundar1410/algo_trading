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

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from websockets.sync.client import connect

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

    # Official payload spelling first; the installed dhanhq==2.2.0 sample
    # handler reads lower-camel spellings, so accept those as an explicit SDK
    # compatibility fallback rather than dropping a genuine update.
    correlation_id = data.get("CorrelationId") or data.get("correlationId") or None
    order_no = data.get("OrderNo") or data.get("orderNo")
    raw_status = data.get("Status") or data.get("status")

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


class OrderUpdateInbox:
    """Thread-safe handoff from the websocket thread to broker submission.

    Acknowledgements are retained but do not wake a submitter as a completed
    outcome.  The submitter waits for a fill/partial/terminal event, then uses
    one correlation lookup to confirm the authoritative REST representation and
    fetches trade rows.  A timeout simply hands control to that same bounded
    lookup fallback; it never causes another order submission.
    """

    _RESOLVING: ClassVar[frozenset[OrderStatus]] = frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    )

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._events_by_correlation: dict[str, OrderUpdateEvent] = {}
        self._events_by_order_id: dict[str, OrderUpdateEvent] = {}
        self._correlation_by_order_id: dict[str, str] = {}

    def register(self, correlation_id: str, broker_order_id: str) -> None:
        with self._condition:
            self._correlation_by_order_id[broker_order_id] = correlation_id
            pending = self._events_by_order_id.get(broker_order_id)
            if pending is not None:
                self._events_by_correlation[correlation_id] = pending
            self._condition.notify_all()

    def accept(self, event: OrderUpdateEvent) -> None:
        with self._condition:
            correlation_id = event.correlation_id
            if correlation_id is None and event.broker_order_id is not None:
                correlation_id = self._correlation_by_order_id.get(event.broker_order_id)
            if correlation_id is not None:
                self._events_by_correlation[correlation_id] = event
            if event.broker_order_id is not None:
                self._events_by_order_id[event.broker_order_id] = event
            self._condition.notify_all()

    def wait_for_resolution(
        self, correlation_id: str, broker_order_id: str, timeout_seconds: float
    ) -> OrderUpdateEvent | None:
        self.register(correlation_id, broker_order_id)
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        with self._condition:
            while True:
                event = self._events_by_correlation.get(correlation_id)
                if event is not None and event.status in self._RESOLVING:
                    return event
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)


class _OrderUpdateSocket(Protocol):
    def send(self, message: str) -> None: ...

    def close(self) -> None: ...

    def __enter__(self) -> _OrderUpdateSocket: ...

    def __exit__(self, *args: object) -> None: ...

    def __iter__(self) -> Any: ...


SocketConnector = Callable[..., Any]


class DhanOrderUpdateStream:
    """Reconnectable production transport for Dhan order updates.

    The installed SDK's convenience wrapper prints its authentication payload
    (including the access token) and exposes no bounded stop operation.  This
    narrow transport uses the documented websocket protocol directly, never logs
    payloads or credentials, and can be stopped as part of worker shutdown.
    """

    endpoint = "wss://api-order-update.dhan.co"

    def __init__(
        self,
        *,
        client_id: str,
        access_token: str,
        inbox: OrderUpdateInbox,
        connector: SocketConnector = connect,
        reconnect_seconds: float = 1.0,
    ) -> None:
        self._client_id = client_id
        self._access_token = access_token
        self._consumer = OrderUpdateConsumer(inbox.accept)
        self._connector = connector
        self._reconnect_seconds = reconnect_seconds
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: _OrderUpdateSocket | None = None
        self._socket_lock = threading.Lock()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="dhan-order-updates",
            daemon=True,
        )
        self._thread.start()

    def wait_until_connected(self, timeout_seconds: float) -> bool:
        return self._connected.wait(timeout=max(timeout_seconds, 0.0))

    def stop(self, timeout_seconds: float = 5.0) -> None:
        self._stop.set()
        with self._socket_lock:
            socket = self._socket
        if socket is not None:
            try:
                socket.close()
            except Exception:
                log.exception("could not close the Dhan order-update socket cleanly")
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(timeout_seconds, 0.0))
        self._connected.clear()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._connector(
                    self.endpoint,
                    open_timeout=10,
                    close_timeout=5,
                ) as socket:
                    with self._socket_lock:
                        self._socket = socket
                    socket.send(
                        json.dumps(
                            {
                                "LoginReq": {
                                    "MsgCode": 42,
                                    "ClientId": self._client_id,
                                    "Token": self._access_token,
                                },
                                "UserType": "SELF",
                            }
                        )
                    )
                    self._connected.set()
                    for raw in socket:
                        if self._stop.is_set():
                            break
                        try:
                            message = json.loads(raw)
                            if isinstance(message, dict):
                                self._consumer.handle_raw_message(message)
                        except (TypeError, ValueError):
                            log.warning("ignored malformed Dhan order-update message")
            except Exception:
                if not self._stop.is_set():
                    log.exception("Dhan order-update stream disconnected; reconnecting")
            finally:
                self._connected.clear()
                with self._socket_lock:
                    self._socket = None
            self._stop.wait(max(self._reconnect_seconds, 0.0))


__all__ = [
    "DhanOrderUpdateStream",
    "OrderUpdateConsumer",
    "OrderUpdateEvent",
    "OrderUpdateInbox",
    "parse_order_update_message",
]
