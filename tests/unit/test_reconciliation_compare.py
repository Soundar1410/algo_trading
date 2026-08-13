"""Mismatch taxonomy: every category produced by its matching fixture,
critical/informational classification, and the explicit price tolerance."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from common.broker.base import BrokerPosition
from common.models import Order, OrderStatus
from common.reconciliation import (
    CRITICAL_CATEGORIES,
    LocalOrderState,
    LocalPositionState,
    compare_orders,
    compare_positions,
)


def _order(correlation_id: str, status: OrderStatus, broker_order_id: str | None = "b1") -> Order:
    return Order(
        correlation_id=correlation_id,
        strategy_id="st01",
        execution_mode=None,  # type: ignore[arg-type]
        status=status,
        updated_at=datetime.now(UTC),
        broker_order_id=broker_order_id,
    )


# ------------------------------------------------------------------- orders
def test_matching_orders_produce_no_mismatch():
    local = [LocalOrderState(correlation_id="c1", status=OrderStatus.FILLED)]
    broker = [_order("c1", OrderStatus.FILLED)]
    assert compare_orders(local, broker) == []


def test_broker_only_order_is_flagged_critical():
    local: list[LocalOrderState] = []
    broker = [_order("c1", OrderStatus.ACKNOWLEDGED)]
    mismatches = compare_orders(local, broker)
    assert len(mismatches) == 1
    assert mismatches[0].category == "BROKER_ONLY"
    assert mismatches[0].is_critical


def test_local_only_non_terminal_order_is_flagged_not_critical():
    local = [LocalOrderState(correlation_id="c1", status=OrderStatus.SUBMITTED)]
    mismatches = compare_orders(local, [])
    assert len(mismatches) == 1
    assert mismatches[0].category == "LOCAL_ONLY"
    assert not mismatches[0].is_critical


def test_local_only_terminal_order_is_not_flagged_at_all():
    """A closed local order needs no broker confirmation to stay closed."""
    local = [LocalOrderState(correlation_id="c1", status=OrderStatus.FILLED)]
    assert compare_orders(local, []) == []


def test_local_unknown_order_with_no_broker_record_is_flagged_critical():
    local = [LocalOrderState(correlation_id="c1", status=OrderStatus.UNKNOWN)]
    mismatches = compare_orders(local, [])
    assert len(mismatches) == 1
    assert mismatches[0].category == "UNKNOWN_ORDER"
    assert mismatches[0].is_critical


def test_duplicate_local_correlation_id_is_flagged_critical():
    local = [
        LocalOrderState(correlation_id="c1", status=OrderStatus.FILLED),
        LocalOrderState(correlation_id="c1", status=OrderStatus.FILLED),
    ]
    mismatches = compare_orders(local, [])
    assert len(mismatches) == 1
    assert mismatches[0].category == "DUPLICATE_CORRELATION"
    assert mismatches[0].is_critical


# ---------------------------------------------------------------- positions
def _local_position(**overrides) -> LocalPositionState:
    base = dict(
        security_id="sec1",
        quantity=75,
        average_price=190.0,
        product_type="INTRADAY",
        status="OPEN",
    )
    base.update(overrides)
    return LocalPositionState(**base)


def _broker_position(**overrides) -> BrokerPosition:
    base = dict(security_id="sec1", quantity=75, average_price=190.0, product_type="INTRADAY")
    base.update(overrides)
    return BrokerPosition(**base)


def test_matching_positions_are_not_flagged():
    mismatches = compare_positions([_local_position()], [_broker_position()], price_tolerance=0.5)
    assert mismatches == []


def test_local_open_broker_closed_is_not_critical():
    mismatches = compare_positions([_local_position()], [], price_tolerance=0.5)
    assert len(mismatches) == 1
    assert mismatches[0].category == "LOCAL_OPEN_BROKER_CLOSED"
    assert not mismatches[0].is_critical


def test_broker_only_position_is_critical():
    mismatches = compare_positions([], [_broker_position()], price_tolerance=0.5)
    assert len(mismatches) == 1
    assert mismatches[0].category == "BROKER_ONLY"
    assert mismatches[0].is_critical


def test_quantity_mismatch_same_side_is_critical():
    mismatches = compare_positions(
        [_local_position(quantity=75)], [_broker_position(quantity=50)], price_tolerance=0.5
    )
    assert len(mismatches) == 1
    assert mismatches[0].category == "QUANTITY_MISMATCH"
    assert mismatches[0].is_critical


def test_side_mismatch_is_critical():
    mismatches = compare_positions(
        [_local_position(quantity=75)], [_broker_position(quantity=-75)], price_tolerance=0.5
    )
    assert len(mismatches) == 1
    assert mismatches[0].category == "SIDE_MISMATCH"
    assert mismatches[0].is_critical
    assert mismatches[0].local_side == "LONG"
    assert mismatches[0].broker_side == "SHORT"


def test_product_mismatch_is_not_critical():
    mismatches = compare_positions(
        [_local_position(product_type="INTRADAY")],
        [_broker_position(product_type="MARGIN")],
        price_tolerance=0.5,
    )
    assert len(mismatches) == 1
    assert mismatches[0].category == "PRODUCT_MISMATCH"
    assert not mismatches[0].is_critical


def test_price_mismatch_beyond_tolerance_is_not_critical():
    mismatches = compare_positions(
        [_local_position(average_price=190.0)],
        [_broker_position(average_price=192.0)],
        price_tolerance=0.5,
    )
    assert len(mismatches) == 1
    assert mismatches[0].category == "PRICE_MISMATCH"
    assert not mismatches[0].is_critical


def test_price_difference_within_tolerance_is_not_flagged():
    mismatches = compare_positions(
        [_local_position(average_price=190.0)],
        [_broker_position(average_price=190.3)],
        price_tolerance=0.5,
    )
    assert mismatches == []


def test_local_closed_broker_open_is_critical():
    local = [_local_position(status="CLOSED")]
    mismatches = compare_positions(local, [_broker_position()], price_tolerance=0.5)
    categories = {m.category for m in mismatches}
    assert "LOCAL_CLOSED_BROKER_OPEN" in categories
    critical = [m for m in mismatches if m.category == "LOCAL_CLOSED_BROKER_OPEN"]
    assert critical[0].is_critical


def test_critical_categories_match_the_spec_list():
    assert frozenset(
        {
            "BROKER_ONLY",
            "QUANTITY_MISMATCH",
            "SIDE_MISMATCH",
            "UNKNOWN_ORDER",
            "LOCAL_CLOSED_BROKER_OPEN",
            "DUPLICATE_CORRELATION",
        }
    ) == CRITICAL_CATEGORIES


def test_unknown_category_is_rejected_at_construction():
    from common.reconciliation import Mismatch

    with pytest.raises(ValueError, match="unknown mismatch category"):
        Mismatch(category="NOT_A_REAL_CATEGORY", is_critical=False, detail="x")
