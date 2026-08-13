"""Local-vs-broker comparison: the mismatch taxonomy (spec section 9,
"Future controlled-live mismatch classifications").

Two comparisons, because the two things being reconciled carry different
data: **orders** (identity/status — ``Order`` carries no side/quantity/
security_id of its own, only ``OrderIntent`` does) and **positions**
(quantity/side/price, where the taxonomy's QUANTITY_MISMATCH/SIDE_MISMATCH/
PRODUCT_MISMATCH/PRICE_MISMATCH/LOCAL_OPEN_BROKER_CLOSED/
LOCAL_CLOSED_BROKER_OPEN entries actually apply).

A price difference is informational, never critical, and only within an
*explicit* tolerance (spec: "the tolerance must be explicit") — an
untolerated, arbitrarily large price gap is still worth surfacing but the
tolerance itself is a caller-supplied number, never an implicit default
buried in this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from common.broker.base import BrokerPosition
from common.models import Order, OrderStatus

MISMATCH_CATEGORIES = (
    "MATCHED",
    "LOCAL_ONLY",
    "BROKER_ONLY",
    "QUANTITY_MISMATCH",
    "SIDE_MISMATCH",
    "PRODUCT_MISMATCH",
    "PRICE_MISMATCH",
    "LOCAL_OPEN_BROKER_CLOSED",
    "LOCAL_CLOSED_BROKER_OPEN",
    "UNKNOWN_ORDER",
    "DUPLICATE_CORRELATION",
)

#: Spec's own critical list (2234-2244): "Block new entries for: broker-only
#: open position, quantity/side mismatch, unknown pending order, local
#: closed but broker open, duplicate correlation identity." PRODUCT_MISMATCH
#: and PRICE_MISMATCH are deliberately not critical — informational, per
#: the explicit-tolerance note above.
CRITICAL_CATEGORIES = frozenset(
    {
        "BROKER_ONLY",
        "QUANTITY_MISMATCH",
        "SIDE_MISMATCH",
        "UNKNOWN_ORDER",
        "LOCAL_CLOSED_BROKER_OPEN",
        "DUPLICATE_CORRELATION",
    }
)


@dataclass(frozen=True, slots=True)
class Mismatch:
    category: str
    is_critical: bool
    detail: str
    correlation_id: str | None = None
    broker_order_id: str | None = None
    security_id: str | None = None
    local_quantity: int | None = None
    broker_quantity: int | None = None
    local_side: str | None = None
    broker_side: str | None = None
    local_price: float | None = None
    broker_price: float | None = None

    def __post_init__(self) -> None:
        if self.category not in MISMATCH_CATEGORIES:
            raise ValueError(f"unknown mismatch category {self.category!r}")


@dataclass(frozen=True, slots=True)
class LocalOrderState:
    """Minimal local-order view ``compare_orders`` needs — decoupled from
    the full repository/DB row shape so this module is testable without a
    database."""

    correlation_id: str
    status: OrderStatus
    broker_order_id: str | None = None


@dataclass(frozen=True, slots=True)
class LocalPositionState:
    security_id: str
    quantity: int  # signed: positive long, negative short
    average_price: float
    product_type: str
    status: str  # "OPEN" or "CLOSED"


def compare_orders(
    local_orders: Sequence[LocalOrderState], broker_orders: Sequence[Order]
) -> list[Mismatch]:
    """Order-level comparison: identity and UNKNOWN-status reconciliation.

    A local order in a *terminal* state (FILLED/REJECTED/CANCELLED/EXPIRED)
    with no matching broker record is not flagged — the broker legitimately
    only reports recent/open activity for some endpoints, and a closed
    local order needs no broker confirmation to stay closed. A local
    ``UNKNOWN`` order with no broker record, or any local order with the
    same correlation ID appearing more than once, is always flagged.
    """
    mismatches: list[Mismatch] = []

    local_by_id: dict[str, list[LocalOrderState]] = {}
    for local_order in local_orders:
        local_by_id.setdefault(local_order.correlation_id, []).append(local_order)
    broker_groups: dict[str, list[Order]] = {}
    for broker_order in broker_orders:
        broker_groups.setdefault(broker_order.correlation_id, []).append(broker_order)

    duplicate_broker_ids = {
        correlation_id for correlation_id, orders in broker_groups.items() if len(orders) > 1
    }
    for correlation_id in sorted(duplicate_broker_ids):
        broker_duplicates = broker_groups[correlation_id]
        mismatches.append(
            Mismatch(
                category="DUPLICATE_CORRELATION",
                is_critical=True,
                correlation_id=correlation_id,
                broker_order_id=broker_duplicates[0].broker_order_id,
                detail=(
                    f"{len(broker_duplicates)} broker rows share correlation_id "
                    f"{correlation_id!r}"
                ),
            )
        )

    # Only unique broker identities may participate in ordinary matching.
    # A duplicate is already a critical ambiguity and choosing one row would
    # manufacture an authoritative status that the broker snapshot did not have.
    broker_by_id = {
        correlation_id: orders[0]
        for correlation_id, orders in broker_groups.items()
        if correlation_id not in duplicate_broker_ids
    }

    for correlation_id, locals_ in local_by_id.items():
        if correlation_id in duplicate_broker_ids:
            continue
        if len(locals_) > 1:
            mismatches.append(
                Mismatch(
                    category="DUPLICATE_CORRELATION",
                    is_critical=True,
                    correlation_id=correlation_id,
                    detail=f"{len(locals_)} local rows share correlation_id {correlation_id!r}",
                )
            )
            continue

        local = locals_[0]
        broker = broker_by_id.get(correlation_id)

        if broker is None:
            if local.status is OrderStatus.UNKNOWN:
                mismatches.append(
                    Mismatch(
                        category="UNKNOWN_ORDER",
                        is_critical=True,
                        correlation_id=correlation_id,
                        detail="local order is UNKNOWN and the broker has no matching record",
                    )
                )
            elif not local.status.is_terminal:
                mismatches.append(
                    Mismatch(
                        category="LOCAL_ONLY",
                        is_critical=False,
                        correlation_id=correlation_id,
                        detail=f"local order in non-terminal state {local.status.value} has "
                        "no broker record",
                    )
                )
            # a terminal local order with no broker record is not flagged.

    for correlation_id, broker in broker_by_id.items():
        if correlation_id not in local_by_id:
            mismatches.append(
                Mismatch(
                    category="BROKER_ONLY",
                    is_critical=True,
                    correlation_id=correlation_id,
                    broker_order_id=broker.broker_order_id,
                    detail="broker reports an order this process has no local record of at all",
                )
            )

    return mismatches


def compare_positions(
    local_positions: Sequence[LocalPositionState],
    broker_positions: Sequence[BrokerPosition],
    *,
    price_tolerance: float,
) -> list[Mismatch]:
    """Position-level comparison, keyed by ``security_id``. Broker state is
    authoritative for quantity/existence (spec's broker-authoritative
    principle) — this function only classifies the mismatch, it never
    silently prefers one side's number.
    """
    mismatches: list[Mismatch] = []
    local_open = {p.security_id: p for p in local_positions if p.status == "OPEN"}
    local_closed = {p.security_id: p for p in local_positions if p.status == "CLOSED"}
    broker_by_id = {p.security_id: p for p in broker_positions if p.quantity != 0}

    all_ids = set(local_open) | set(broker_by_id)
    for security_id in all_ids:
        local = local_open.get(security_id)
        broker = broker_by_id.get(security_id)

        if local is not None and broker is None:
            mismatches.append(
                Mismatch(
                    category="LOCAL_OPEN_BROKER_CLOSED",
                    is_critical=False,
                    security_id=security_id,
                    local_quantity=local.quantity,
                    detail="local position is OPEN but the broker reports no such position",
                )
            )
            continue

        if local is None and broker is not None:
            if security_id in local_closed:
                mismatches.append(
                    Mismatch(
                        category="LOCAL_CLOSED_BROKER_OPEN",
                        is_critical=True,
                        security_id=security_id,
                        broker_quantity=broker.quantity,
                        detail=(
                            "local position is recorded CLOSED but the broker still "
                            "reports it open"
                        ),
                    )
                )
                continue
            mismatches.append(
                Mismatch(
                    category="BROKER_ONLY",
                    is_critical=True,
                    security_id=security_id,
                    broker_quantity=broker.quantity,
                    detail="broker reports an open position with no local record at all",
                )
            )
            continue

        assert local is not None and broker is not None
        if local.quantity != broker.quantity:
            if (local.quantity > 0) != (broker.quantity > 0):
                mismatches.append(
                    Mismatch(
                        category="SIDE_MISMATCH",
                        is_critical=True,
                        security_id=security_id,
                        local_quantity=local.quantity,
                        broker_quantity=broker.quantity,
                        local_side="LONG" if local.quantity > 0 else "SHORT",
                        broker_side="LONG" if broker.quantity > 0 else "SHORT",
                        detail="local and broker disagree on which side this position is on",
                    )
                )
            else:
                mismatches.append(
                    Mismatch(
                        category="QUANTITY_MISMATCH",
                        is_critical=True,
                        security_id=security_id,
                        local_quantity=local.quantity,
                        broker_quantity=broker.quantity,
                        detail=(
                            f"local quantity {local.quantity} != "
                            f"broker quantity {broker.quantity}"
                        ),
                    )
                )
            continue

        if local.product_type != broker.product_type:
            mismatches.append(
                Mismatch(
                    category="PRODUCT_MISMATCH",
                    is_critical=False,
                    security_id=security_id,
                    detail=f"local product_type {local.product_type!r} != broker "
                    f"{broker.product_type!r}",
                )
            )
            continue

        price_diff = abs(local.average_price - broker.average_price)
        if price_diff > price_tolerance:
            mismatches.append(
                Mismatch(
                    category="PRICE_MISMATCH",
                    is_critical=False,
                    security_id=security_id,
                    local_price=local.average_price,
                    broker_price=broker.average_price,
                    detail=f"average price differs by {price_diff:.4f}, "
                    f"beyond tolerance {price_tolerance:.4f}",
                )
            )
            continue

    # LOCAL_CLOSED_BROKER_OPEN: a local position that is *recorded* (any
    # trading date) as CLOSED, but the broker still reports it open.
    return mismatches
