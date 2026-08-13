"""Order-update websocket message parsing — no real socket anywhere here."""

from __future__ import annotations

import json
import threading

from common.broker.order_update_consumer import (
    DhanOrderUpdateStream,
    OrderUpdateConsumer,
    OrderUpdateEvent,
    OrderUpdateInbox,
    parse_order_update_message,
)
from common.models import OrderStatus


def test_parses_a_documented_order_alert_message():
    message = {
        "Type": "order_alert",
        "Data": {
            "CorrelationId": "l_io_st01_20260813_0001",
            "OrderNo": "112111182045",
            "Status": "TRADED",
        },
    }
    event = parse_order_update_message(message)
    assert event is not None
    assert event.correlation_id == "l_io_st01_20260813_0001"
    assert event.broker_order_id == "112111182045"
    assert event.status is OrderStatus.FILLED


def test_ignores_a_non_order_alert_message():
    assert parse_order_update_message({"Type": "something_else", "Data": {}}) is None


def test_ignores_a_message_with_no_type_field():
    assert parse_order_update_message({}) is None


def test_handles_a_message_with_malformed_data():
    assert parse_order_update_message({"Type": "order_alert", "Data": "not a dict"}) is None


def test_missing_correlation_id_still_parses_with_none():
    message = {
        "Type": "order_alert",
        "Data": {"OrderNo": "112111182045", "Status": "TRADED"},
    }
    event = parse_order_update_message(message)
    assert event is not None
    assert event.correlation_id is None
    assert event.broker_order_id == "112111182045"


def test_installed_sdk_lower_camel_payload_variant_is_accepted():
    event = parse_order_update_message(
        {
            "Type": "order_alert",
            "Data": {
                "correlationId": "corr",
                "orderNo": "order-1",
                "status": "TRADED",
            },
        }
    )
    assert event is not None
    assert event.correlation_id == "corr"
    assert event.broker_order_id == "order-1"
    assert event.status is OrderStatus.FILLED


def test_an_unrecognised_status_maps_to_unknown_not_terminal():
    message = {
        "Type": "order_alert",
        "Data": {"CorrelationId": "l_io_st01_20260813_0001", "Status": "SOMETHING_NEW"},
    }
    event = parse_order_update_message(message)
    assert event is not None
    assert event.status is OrderStatus.UNKNOWN


def test_consumer_dispatches_recognised_messages_to_the_callback():
    received = []
    consumer = OrderUpdateConsumer(on_event=received.append)
    consumer.handle_raw_message(
        {
            "Type": "order_alert",
            "Data": {"CorrelationId": "l_io_st01_20260813_0001", "Status": "TRANSIT"},
        }
    )
    assert len(received) == 1
    assert received[0].correlation_id == "l_io_st01_20260813_0001"


def test_consumer_does_not_dispatch_unrecognised_messages():
    received = []
    consumer = OrderUpdateConsumer(on_event=received.append)
    consumer.handle_raw_message({"Type": "heartbeat", "Data": {}})
    assert received == []


def test_consumer_still_dispatches_a_message_missing_correlation_id(
    caplog,
):
    """Logged loudly (the fallback path must pick this up), but not dropped."""
    import logging

    received = []
    consumer = OrderUpdateConsumer(on_event=received.append)
    with caplog.at_level(logging.WARNING):
        consumer.handle_raw_message(
            {"Type": "order_alert", "Data": {"OrderNo": "1", "Status": "TRANSIT"}}
        )
    assert len(received) == 1
    assert any("no CorrelationId" in r.getMessage() for r in caplog.records)


def test_inbox_waits_past_acknowledgement_for_a_resolving_update():
    inbox = OrderUpdateInbox()
    inbox.register("corr", "order-1")
    inbox.accept(OrderUpdateEvent("corr", "order-1", OrderStatus.ACKNOWLEDGED, "PENDING"))
    assert inbox.wait_for_resolution("corr", "order-1", 0) is None

    filled = OrderUpdateEvent("corr", "order-1", OrderStatus.FILLED, "TRADED")
    inbox.accept(filled)
    assert inbox.wait_for_resolution("corr", "order-1", 0) == filled


def test_inbox_resolves_missing_correlation_from_registered_order_id():
    inbox = OrderUpdateInbox()
    inbox.register("corr", "order-1")
    filled = OrderUpdateEvent(None, "order-1", OrderStatus.FILLED, "TRADED")
    inbox.accept(filled)
    assert inbox.wait_for_resolution("corr", "order-1", 0) == filled


class _FakeSocket:
    def __init__(self, message: dict) -> None:
        self.message = message
        self.sent: list[str] = []
        self.closed = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def send(self, message: str) -> None:
        self.sent.append(message)

    def close(self) -> None:
        self.closed.set()

    def __iter__(self):
        yield json.dumps(self.message)
        self.closed.wait(2)


def test_production_stream_authenticates_without_logging_or_network(caplog):
    socket = _FakeSocket(
        {
            "Type": "order_alert",
            "Data": {
                "CorrelationId": "corr",
                "OrderNo": "order-1",
                "Status": "TRADED",
            },
        }
    )
    inbox = OrderUpdateInbox()
    stream = DhanOrderUpdateStream(
        client_id="client-secret",
        access_token="token-secret",
        inbox=inbox,
        connector=lambda *_args, **_kwargs: socket,
    )

    stream.start()
    try:
        assert stream.wait_until_connected(1)
        assert inbox.wait_for_resolution("corr", "order-1", 1) is not None
    finally:
        stream.stop()

    auth = json.loads(socket.sent[0])
    assert auth["LoginReq"]["ClientId"] == "client-secret"
    assert auth["LoginReq"]["Token"] == "token-secret"
    assert "client-secret" not in caplog.text
    assert "token-secret" not in caplog.text
