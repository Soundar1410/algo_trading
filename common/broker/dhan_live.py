"""``DhanLiveBroker`` — the live order-execution adapter (Phase 10).

Implements the full :class:`~common.broker.base.Broker` protocol against
the installed ``dhanhq==2.2.0`` SDK, narrowed to exactly the calls actually
used (:class:`DhanOrderClient`) so tests exercise this module against a
plain test double — never the real SDK, never a network call.

**Status classification is evidence-gated, not shape-inferred.** An earlier
design classified a ``place_order`` failure as REJECTED when the SDK's
``remarks`` field was a dict, UNKNOWN when it was a string. That heuristic
is unsound: ``dhanhq.dhan_http.DhanHTTP._parse_response`` produces the
*same* string-shaped ``remarks`` both for a genuine transport exception
(no response ever arrived) and for a real HTTP response whose body failed
to parse as JSON (a response *did* arrive, we just cannot read the error
detail from it) — so the shape alone proves nothing about whether an order
was created. The corrected rule below uses only documented, positive
evidence:

* ``status == 'success'`` with a genuine ``orderId`` in the response is the
  only submission-time path that resolves definitively.
* Every ``status == 'failure'`` response — regardless of ``remarks``'
  shape — is always :data:`~common.models.OrderStatus.UNKNOWN` at
  submission time. It is resolved later, and only by a *positive*
  ``get_order_by_correlationID`` confirmation (:func:`resolve_unknown_via_lookup`),
  never inferred from the original failure's shape and never from a lookup
  that merely fails to find anything (absence is not evidence of
  non-existence — broker visibility may be eventually consistent).

Dhan's documented ``orderStatus`` vocabulary (verified directly against
``https://dhanhq.co/docs/v2/orders/``, not invented) is
``TRANSIT, PENDING, REJECTED, CANCELLED, PART_TRADED, TRADED, EXPIRED``.
Anything else — including a missing field — fails closed to ``UNKNOWN``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from common.config.models import RateLimitCallClass
from common.logging import get_logger
from common.models import Fill, Order, OrderIntent, OrderStatus, OrderType
from common.utils.timeutils import get_tz

from .base import Broker, BrokerError, BrokerPosition, Quote
from .paper import InstrumentRulesLookup

log = get_logger(__name__)

#: Dhan's own documented orderStatus values -> our internal OrderStatus.
#: Note the naming collision this deliberately does NOT create: Dhan's own
#: "PENDING" means "resting on the exchange, awaiting execution" and maps to
#: our ACKNOWLEDGED — never our own pre-submission PENDING, which no Dhan
#: response ever reports (it exists before any Dhan order does).
_DHAN_STATUS_MAP: dict[str, OrderStatus] = {
    "TRANSIT": OrderStatus.SUBMITTED,
    "PENDING": OrderStatus.ACKNOWLEDGED,
    "PART_TRADED": OrderStatus.PARTIALLY_FILLED,
    "TRADED": OrderStatus.FILLED,
    "REJECTED": OrderStatus.REJECTED,
    "CANCELLED": OrderStatus.CANCELLED,
    "EXPIRED": OrderStatus.EXPIRED,
}


def map_dhan_order_status(dhan_status: str | None) -> OrderStatus:
    """Dhan's documented vocabulary only. Anything else — missing, blank,
    or a string Dhan has never documented — fails closed to ``UNKNOWN``
    rather than being guessed at or treated as terminal/successful."""
    if not dhan_status:
        return OrderStatus.UNKNOWN
    mapped = _DHAN_STATUS_MAP.get(dhan_status.upper())
    if mapped is None:
        log.warning(
            "unrecognised Dhan orderStatus %r — classified UNKNOWN rather than guessed",
            dhan_status,
        )
        return OrderStatus.UNKNOWN
    return mapped


@dataclass(frozen=True, slots=True)
class DhanApiResponse:
    """The dhanhq SDK's own response envelope
    (``{'status', 'remarks', 'data'}``), typed for classification. Build one
    of these from whatever ``DhanOrderClient`` returned — every dhanhq
    method returns this same shape.
    """

    status: str
    remarks: dict[str, Any] | str | None
    #: A dict for a single-order response (place_order, lookups), a list of
    #: dicts for a collection endpoint (get_order_list, get_positions), or
    #: ``''``/``None`` on failure — dhanhq's own shape, not narrowed further.
    data: dict[str, Any] | list[dict[str, Any]] | str | None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> DhanApiResponse:
        return cls(
            status=raw.get("status", "failure"),
            remarks=raw.get("remarks"),
            data=raw.get("data"),
        )


def _order(intent: OrderIntent, *, status: OrderStatus, broker_order_id: str | None) -> Order:
    return Order(
        correlation_id=intent.correlation_id,
        strategy_id=intent.strategy_id,
        execution_mode=intent.execution_mode,
        status=status,
        updated_at=datetime.now(UTC),
        broker_order_id=broker_order_id,
    )


def _order_from_broker_read(
    correlation_id: str,
    *,
    status: OrderStatus,
    broker_order_id: str | None,
    filled_quantity: int = 0,
    average_fill_price: float | None = None,
    fills: tuple[Fill, ...] = (),
) -> Order:
    """Build an ``Order`` for a broker-side read (lookup/order-book/cancel/
    modify) where — unlike :func:`_order` — there is no original
    ``OrderIntent`` in hand, only the correlation ID Dhan echoed back.

    ``execution_mode`` and ``strategy_id`` are recovered from the
    correlation ID's own structure (``common.execution.correlation``) —
    this broker only ever handles live orders, and the ID's namespace
    prefix and strategy token are self-describing by design. This is
    **best-effort, not authoritative**: reconciliation code must join on
    ``correlation_id`` against local state and treat the local row's
    ``strategy_id`` as the real one, never this field, for exactly the
    reconciliation decisions that matter (spec's broker-authoritative
    principle is about *quantity/existence*, not about which strategy a
    correlation ID's truncated token happens to spell out).
    """
    from common.config.models import ExecutionMode
    from common.execution.correlation import CorrelationIdError, parse_correlation_id

    try:
        parsed = parse_correlation_id(correlation_id)
        strategy_id = parsed.strategy_token
        execution_mode = parsed.execution_mode
    except CorrelationIdError:
        log.warning(
            "order/position with correlation_id=%r does not match our own correlation-ID "
            "format — not one of our orders, or a format we do not recognise",
            correlation_id,
        )
        strategy_id = ""
        execution_mode = ExecutionMode.LIVE
    return Order(
        correlation_id=correlation_id,
        strategy_id=strategy_id,
        execution_mode=execution_mode,
        status=status,
        updated_at=datetime.now(UTC),
        broker_order_id=broker_order_id,
        filled_quantity=filled_quantity,
        average_fill_price=average_fill_price,
        fills=fills,
    )


def classify_submission_response(response: DhanApiResponse, *, intent: OrderIntent) -> Order:
    """Classify a fresh ``place_order`` response. See module docstring for
    why this never infers REJECTED from response shape."""
    if response.status == "success" and isinstance(response.data, dict):
        order_id = response.data.get("orderId")
        if order_id is None:
            log.error(
                "Dhan reported success for correlation_id=%s with no orderId in the "
                "response — an unexpected shape, classified UNKNOWN rather than trusted",
                intent.correlation_id,
            )
            return _order(intent, status=OrderStatus.UNKNOWN, broker_order_id=None)
        dhan_status = response.data.get("orderStatus")
        # A confirmed orderId with no orderStatus field is real evidence an
        # order exists (unlike a bare 'failure') even if we don't yet know
        # its granular state — SUBMITTED, not UNKNOWN, which is reserved for
        # "existence itself is unconfirmed".
        status = map_dhan_order_status(dhan_status) if dhan_status else OrderStatus.SUBMITTED
        return _order(intent, status=status, broker_order_id=str(order_id))

    log.warning(
        "Dhan place_order did not confirm success for correlation_id=%s: status=%r "
        "(remarks omitted from this log line — may echo request/account details)",
        intent.correlation_id,
        response.status,
    )
    return _order(intent, status=OrderStatus.UNKNOWN, broker_order_id=None)


def resolve_unknown_via_lookup(response: DhanApiResponse, *, intent: OrderIntent) -> Order:
    """Resolve an UNKNOWN order using ``get_order_by_correlationID``'s
    response. The *only* path that may ever classify REJECTED — and only
    from a genuine, positive ``orderStatus == 'REJECTED'`` in this lookup's
    own response body. A failed or empty lookup is never treated as proof
    the order does not exist; it stays UNKNOWN for the reconciliation
    runner's own bounded, longer-window policy to resolve.
    """
    if response.status == "success" and isinstance(response.data, dict):
        order_id = response.data.get("orderId")
        dhan_status = response.data.get("orderStatus")
        if order_id is not None and dhan_status:
            return _order(
                intent,
                status=map_dhan_order_status(dhan_status),
                broker_order_id=str(order_id),
            )
    return _order(intent, status=OrderStatus.UNKNOWN, broker_order_id=None)


class LiveCallGuard(Protocol):
    """Fresh-preflight and account-wide rate-limit boundary for one call."""

    def before_call(
        self, call_class: RateLimitCallClass, *, risk_reducing: bool = False
    ) -> None: ...


class LiveOrderUpdates(Protocol):
    """The synchronous handoff exposed by the websocket update inbox."""

    def register(self, correlation_id: str, broker_order_id: str) -> None: ...

    def wait_for_resolution(
        self, correlation_id: str, broker_order_id: str, timeout_seconds: float
    ) -> object | None: ...


class DhanOrderClient(Protocol):
    """The exact ``dhanhq==2.2.0`` SDK surface used by the adapter."""

    def place_order(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_order_by_correlationID(self, correlation_id: str) -> dict[str, Any]: ...

    def get_order_by_id(self, order_id: str) -> dict[str, Any]: ...

    def get_order_list(self) -> dict[str, Any]: ...

    def get_trade_book(self, order_id: str | None = None) -> dict[str, Any]: ...

    def modify_order(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    def cancel_order(self, order_id: str) -> dict[str, Any]: ...

    def get_positions(self) -> dict[str, Any]: ...


def _broker_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise BrokerError("Dhan trade row has no usable exchange/create time")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise BrokerError(f"Dhan trade row has invalid time {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=get_tz())
    return parsed.astimezone(UTC)


@dataclass
class DhanLiveBroker:
    """Validated, guarded adapter for Dhan's live order APIs.

    Every SDK call first crosses ``call_guard``; every broker collection read
    raises on failure or malformed data instead of converting an outage into an
    empty account.  Dhan's correlation id is a lookup key, not documented
    idempotency, so local duplicate suppression remains explicit.
    """

    client: DhanOrderClient
    exchange_segment: str
    product_type: str
    call_guard: LiveCallGuard
    instrument_rules: InstrumentRulesLookup
    max_quantity_lots: int
    order_updates: LiveOrderUpdates | None = None
    settlement_timeout_seconds: float = 5.0
    _known_orders: dict[str, str] = field(default_factory=dict)
    _known_correlations_by_order_id: dict[str, str] = field(default_factory=dict)
    _known_security_ids: dict[str, str] = field(default_factory=dict)
    _known_order_types: dict[str, OrderType] = field(default_factory=dict)
    _submitted_correlations: set[str] = field(default_factory=set)

    @property
    def name(self) -> str:
        return "dhan_live"

    def _before(self, call_class: RateLimitCallClass, *, risk_reducing: bool = False) -> None:
        self.call_guard.before_call(call_class, risk_reducing=risk_reducing)

    def is_healthy(self) -> bool:
        try:
            self._before(RateLimitCallClass.READ)
            response = DhanApiResponse.from_raw(self.client.get_order_list())
        except Exception:
            return False
        return response.status == "success" and isinstance(response.data, list)

    def _remember(
        self,
        correlation_id: str,
        order_id: str,
        *,
        security_id: str | None = None,
        order_type: OrderType | None = None,
    ) -> None:
        self._known_orders[correlation_id] = order_id
        self._known_correlations_by_order_id[order_id] = correlation_id
        if security_id is not None:
            self._known_security_ids[correlation_id] = security_id
        if order_type is not None:
            self._known_order_types[correlation_id] = order_type
        if self.order_updates is not None:
            self.order_updates.register(correlation_id, order_id)

    def _validate_intent(self, intent: OrderIntent) -> None:
        if not self.exchange_segment.strip() or not self.product_type.strip():
            raise BrokerError("live broker has no exchange segment/product type")
        if intent.product_type != self.product_type:
            raise BrokerError(
                f"intent product_type {intent.product_type!r} does not match live broker "
                f"product_type {self.product_type!r}"
            )
        rules = self.instrument_rules(intent.security_id)
        if rules is None or rules.lot_size is None or rules.lot_size <= 0:
            raise BrokerError(
                f"no authoritative lot-size rules for live security_id={intent.security_id!r}"
            )
        if intent.quantity <= 0 or intent.quantity % rules.lot_size:
            raise BrokerError(
                f"live quantity {intent.quantity} is not a positive multiple of lot size "
                f"{rules.lot_size}"
            )
        controlled_max = self.max_quantity_lots * rules.lot_size
        if intent.quantity > controlled_max:
            raise BrokerError(
                f"live quantity {intent.quantity} exceeds the separately-approved "
                f"controlled-live maximum {controlled_max}"
            )
        if rules.max_quantity is not None and intent.quantity > rules.max_quantity:
            raise BrokerError(
                f"live quantity {intent.quantity} exceeds exchange maximum {rules.max_quantity}"
            )
        if intent.order_type is OrderType.LIMIT:
            if intent.limit_price is None or intent.limit_price <= 0:
                raise BrokerError("a live LIMIT order requires a positive limit_price")
            if rules.tick_size is None or rules.tick_size <= 0:
                raise BrokerError("a live LIMIT order requires an authoritative tick size")
            ticks = intent.limit_price / rules.tick_size
            if not math.isclose(ticks, round(ticks), abs_tol=1e-9):
                raise BrokerError(
                    f"limit_price {intent.limit_price} is not on tick size {rules.tick_size}"
                )

    def submit(self, intent: OrderIntent, quote: Quote) -> Order:
        del quote
        self._validate_intent(intent)
        if intent.correlation_id in self._submitted_correlations:
            raise BrokerError(
                f"duplicate live submit refused for correlation_id={intent.correlation_id!r}"
            )
        self._before(
            RateLimitCallClass.NEW_ORDER,
            risk_reducing=intent.risk_reducing,
        )
        # Add immediately before the potentially-ambiguous network call.  A
        # guard refusal proves no submission was attempted and may be retried;
        # any exception/timeout after this point must retain duplicate
        # suppression because the broker outcome may be unknown.
        self._submitted_correlations.add(intent.correlation_id)
        price = intent.limit_price if intent.order_type is OrderType.LIMIT else 0.0
        try:
            raw = self.client.place_order(
                security_id=intent.security_id,
                exchange_segment=self.exchange_segment,
                transaction_type=intent.side.value,
                quantity=intent.quantity,
                order_type=intent.order_type.value,
                product_type=self.product_type,
                price=price,
                trigger_price=intent.trigger_price or 0,
                tag=intent.correlation_id,
            )
            order = classify_submission_response(DhanApiResponse.from_raw(raw), intent=intent)
        except Exception:
            # The SDK normally returns a failure envelope, but an exception here
            # is still an ambiguous post-call outcome.  Preserve UNKNOWN and use
            # the same one-shot correlation recovery below; never tell the
            # lifecycle this was a definite rejection and never resubmit.
            log.exception(
                "Dhan place_order raised after submission began; correlation_id=%s "
                "is UNKNOWN pending correlation lookup",
                intent.correlation_id,
            )
            order = _order(intent, status=OrderStatus.UNKNOWN, broker_order_id=None)
        if order.status is OrderStatus.UNKNOWN and order.broker_order_id is None:
            try:
                recovered = self.order_by_correlation_id(intent.correlation_id)
            except BrokerError:
                recovered = None
            if recovered is not None:
                order = recovered
        if order.broker_order_id is not None:
            self._remember(
                intent.correlation_id,
                order.broker_order_id,
                security_id=intent.security_id,
                order_type=intent.order_type,
            )
        if order.broker_order_id is None:
            return order
        if self.order_updates is not None and not order.status.is_terminal:
            # Websocket is the primary signal.  Its event is still confirmed by
            # the correlation lookup below because fills/prices live in the REST
            # trade book, not in our narrow event model.  A timeout performs this
            # same single lookup as the bounded polling fallback — never submit.
            self.order_updates.wait_for_resolution(
                intent.correlation_id,
                order.broker_order_id,
                self.settlement_timeout_seconds,
            )
            try:
                confirmed = self.order_by_correlation_id(intent.correlation_id)
            except BrokerError:
                return _order(
                    intent,
                    status=OrderStatus.UNKNOWN,
                    broker_order_id=order.broker_order_id,
                )
            if confirmed is not None:
                order = confirmed
        return self._attach_confirmed_fills(intent, order)

    def _attach_confirmed_fills(self, intent: OrderIntent, order: Order) -> Order:
        if order.status not in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}:
            return order
        if order.broker_order_id is None:
            return _order(intent, status=OrderStatus.UNKNOWN, broker_order_id=None)
        try:
            fills = self._fetch_fills_for_order(
                order.broker_order_id, correlation_id=intent.correlation_id
            )
        except BrokerError:
            log.exception(
                "Dhan confirmed execution but its trade details were unavailable; "
                "correlation_id=%s remains UNKNOWN",
                intent.correlation_id,
            )
            return _order(
                intent,
                status=OrderStatus.UNKNOWN,
                broker_order_id=order.broker_order_id,
            )
        if not fills:
            return _order(
                intent,
                status=OrderStatus.UNKNOWN,
                broker_order_id=order.broker_order_id,
            )
        total = sum(fill.quantity for fill in fills)
        average = sum(fill.quantity * fill.price for fill in fills) / total
        return Order(
            correlation_id=intent.correlation_id,
            strategy_id=intent.strategy_id,
            execution_mode=intent.execution_mode,
            status=order.status,
            updated_at=datetime.now(UTC),
            broker_order_id=order.broker_order_id,
            filled_quantity=total,
            average_fill_price=average,
            fills=fills,
        )

    def order_by_correlation_id(self, correlation_id: str) -> Order | None:
        self._before(RateLimitCallClass.READ)
        response = DhanApiResponse.from_raw(self.client.get_order_by_correlationID(correlation_id))
        if response.status != "success":
            raise BrokerError(
                f"Dhan correlation lookup failed for {correlation_id!r}; absence is not trusted"
            )
        if not isinstance(response.data, dict):
            raise BrokerError("Dhan correlation lookup returned malformed data")
        order_id = response.data.get("orderId")
        if order_id is None:
            return None
        order_id_text = str(order_id)
        security_value = response.data.get("securityId")
        order_type_value = response.data.get("orderType")
        try:
            order_type = OrderType(str(order_type_value)) if order_type_value else None
        except ValueError:
            order_type = None
        self._remember(
            correlation_id,
            order_id_text,
            security_id=str(security_value) if security_value is not None else None,
            order_type=order_type,
        )
        return _order_from_broker_read(
            correlation_id,
            status=map_dhan_order_status(response.data.get("orderStatus")),
            broker_order_id=order_id_text,
            filled_quantity=int(response.data.get("filledQty") or 0),
            average_fill_price=(
                float(response.data["averageTradedPrice"])
                if response.data.get("averageTradedPrice") is not None
                else None
            ),
        )

    def _resolve_order_id(self, correlation_id: str) -> str:
        known = self._known_orders.get(correlation_id)
        if known is not None:
            return known
        found = self.order_by_correlation_id(correlation_id)
        if found is None or found.broker_order_id is None:
            raise BrokerError(f"Dhan has no confirmed order for {correlation_id!r}")
        return found.broker_order_id

    def modify(
        self, correlation_id: str, *, quantity: int | None = None, limit_price: float | None = None
    ) -> Order:
        order_id = self._resolve_order_id(correlation_id)
        security_id = self._known_security_ids.get(correlation_id)
        order_type = self._known_order_types.get(correlation_id)
        if security_id is None or order_type is None:
            raise BrokerError(
                f"cannot safely modify {correlation_id!r}: Dhan did not provide "
                "securityId/orderType for authoritative validation"
            )
        rules = self.instrument_rules(security_id)
        if rules is None or rules.lot_size is None or rules.lot_size <= 0:
            raise BrokerError(f"no authoritative rules for live security_id={security_id!r}")
        if quantity is not None:
            if quantity <= 0 or quantity % rules.lot_size:
                raise BrokerError(
                    f"modified quantity {quantity} is not a positive multiple of "
                    f"lot size {rules.lot_size}"
                )
            if quantity > self.max_quantity_lots * rules.lot_size:
                raise BrokerError("modified quantity exceeds the controlled-live maximum")
            if rules.max_quantity is not None and quantity > rules.max_quantity:
                raise BrokerError("modified quantity exceeds the exchange maximum")
        if limit_price is not None:
            if limit_price <= 0 or rules.tick_size is None or rules.tick_size <= 0:
                raise BrokerError("modified limit price requires a positive authoritative tick")
            ticks = limit_price / rules.tick_size
            if not math.isclose(ticks, round(ticks), abs_tol=1e-9):
                raise BrokerError(
                    f"modified limit_price {limit_price} is not on tick size {rules.tick_size}"
                )
            order_type = OrderType.LIMIT
        self._before(RateLimitCallClass.MODIFY)
        response = DhanApiResponse.from_raw(
            self.client.modify_order(
                order_id,
                order_type.value,
                "",
                quantity or 0,
                limit_price or 0,
                0,
                0,
                "DAY",
            )
        )
        status = (
            map_dhan_order_status(response.data.get("orderStatus"))
            if response.status == "success" and isinstance(response.data, dict)
            else OrderStatus.UNKNOWN
        )
        return _order_from_broker_read(correlation_id, status=status, broker_order_id=order_id)

    def cancel(self, correlation_id: str) -> Order:
        order_id = self._resolve_order_id(correlation_id)
        self._before(RateLimitCallClass.CANCEL)
        response = DhanApiResponse.from_raw(self.client.cancel_order(order_id))
        status = OrderStatus.UNKNOWN
        if response.status == "success" and isinstance(response.data, dict):
            mapped = map_dhan_order_status(response.data.get("orderStatus"))
            if mapped is OrderStatus.CANCELLED:
                status = mapped
        return _order_from_broker_read(correlation_id, status=status, broker_order_id=order_id)

    def fetch_order_book(self) -> tuple[Order, ...]:
        self._before(RateLimitCallClass.READ)
        response = DhanApiResponse.from_raw(self.client.get_order_list())
        if response.status != "success" or not isinstance(response.data, list):
            raise BrokerError("Dhan order-book fetch failed or returned malformed data")
        orders: list[Order] = []
        for row in response.data:
            order_id = row.get("orderId")
            if order_id is None:
                raise BrokerError("Dhan order-book row has no orderId")
            order_id_text = str(order_id)
            correlation_value = row.get("correlationId")
            correlation_id = (
                str(correlation_value) if correlation_value else f"unattributed:{order_id_text}"
            )
            security_value = row.get("securityId")
            order_type_value = row.get("orderType")
            try:
                order_type = OrderType(str(order_type_value)) if order_type_value else None
            except ValueError:
                order_type = None
            self._remember(
                correlation_id,
                order_id_text,
                security_id=str(security_value) if security_value is not None else None,
                order_type=order_type,
            )
            orders.append(
                _order_from_broker_read(
                    correlation_id,
                    status=map_dhan_order_status(row.get("orderStatus")),
                    broker_order_id=order_id_text,
                    filled_quantity=int(row.get("filledQty") or 0),
                    average_fill_price=(
                        float(row["averageTradedPrice"])
                        if row.get("averageTradedPrice") is not None
                        else None
                    ),
                )
            )
        return tuple(orders)

    def _fill_from_row(self, row: dict[str, Any], *, correlation_id: str) -> Fill:
        fill_id = row.get("exchangeTradeId")
        quantity = row.get("tradedQuantity")
        price = row.get("tradedPrice")
        if fill_id is None or quantity is None or price is None:
            raise BrokerError("Dhan trade row is missing trade id, quantity or price")
        identity = _order_from_broker_read(
            correlation_id, status=OrderStatus.FILLED, broker_order_id=str(row.get("orderId"))
        )
        return Fill(
            correlation_id=correlation_id,
            broker_fill_id=str(fill_id),
            strategy_id=identity.strategy_id,
            execution_mode=identity.execution_mode,
            quantity=int(quantity),
            price=float(price),
            filled_at=_broker_time(
                row.get("exchangeTime") or row.get("updateTime") or row.get("createTime")
            ),
            fill_method="dhan_trade_book",
        )

    def _fetch_fills_for_order(self, order_id: str, *, correlation_id: str) -> tuple[Fill, ...]:
        self._before(RateLimitCallClass.READ)
        response = DhanApiResponse.from_raw(self.client.get_trade_book(order_id))
        if response.status != "success" or not isinstance(response.data, list):
            raise BrokerError(f"Dhan trade fetch failed for order_id={order_id!r}")
        return tuple(
            self._fill_from_row(row, correlation_id=correlation_id) for row in response.data
        )

    def fetch_trades(self) -> tuple[Fill, ...]:
        if not self._known_correlations_by_order_id:
            self.fetch_order_book()
        self._before(RateLimitCallClass.READ)
        response = DhanApiResponse.from_raw(self.client.get_trade_book())
        if response.status != "success" or not isinstance(response.data, list):
            raise BrokerError("Dhan trade-book fetch failed or returned malformed data")
        fills: list[Fill] = []
        for row in response.data:
            order_id = row.get("orderId")
            if order_id is None:
                raise BrokerError("Dhan trade row has no orderId")
            correlation_id = self._known_correlations_by_order_id.get(str(order_id))
            if correlation_id is None:
                raise BrokerError(
                    f"Dhan trade order_id={order_id!r} has no order-book correlation identity"
                )
            fills.append(self._fill_from_row(row, correlation_id=correlation_id))
        return tuple(fills)

    def fetch_positions(self) -> tuple[BrokerPosition, ...]:
        self._before(RateLimitCallClass.READ)
        response = DhanApiResponse.from_raw(self.client.get_positions())
        if response.status != "success" or not isinstance(response.data, list):
            raise BrokerError("Dhan positions fetch failed or returned malformed data")
        positions: list[BrokerPosition] = []
        for row in response.data:
            security_id = row.get("securityId")
            quantity = row.get("netQty")
            if security_id is None or quantity is None:
                raise BrokerError("Dhan position row is missing securityId or netQty")
            parsed_quantity = int(quantity)
            if parsed_quantity == 0:
                continue
            positions.append(
                BrokerPosition(
                    security_id=str(security_id),
                    quantity=parsed_quantity,
                    average_price=float(row.get("costPrice", 0.0)),
                    product_type=str(row.get("productType", self.product_type)),
                )
            )
        return tuple(positions)


def _assert_satisfies_broker_protocol(broker: Broker) -> None:
    """Never called — exists so mypy checks ``DhanLiveBroker`` against the
    full ``Broker`` protocol at import time, the same structural proof
    ``common/broker/factory.py`` relies on when it returns one typed as
    ``Broker``."""
    del broker
