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


#: The reference repository's name for the same enum, kept as an **alias** rather
#: than a second type (Phase 3 Part 2a). Its members and values are identical
#: (``BUY``/``SELL``), and the ported exit engines compare by identity —
#: ``position.side is OrderSide.BUY``. Two distinct enums would make that check
#: silently False against this repository's own models, which is the kind of bug
#: that produces a wrong exit rather than an error.
OrderSide = Side


class OptionType(StrEnum):
    """Option right. Ported from the reference's ``framework/core/models.py``."""

    CE = "CE"  # Call
    PE = "PE"  # Put


class ExitReason(StrEnum):
    """Why a position was closed (drives reporting and logs).

    Ported verbatim from the reference repository, including the members this
    repository has no consumer for yet: the exit engines set them, and dropping
    the ones that look unused today would silently change what a ported engine
    reports. ``StrEnum`` rather than the reference's ``str, Enum`` to match this
    repository's convention — same runtime behaviour for comparison and
    serialisation.
    """

    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    TARGET_PROFIT = "TARGET_PROFIT"
    OPPOSITE_SIGNAL = "OPPOSITE_SIGNAL"
    STRATEGY_EXIT = "STRATEGY_EXIT"
    SQUARE_OFF = "SQUARE_OFF"
    MANUAL = "MANUAL"
    # Multi-leg/basket strategies: a leg-wise stop loss reuses STOP_LOSS; these
    # two are basket-level outcomes with no single-leg equivalent above.
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    ADJUSTMENT = "ADJUSTMENT"
    # MOMENTUM_LOW_OR_HIGHEST_CLOSE combined candle-structure exit (see
    # common.exit.combined_candle_exit): granular reasons so a report/log can
    # tell which rule(s) fired without re-deriving it from raw candle data.
    MOMENTUM_LOW = "MOMENTUM_LOW"
    MOMENTUM_HIGH = "MOMENTUM_HIGH"
    HIGHEST_CLOSE_TRAIL = "HIGHEST_CLOSE_TRAIL"
    LOWEST_CLOSE_TRAIL = "LOWEST_CLOSE_TRAIL"
    MOMENTUM_AND_TRAIL = "MOMENTUM_AND_TRAIL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(StrEnum):
    """Internal order lifecycle (spec section 2 of the execution architecture).

    ``UNKNOWN`` is not a failure state. It means a submission timed out and the
    order's real fate must be established by querying the broker with the
    correlation ID — never by issuing a second order.

    ``EXPIRED`` is Phase 10: Dhan documents ``EXPIRED`` as a real terminal
    ``orderStatus`` value, distinct from both ``REJECTED`` and ``CANCELLED``
    (a DAY-validity order that never filled before market close) —
    verified against ``https://dhanhq.co/docs/v2/orders/`` rather than
    folded into ``CANCELLED`` to avoid a schema change, per the corrected
    Phase 10 design. Our own ``PENDING`` predates any Dhan order existing at
    all (a locally-reserved, not-yet-submitted intent) and is never the
    target of a Dhan-status mapping — Dhan's own "PENDING" (order resting on
    the exchange) maps to ``ACKNOWLEDGED`` instead; see
    ``common.broker.dhan_live.map_dhan_order_status``.
    """

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


#: The shape ``strategy_state.payload`` (and, by extension, everything this
#: build writes into ``strategy_state.state_version``) is written against.
#: Phase 6 Part 3. Lives here rather than in ``common.engine.state_payload``
#: (the natural-looking home, next to the key constants) because
#: ``common.execution.repository`` — the write side — must not import from
#: ``common.engine`` at runtime; Parts 1-2 kept that direction
#: ``TYPE_CHECKING``-only deliberately. Both packages already depend on this
#: leaf module, so it is the one place this constant can live without adding
#: a new coupling in either direction.
CURRENT_STATE_VERSION = 1


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
    #: Whether the simulated latency actually selected a later quote, or the fill
    #: fell back to the submission quote (Phase 4 Part 5, deviation D48). Recorded
    #: per fill rather than asserted per configuration, because on a live feed the
    #: post-latency quote does not exist yet at submission time. ``None`` for any
    #: fill that did not come from the simulator.
    latency_applied: bool | None = None
    #: The submission-time quote the fill was priced against — spec section 6's
    #: "record the submission-time quote". ``None`` on either side means the book
    #: had no resting order there, which is why the fill fell back to last price.
    quote_bid: float | None = None
    quote_ask: float | None = None
    #: How old that quote was when the fill was priced. ``None`` when the quote's
    #: exchange timestamp is ahead of our clock, which is ordinary on a replay.
    quote_age_ms: float | None = None

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
