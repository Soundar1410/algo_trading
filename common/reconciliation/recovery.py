"""Idempotent broker-evidence recovery for controlled-live reconciliation.

The broker snapshot is read-only evidence.  It may repair local state only
when an existing local intent provides the complete strategy/instrument/side
identity and the broker order/trade quantities agree.  Ambiguous evidence is
returned as a critical mismatch; it is never guessed or silently discarded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from common.config.models import ExecutionMode
from common.models import Fill, Order, OrderStatus, Side

from .compare import LocalOrderState, LocalPositionState, Mismatch
from .policies import resolution_is_permitted
from .snapshot import BrokerSnapshot

if TYPE_CHECKING:
    from common.execution.repository import ExecutionRepository


@dataclass(frozen=True, slots=True)
class RecoveryResolution:
    category: str
    action: str
    reason: str
    correlation_id: str
    broker_order_id: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    mismatches: tuple[Mismatch, ...]
    resolutions: tuple[RecoveryResolution, ...]


@dataclass(frozen=True, slots=True)
class _FillApplication:
    raw_fill: Fill
    order_id: int
    order_status: OrderStatus
    instrument: str
    security_id: str
    side: Side
    trading_date: str


def recover_broker_evidence(
    repository: ExecutionRepository,
    *,
    runtime_id: str,
    strategy_id: str,
    snapshot: BrokerSnapshot,
) -> RecoveryOutcome:
    """Apply complete broker-confirmed outcomes for known local intents.

    Replaying this function is safe: order persistence is an upsert and fills
    are unique on ``(order_id, broker_fill_id)``.  A crash at any point can
    therefore resume without submitting or applying anything twice.
    """
    mismatches: list[Mismatch] = []
    resolutions: list[RecoveryResolution] = []
    fill_applications: list[_FillApplication] = []
    order_groups: dict[str, list[Order]] = {}
    for order in snapshot.orders:
        order_groups.setdefault(order.correlation_id, []).append(order)

    fill_groups: dict[str, list[Fill]] = {}
    for fill in snapshot.trades:
        fill_groups.setdefault(fill.correlation_id, []).append(fill)

    for correlation_id in fill_groups:
        if correlation_id not in order_groups:
            mismatches.append(
                Mismatch(
                    category="BROKER_ONLY",
                    is_critical=True,
                    correlation_id=correlation_id,
                    detail="broker trade has no corresponding broker order-book row",
                )
            )

    conn = repository.database.connect()
    for correlation_id, orders in order_groups.items():
        if len(orders) != 1:
            # compare_orders persists the duplicate-correlation mismatch.
            continue
        broker_order = orders[0]
        intent = conn.execute(
            "SELECT * FROM order_intents WHERE correlation_id = ? AND runtime_id = ? "
            "AND strategy_id = ? AND execution_mode = 'live'",
            (correlation_id, runtime_id, strategy_id),
        ).fetchone()
        if intent is None:
            # compare_orders classifies the broker-only identity.
            continue

        raw_fills = fill_groups.get(correlation_id, [])
        unique_fills: dict[str, Fill] = {}
        conflicting_fill = False
        for fill in raw_fills:
            existing_fill = unique_fills.get(fill.broker_fill_id)
            if existing_fill is None:
                unique_fills[fill.broker_fill_id] = fill
                continue
            if (
                existing_fill.quantity != fill.quantity
                or existing_fill.price != fill.price
                or existing_fill.filled_at != fill.filled_at
            ):
                conflicting_fill = True
        if conflicting_fill:
            mismatches.append(
                Mismatch(
                    category="QUANTITY_MISMATCH",
                    is_critical=True,
                    correlation_id=correlation_id,
                    broker_order_id=broker_order.broker_order_id,
                    detail="broker returned conflicting rows for one broker_fill_id",
                )
            )
            continue

        confirmed_fills = tuple(unique_fills.values())
        traded_quantity = sum(fill.quantity for fill in confirmed_fills)
        requested_quantity = int(intent["quantity"])
        if traded_quantity != broker_order.filled_quantity:
            mismatches.append(
                Mismatch(
                    category="QUANTITY_MISMATCH",
                    is_critical=True,
                    correlation_id=correlation_id,
                    broker_order_id=broker_order.broker_order_id,
                    local_quantity=traded_quantity,
                    broker_quantity=broker_order.filled_quantity,
                    detail=(
                        "broker trade-book quantity does not equal the broker order-book "
                        "filled quantity; local recovery is refused"
                    ),
                )
            )
            continue
        if traded_quantity > requested_quantity or (
            broker_order.status is OrderStatus.FILLED and traded_quantity != requested_quantity
        ):
            mismatches.append(
                Mismatch(
                    category="QUANTITY_MISMATCH",
                    is_critical=True,
                    correlation_id=correlation_id,
                    broker_order_id=broker_order.broker_order_id,
                    local_quantity=requested_quantity,
                    broker_quantity=traded_quantity,
                    detail="broker-confirmed fill quantity is incompatible with the local intent",
                )
            )
            continue

        local_order = conn.execute(
            "SELECT * FROM orders WHERE correlation_id = ?", (correlation_id,)
        ).fetchone()
        if local_order is not None:
            local_status = OrderStatus(str(local_order["status"]))
            if (
                local_status.is_terminal
                and broker_order.status.is_terminal
                and local_status is not broker_order.status
            ):
                mismatches.append(
                    Mismatch(
                        category="UNKNOWN_ORDER",
                        is_critical=True,
                        correlation_id=correlation_id,
                        broker_order_id=broker_order.broker_order_id,
                        detail=(
                            f"terminal local status {local_status.value} conflicts with "
                            f"terminal broker status {broker_order.status.value}"
                        ),
                    )
                )
                continue

        if local_order is None and not resolution_is_permitted(
            "LOCAL_ONLY", "adopt_broker_order"
        ):
            raise RuntimeError("reconciliation policy forbids adopting a broker order")
        if (
            local_order is not None
            and broker_order.status is OrderStatus.REJECTED
            and not resolution_is_permitted(
                "UNKNOWN_ORDER", "mark_rejected", broker_status=broker_order.status
            )
        ):
            raise RuntimeError("reconciliation policy forbids marking an order rejected")

        authoritative_order = Order(
            correlation_id=correlation_id,
            strategy_id=strategy_id,
            execution_mode=ExecutionMode.LIVE,
            status=broker_order.status,
            updated_at=broker_order.updated_at,
            broker_order_id=broker_order.broker_order_id,
            filled_quantity=broker_order.filled_quantity,
            average_fill_price=broker_order.average_fill_price,
            rejection_reason=broker_order.rejection_reason,
        )
        order_id = repository.record_submission(
            intent_id=int(intent["id"]), order=authoritative_order, runtime_id=runtime_id
        )
        if local_order is None:
            resolutions.append(
                RecoveryResolution(
                    category="LOCAL_ONLY",
                    action="adopt_broker_order",
                    reason="adopted broker order using the existing persisted correlation ID",
                    correlation_id=correlation_id,
                    broker_order_id=broker_order.broker_order_id,
                )
            )
        elif broker_order.status is OrderStatus.REJECTED:
            resolutions.append(
                RecoveryResolution(
                    category="UNKNOWN_ORDER",
                    action="mark_rejected",
                    reason="broker order book positively confirmed rejection",
                    correlation_id=correlation_id,
                    broker_order_id=broker_order.broker_order_id,
                )
            )

        side = Side(str(intent["side"]))
        for raw_fill in sorted(
            confirmed_fills, key=lambda value: (value.filled_at, value.broker_fill_id)
        ):
            fill_applications.append(
                _FillApplication(
                    raw_fill=raw_fill,
                    order_id=order_id,
                    order_status=broker_order.status,
                    instrument=str(intent["instrument"]),
                    security_id=str(intent["security_id"]),
                    side=side,
                    trading_date=str(intent["trading_date"]),
                )
            )
        if confirmed_fills:
            if not resolution_is_permitted(
                "QUANTITY_MISMATCH", "update_traded_quantity"
            ):
                raise RuntimeError("reconciliation policy forbids applying traded quantity")
            resolutions.append(
                RecoveryResolution(
                    category="QUANTITY_MISMATCH",
                    action="update_traded_quantity",
                    reason=(
                        f"idempotently applied {len(confirmed_fills)} broker-confirmed fill(s) "
                        f"totalling {traded_quantity}"
                    ),
                    correlation_id=correlation_id,
                    broker_order_id=broker_order.broker_order_id,
                )
            )

    # Broker order-book ordering is not a lifecycle guarantee. Apply every
    # validated fill in exchange-time order so an exit can never be replayed
    # before its entry merely because the provider returned rows newest-first.
    for application in sorted(
        fill_applications,
        key=lambda value: (value.raw_fill.filled_at, value.raw_fill.broker_fill_id),
    ):
        raw_fill = application.raw_fill
        fill = Fill(
            correlation_id=raw_fill.correlation_id,
            broker_fill_id=raw_fill.broker_fill_id,
            strategy_id=strategy_id,
            execution_mode=ExecutionMode.LIVE,
            quantity=raw_fill.quantity,
            price=raw_fill.price,
            filled_at=raw_fill.filled_at,
            reference_price=raw_fill.reference_price,
            slippage_amount=raw_fill.slippage_amount,
            latency_ms=raw_fill.latency_ms,
            fill_method=raw_fill.fill_method,
            charges=raw_fill.charges,
            latency_applied=raw_fill.latency_applied,
            quote_bid=raw_fill.quote_bid,
            quote_ask=raw_fill.quote_ask,
            quote_age_ms=raw_fill.quote_age_ms,
        )
        repository.apply_fill(
            order_id=application.order_id,
            runtime_id=runtime_id,
            fill=fill,
            order_status=application.order_status,
            instrument=application.instrument,
            security_id=application.security_id,
            side=application.side,
            trading_date=application.trading_date,
        )

    return RecoveryOutcome(tuple(mismatches), tuple(resolutions))


def load_local_reconciliation_state(
    repository: ExecutionRepository, *, strategy_id: str
) -> tuple[list[LocalOrderState], list[LocalPositionState]]:
    """Reload local state after recovery so comparison never uses stale input."""
    orders = [
        LocalOrderState(
            correlation_id=str(row["correlation_id"]),
            status=OrderStatus(str(row["status"])),
            broker_order_id=(
                str(row["broker_order_id"]) if row["broker_order_id"] is not None else None
            ),
            filled_quantity=int(row["filled_quantity"]),
        )
        for row in repository.all_orders(strategy_id=strategy_id, execution_mode=ExecutionMode.LIVE)
    ]
    pending = repository.database.connect().execute(
        "SELECT oi.correlation_id FROM order_intents oi "
        "LEFT JOIN orders o ON o.intent_id = oi.id "
        "WHERE oi.strategy_id = ? AND oi.execution_mode = 'live' AND o.id IS NULL",
        (strategy_id,),
    )
    orders.extend(
        LocalOrderState(correlation_id=str(row["correlation_id"]), status=OrderStatus.UNKNOWN)
        for row in pending
    )
    positions = [
        LocalPositionState(
            security_id=position.security_id,
            quantity=position.quantity,
            average_price=position.average_price,
            product_type="INTRADAY",
            status=position.status.value,
        )
        for position in repository.positions_all_dates(
            strategy_id=strategy_id, execution_mode=ExecutionMode.LIVE
        )
    ]
    return orders, positions
