"""Execution-domain value types.

The spec requires that ``PaperBroker`` and the future ``DhanLiveBroker`` return
the *same* internal order and fill models. These are those models. If a live
adapter ever needed its own variant, paper forward-testing would stop being
evidence about live behaviour — so the types live here, above both adapters, and
neither is allowed to widen them.

Every trading type carries ``strategy_id`` and ``execution_mode``. That is
deliberate duplication of what the config already knows: a fill in flight, in a
queue, or in a log line must be self-describing, because the most dangerous
question during an incident is "was that paper or real?".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from common.config.models import ExecutionMode

from .market import Candle


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        """+1 for BUY, -1 for SELL — position arithmetic in one place."""
        return 1 if self is Side.BUY else -1


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(StrEnum):
    """Internal order lifecycle (spec section 2 of the execution architecture).

    ``UNKNOWN`` is not a failure state. It means a submission timed out and the
    order's real fate must be established by querying the broker with the
    correlation ID — never by issuing a second order.
    """

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
        }


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class RiskDecision(StrEnum):
    ALLOWED = "ALLOWED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class Signal:
    """A strategy's decision, bound to the exact candle that produced it.

    The candle is carried by value, not by reference to "the latest bar". The
    spec requires recording the exact snapshot used, and a signal that merely
    points at mutable shared state cannot be audited after the fact.
    """

    strategy_id: str
    execution_mode: ExecutionMode
    instrument: str
    security_id: str
    side: Side
    quantity: int
    candle: Candle
    reference_price: float
    evaluated_at: datetime
    reason: str = ""

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"signal quantity must be positive, got {self.quantity}")


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """A persisted intention to trade, created *before* any broker call.

    The correlation ID is assigned here and never regenerated. On a submission
    timeout the same ID is used to query the broker; issuing a fresh one would
    risk a duplicate real order.
    """

    correlation_id: str
    strategy_id: str
    runtime_id: str
    execution_mode: ExecutionMode
    trading_date: str
    sequence_number: int
    instrument: str
    security_id: str
    side: Side
    quantity: int
    order_type: OrderType
    product_type: str
    created_at: datetime
    limit_price: float | None = None
    trigger_price: float | None = None
    signal_id: int | None = None
    basket_id: str | None = None
    leg_id: str | None = None
    config_fingerprint: str | None = None
    risk_decision: RiskDecision = RiskDecision.ALLOWED
    risk_reason: str | None = None

    @property
    def correlation_namespace(self) -> str:
        return self.execution_mode.value


@dataclass(frozen=True, slots=True)
class Fill:
    """One execution against an order.

    ``broker_fill_id`` is the idempotency key: replaying the same fill event
    must not move the position twice. For paper fills the provenance fields
    below are what make a simulated P&L auditable rather than merely plausible.
    """

    correlation_id: str
    broker_fill_id: str
    strategy_id: str
    execution_mode: ExecutionMode
    quantity: int
    price: float
    filled_at: datetime
    reference_price: float | None = None
    slippage_amount: float | None = None
    latency_ms: int | None = None
    fill_method: str | None = None
    charges: float = 0.0

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"fill quantity must be positive, got {self.quantity}")
        if self.price <= 0:
            raise ValueError(f"fill price must be positive, got {self.price}")


@dataclass(frozen=True, slots=True)
class Order:
    """Broker-side state of one intent. Returned identically by every adapter."""

    correlation_id: str
    strategy_id: str
    execution_mode: ExecutionMode
    status: OrderStatus
    updated_at: datetime
    broker_order_id: str | None = None
    filled_quantity: int = 0
    average_fill_price: float | None = None
    rejection_reason: str | None = None
    fills: tuple[Fill, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Position:
    """Net exposure for one strategy, mode, instrument and trading date.

    ``trading_date`` is part of the identity because intraday state must never
    leak across a day boundary — a restart on Tuesday must not adopt Monday's
    open position (spec section 12).
    """

    strategy_id: str
    execution_mode: ExecutionMode
    trading_date: str
    instrument: str
    security_id: str
    quantity: int
    average_price: float
    status: PositionStatus
    opened_at: datetime
    updated_at: datetime
    entry_correlation_id: str | None = None
    stop_price: float | None = None
    target_price: float | None = None
    highest_favourable: float | None = None
    lowest_favourable: float | None = None
    realised_pnl: float = 0.0
    charges: float = 0.0
    closed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.status is PositionStatus.OPEN and self.quantity != 0
