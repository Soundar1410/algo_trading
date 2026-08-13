"""DhanLiveBroker: status mapping, evidence-gated classification, and the
full Broker protocol — exercised entirely against a fake DhanOrderClient.
No real dhanhq SDK object and no network call anywhere in this file
(architecture report §20 / requirement #20)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from common.broker import BrokerError
from common.broker.dhan_live import (
    DhanApiResponse,
    DhanLiveBroker,
    classify_submission_response,
    map_dhan_order_status,
    resolve_unknown_via_lookup,
)
from common.config.models import ExecutionMode
from common.models import OrderIntent, OrderStatus, OrderType, Side


def _intent(
    *,
    correlation_id: str = "l_io_st01_20260813_0001",
    order_type: OrderType = OrderType.MARKET,
    limit_price: float | None = None,
) -> OrderIntent:
    return OrderIntent(
        limit_price=limit_price,
        correlation_id=correlation_id,
        strategy_id="st01",
        runtime_id="intraday_options",
        execution_mode=ExecutionMode.LIVE,
        trading_date="2026-08-13",
        sequence_number=1,
        instrument="NIFTY",
        security_id="49081",
        side=Side.BUY,
        quantity=75,
        order_type=order_type,
        product_type="INTRADAY",
        created_at=datetime.now(UTC),
    )


class _FakeDhanClient:
    """A minimal, fully scripted double for DhanOrderClient — every
    response is exactly what the test hands it, nothing inferred."""

    def __init__(self) -> None:
        _not_set: dict[str, Any] = {"status": "failure", "remarks": "not set", "data": ""}
        self.place_order_response: dict[str, Any] = dict(_not_set)
        self.lookup_response: dict[str, Any] = dict(_not_set)
        self.order_list_response: dict[str, Any] = {"status": "success", "data": []}
        self.positions_response: dict[str, Any] = {"status": "success", "data": []}
        self.modify_response: dict[str, Any] = {"status": "success", "data": {}}
        self.cancel_response: dict[str, Any] = {"status": "success", "data": {}}
        self.place_order_calls: list[dict[str, Any]] = []

    def place_order(self, **kwargs: Any) -> dict[str, Any]:
        self.place_order_calls.append(kwargs)
        return self.place_order_response

    def get_order_by_correlationID(self, correlation_id: str) -> dict[str, Any]:
        return self.lookup_response

    def get_order_by_id(self, order_id: str) -> dict[str, Any]:
        return self.lookup_response

    def get_order_list(self) -> dict[str, Any]:
        return self.order_list_response

    def modify_order(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.modify_response

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self.cancel_response

    def get_positions(self) -> dict[str, Any]:
        return self.positions_response


def _broker(client: _FakeDhanClient | None = None) -> tuple[DhanLiveBroker, _FakeDhanClient]:
    c = client or _FakeDhanClient()
    return DhanLiveBroker(client=c, exchange_segment="NSE_FNO", product_type="INTRADAY"), c


# ------------------------------------------------------------ status mapping
@pytest.mark.parametrize(
    "dhan_status,expected",
    [
        ("TRANSIT", OrderStatus.SUBMITTED),
        ("PENDING", OrderStatus.ACKNOWLEDGED),
        ("PART_TRADED", OrderStatus.PARTIALLY_FILLED),
        ("TRADED", OrderStatus.FILLED),
        ("REJECTED", OrderStatus.REJECTED),
        ("CANCELLED", OrderStatus.CANCELLED),
        ("EXPIRED", OrderStatus.EXPIRED),
        ("transit", OrderStatus.SUBMITTED),  # case-insensitive
    ],
)
def test_dhan_status_mapping_matches_the_documented_vocabulary(dhan_status, expected):
    assert map_dhan_order_status(dhan_status) is expected


def test_an_unrecognised_dhan_status_string_is_classified_unknown():
    """Requirement #28: unknown broker statuses fail closed — never
    interpreted as successful or terminal."""
    assert map_dhan_order_status("SOME_NEW_STATUS_DHAN_ADDS_LATER") is OrderStatus.UNKNOWN


def test_a_missing_or_blank_dhan_status_is_classified_unknown():
    assert map_dhan_order_status(None) is OrderStatus.UNKNOWN
    assert map_dhan_order_status("") is OrderStatus.UNKNOWN


def test_our_own_pending_is_never_the_target_of_the_mapping():
    """Dhan's own PENDING (resting on exchange) must map to ACKNOWLEDGED,
    never to our unrelated pre-submission PENDING."""
    assert map_dhan_order_status("PENDING") is not OrderStatus.PENDING


# ---------------------------------------------- submission-time classification
def test_a_successful_response_with_order_id_and_status_is_classified_directly():
    intent = _intent()
    response = DhanApiResponse.from_raw(
        {
            "status": "success",
            "remarks": "",
            "data": {"orderId": "112111182045", "orderStatus": "TRANSIT"},
        }
    )
    order = classify_submission_response(response, intent=intent)
    assert order.status is OrderStatus.SUBMITTED
    assert order.broker_order_id == "112111182045"
    assert order.correlation_id == intent.correlation_id


def test_a_successful_response_with_order_id_but_no_status_is_submitted_not_unknown():
    """A confirmed orderId is real evidence an order exists — SUBMITTED,
    not UNKNOWN, even without a granular status yet."""
    intent = _intent()
    response = DhanApiResponse.from_raw(
        {"status": "success", "remarks": "", "data": {"orderId": "112111182045"}}
    )
    order = classify_submission_response(response, intent=intent)
    assert order.status is OrderStatus.SUBMITTED
    assert order.broker_order_id == "112111182045"


def test_a_success_status_with_no_order_id_at_all_is_unknown_not_trusted():
    """An unexpected shape on a nominal success is not treated as evidence
    of anything — classified UNKNOWN rather than guessed."""
    intent = _intent()
    response = DhanApiResponse.from_raw({"status": "success", "remarks": "", "data": {}})
    order = classify_submission_response(response, intent=intent)
    assert order.status is OrderStatus.UNKNOWN
    assert order.broker_order_id is None


def test_a_failure_with_dict_remarks_is_classified_unknown_not_rejected():
    """The corrected design: a dict-shaped remarks (a real HTTP error body)
    is NOT trusted as proof of rejection — only a positive lookup is."""
    intent = _intent()
    response = DhanApiResponse.from_raw(
        {
            "status": "failure",
            "remarks": {
                "error_code": "DH-905",
                "error_type": "Order Error",
                "error_message": "Insufficient funds",
            },
            "data": "",
        }
    )
    order = classify_submission_response(response, intent=intent)
    assert order.status is OrderStatus.UNKNOWN
    assert order.broker_order_id is None


def test_a_failure_with_string_remarks_is_also_classified_unknown():
    """A genuine transport exception — the shape dict-vs-string does not
    change the outcome; both are UNKNOWN."""
    intent = _intent()
    response = DhanApiResponse.from_raw(
        {"status": "failure", "remarks": "ConnectionError: timed out", "data": ""}
    )
    order = classify_submission_response(response, intent=intent)
    assert order.status is OrderStatus.UNKNOWN


def test_no_secret_or_raw_broker_response_reaches_the_log(caplog: pytest.LogCaptureFixture):
    """Requirement #17/#27: a failure log must not echo raw remarks, which
    may carry account/order details."""
    intent = _intent()
    response = DhanApiResponse.from_raw(
        {
            "status": "failure",
            "remarks": {"error_message": "some detail that should not be echoed verbatim"},
            "data": "",
        }
    )
    with caplog.at_level(logging.WARNING):
        classify_submission_response(response, intent=intent)
    for record in caplog.records:
        assert "should not be echoed verbatim" not in record.getMessage()


# --------------------------------------------------------- UNKNOWN resolution
def test_resolve_unknown_via_lookup_classifies_rejected_only_from_a_positive_confirmation():
    intent = _intent()
    response = DhanApiResponse.from_raw(
        {
            "status": "success",
            "remarks": "",
            "data": {"orderId": "112111182045", "orderStatus": "REJECTED"},
        }
    )
    order = resolve_unknown_via_lookup(response, intent=intent)
    assert order.status is OrderStatus.REJECTED


def test_resolve_unknown_via_lookup_stays_unknown_when_the_lookup_finds_nothing():
    """Absence of evidence is not evidence of absence — a lookup that
    fails to find the order does NOT authorize concluding REJECTED."""
    intent = _intent()
    response = DhanApiResponse.from_raw({"status": "failure", "remarks": "not found", "data": ""})
    order = resolve_unknown_via_lookup(response, intent=intent)
    assert order.status is OrderStatus.UNKNOWN


def test_resolve_unknown_via_lookup_stays_unknown_on_a_successful_but_empty_lookup():
    intent = _intent()
    response = DhanApiResponse.from_raw({"status": "success", "remarks": "", "data": {}})
    order = resolve_unknown_via_lookup(response, intent=intent)
    assert order.status is OrderStatus.UNKNOWN


# ---------------------------------------------------------------- submit()
def test_submit_passes_the_correlation_id_as_the_tag():
    broker, client = _broker()
    client.place_order_response = {
        "status": "success",
        "remarks": "",
        "data": {"orderId": "1", "orderStatus": "TRANSIT"},
    }
    intent = _intent()

    broker.submit(intent, quote=None)  # type: ignore[arg-type]

    assert client.place_order_calls[0]["tag"] == intent.correlation_id


def test_submit_a_limit_order_without_a_limit_price_raises_before_any_network_call():
    broker, client = _broker()
    intent = _intent(order_type=OrderType.LIMIT, limit_price=None)

    with pytest.raises(BrokerError, match="LIMIT"):
        broker.submit(intent, quote=None)  # type: ignore[arg-type]

    assert client.place_order_calls == []


def test_submit_remembers_the_broker_order_id_for_later_modify_cancel():
    broker, client = _broker()
    client.place_order_response = {
        "status": "success",
        "remarks": "",
        "data": {"orderId": "998877", "orderStatus": "TRANSIT"},
    }
    intent = _intent()
    broker.submit(intent, quote=None)  # type: ignore[arg-type]

    order = broker.cancel(intent.correlation_id)
    assert order.broker_order_id == "998877"


# ------------------------------------------------------------------- cancel
def test_cancel_an_unknown_correlation_id_raises():
    broker, _ = _broker()
    with pytest.raises(BrokerError, match="no known Dhan order id"):
        broker.cancel("l_io_st01_20260813_9999")


def test_cancel_success_reports_cancelled():
    broker, client = _broker()
    client.place_order_response = {
        "status": "success",
        "remarks": "",
        "data": {"orderId": "1", "orderStatus": "TRANSIT"},
    }
    intent = _intent()
    broker.submit(intent, quote=None)  # type: ignore[arg-type]
    client.cancel_response = {"status": "success", "remarks": "", "data": {}}

    order = broker.cancel(intent.correlation_id)
    assert order.status is OrderStatus.CANCELLED


def test_cancel_an_ambiguous_result_is_not_trusted_as_cancelled():
    """An ambiguous cancel outcome (spec: same discipline as submit) must
    not be assumed successful."""
    broker, client = _broker()
    client.place_order_response = {
        "status": "success",
        "remarks": "",
        "data": {"orderId": "1", "orderStatus": "TRANSIT"},
    }
    intent = _intent()
    broker.submit(intent, quote=None)  # type: ignore[arg-type]
    client.cancel_response = {"status": "failure", "remarks": "timeout", "data": ""}

    order = broker.cancel(intent.correlation_id)
    assert order.status is OrderStatus.UNKNOWN


# ------------------------------------------------------------------- modify
def test_modify_an_unknown_correlation_id_raises():
    broker, _ = _broker()
    with pytest.raises(BrokerError, match="no known Dhan order id"):
        broker.modify("l_io_st01_20260813_9999", quantity=10)


def test_modify_failure_raises():
    broker, client = _broker()
    client.place_order_response = {
        "status": "success",
        "remarks": "",
        "data": {"orderId": "1", "orderStatus": "TRANSIT"},
    }
    intent = _intent()
    broker.submit(intent, quote=None)  # type: ignore[arg-type]
    client.modify_response = {"status": "failure", "remarks": "rejected", "data": ""}

    with pytest.raises(BrokerError):
        broker.modify(intent.correlation_id, quantity=25)


# ---------------------------------------------------------- reconciliation reads
def test_fetch_order_book_maps_every_row():
    broker, client = _broker()
    client.order_list_response = {
        "status": "success",
        "data": [
            {"correlationId": "l_io_st01_20260813_0001", "orderId": "1", "orderStatus": "TRANSIT"},
            {"correlationId": "l_io_st01_20260813_0002", "orderId": "2", "orderStatus": "TRADED"},
        ],
    }
    book = broker.fetch_order_book()
    assert len(book) == 2
    assert {o.status for o in book} == {OrderStatus.SUBMITTED, OrderStatus.FILLED}


def test_fetch_order_book_skips_rows_missing_identity():
    broker, client = _broker()
    client.order_list_response = {
        "status": "success",
        "data": [{"correlationId": None, "orderId": "1", "orderStatus": "TRANSIT"}],
    }
    assert broker.fetch_order_book() == ()


def test_fetch_order_book_is_empty_on_failure():
    broker, client = _broker()
    client.order_list_response = {"status": "failure", "remarks": "down", "data": ""}
    assert broker.fetch_order_book() == ()


def test_fetch_trades_returns_empty_and_warns_rather_than_guessing(
    caplog: pytest.LogCaptureFixture,
):
    broker, _ = _broker()
    with caplog.at_level(logging.WARNING):
        trades = broker.fetch_trades()
    assert trades == ()
    assert any("no confirmed SDK method" in r.getMessage() for r in caplog.records)


def test_fetch_positions_maps_every_row():
    broker, client = _broker()
    client.positions_response = {
        "status": "success",
        "data": [
            {"securityId": "49081", "netQty": 75, "costPrice": 190.5, "productType": "INTRADAY"}
        ],
    }
    positions = broker.fetch_positions()
    assert len(positions) == 1
    assert positions[0].security_id == "49081"
    assert positions[0].quantity == 75


def test_fetch_positions_is_empty_on_failure():
    broker, client = _broker()
    client.positions_response = {"status": "failure", "remarks": "down", "data": ""}
    assert broker.fetch_positions() == ()


# ------------------------------------------------------------------ health
def test_is_healthy_reflects_order_list_success():
    broker, client = _broker()
    client.order_list_response = {"status": "success", "data": []}
    assert broker.is_healthy() is True


def test_is_healthy_false_on_failure():
    broker, client = _broker()
    client.order_list_response = {"status": "failure", "remarks": "down", "data": ""}
    assert broker.is_healthy() is False


def test_broker_name_identifies_itself():
    broker, _ = _broker()
    assert broker.name == "dhan_live"


# ------------------------------------------------------------- order_by_correlation_id
def test_order_by_correlation_id_returns_none_on_failure():
    broker, client = _broker()
    client.lookup_response = {"status": "failure", "remarks": "not found", "data": ""}
    assert broker.order_by_correlation_id("l_io_st01_20260813_0001") is None


def test_order_by_correlation_id_returns_the_mapped_order_on_success():
    broker, client = _broker()
    client.lookup_response = {
        "status": "success",
        "remarks": "",
        "data": {"orderId": "1", "orderStatus": "TRADED"},
    }
    order = broker.order_by_correlation_id("l_io_st01_20260813_0001")
    assert order is not None
    assert order.status is OrderStatus.FILLED
