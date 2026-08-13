"""Order-update websocket message parsing — no real socket anywhere here."""

from __future__ import annotations

from common.broker.order_update_consumer import (
    OrderUpdateConsumer,
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
