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

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from common.logging import get_logger
from common.models import Fill, Order, OrderIntent, OrderStatus, OrderType

from .base import Broker, BrokerError, BrokerPosition, Quote

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
    correlation_id: str, *, status: OrderStatus, broker_order_id: str | None
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


class DhanOrderClient(Protocol):
    """The exact ``dhanhq`` SDK surface this module calls — narrowed so a
    test double only needs to implement this much, never the real SDK."""

    def place_order(
        self,
        security_id: str,
        exchange_segment: str,
        transaction_type: str,
        quantity: int,
        order_type: str,
        product_type: str,
        price: float,
        trigger_price: float = 0,
        disclosed_quantity: int = 0,
        after_market_order: bool = False,
        validity: str = "DAY",
        amo_time: str = "OPEN",
        bo_profit_value: float | None = None,
        bo_stop_loss_Value: float | None = None,
        tag: str | None = None,
        should_slice: bool = False,
    ) -> dict[str, Any]: ...

    def get_order_by_correlationID(self, correlation_id: str) -> dict[str, Any]: ...

    def get_order_by_id(self, order_id: str) -> dict[str, Any]: ...

    def get_order_list(self) -> dict[str, Any]: ...

    def modify_order(
        self,
        order_id: str,
        order_type: str,
        leg_name: str,
        quantity: int,
        price: float,
        trigger_price: float,
        disclosed_quantity: int,
        validity: str,
    ) -> dict[str, Any]: ...

    def cancel_order(self, order_id: str) -> dict[str, Any]: ...

    def get_positions(self) -> dict[str, Any]: ...


@dataclass
class DhanLiveBroker:
    """Satisfies :class:`Broker`. Maps internal orders to Dhan requests,
    validates nothing the caller hasn't already validated (segment/product/
    lot/tick/quantity checks belong to the instrument-rules layer shared
    with :class:`~common.broker.paper.PaperBroker`, applied before
    ``submit`` is ever called — this class assumes a valid intent), and
    never resubmits on an ambiguous result.
    """

    client: DhanOrderClient
    exchange_segment: str
    product_type: str
    #: Correlation-ID -> broker order-id, so lookups and reconciliation
    #: don't require a network round trip for state this process already
    #: knows. Not a cache of trust — ``order_by_correlation_id`` always asks
    #: the broker; this only tells cancel/modify which Dhan order to target.
    _known_orders: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return "dhan_live"

    def is_healthy(self) -> bool:
        response = DhanApiResponse.from_raw(self.client.get_order_list())
        return response.status == "success"

    # ------------------------------------------------------------------ submit
    def submit(self, intent: OrderIntent, quote: Quote) -> Order:
        """Submit one intent. Idempotent on ``intent.correlation_id`` at the
        Dhan API layer via ``tag`` — a caller retrying the *same* intent
        after an UNKNOWN result must go through
        :meth:`order_by_correlation_id`/reconciliation, never call this a
        second time for it (spec 1696-1717); this method itself does not
        guard against that misuse, the same way ``PaperBroker.submit``
        trusts its own caller not to re-`reserve_intent` twice.
        """
        price: float
        if intent.order_type is OrderType.LIMIT:
            if intent.limit_price is None:
                raise BrokerError(
                    f"cannot submit {intent.correlation_id!r}: order_type is LIMIT but "
                    "limit_price is None"
                )
            price = intent.limit_price
        else:
            price = 0.0
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
        if order.broker_order_id is not None:
            self._known_orders[intent.correlation_id] = order.broker_order_id
        return order

    def order_by_correlation_id(self, correlation_id: str) -> Order | None:
        """The UNKNOWN-state recovery path (spec 1712): query Dhan directly
        by correlation ID rather than trusting any local cache."""
        raw = self.client.get_order_by_correlationID(correlation_id)
        response = DhanApiResponse.from_raw(raw)
        if response.status != "success" or not isinstance(response.data, dict):
            return None
        order_id = response.data.get("orderId")
        dhan_status = response.data.get("orderStatus")
        if order_id is None:
            return None
        return _order_from_broker_read(
            correlation_id, status=map_dhan_order_status(dhan_status), broker_order_id=str(order_id)
        )

    # --------------------------------------------------------- modify/cancel
    def modify(
        self, correlation_id: str, *, quantity: int | None = None, limit_price: float | None = None
    ) -> Order:
        order_id = self._known_orders.get(correlation_id)
        if order_id is None:
            raise BrokerError(f"cannot modify {correlation_id!r}: no known Dhan order id for it")
        raw = self.client.modify_order(
            order_id,
            OrderType.LIMIT.value if limit_price is not None else OrderType.MARKET.value,
            "",
            quantity or 0,
            limit_price or 0,
            0,
            0,
            "DAY",
        )
        response = DhanApiResponse.from_raw(raw)
        if response.status != "success":
            raise BrokerError(f"Dhan refused to modify order {order_id!r} for {correlation_id!r}")
        return _order_from_broker_read(
            correlation_id, status=OrderStatus.ACKNOWLEDGED, broker_order_id=order_id
        )

    def cancel(self, correlation_id: str) -> Order:
        """Cancel a still-resting order. An ambiguous cancel result is
        never trusted outright — the caller must verify via
        :meth:`order_by_correlation_id` before treating the order as
        actually cancelled, the same ambiguous-outcome discipline as
        :meth:`submit`."""
        order_id = self._known_orders.get(correlation_id)
        if order_id is None:
            raise BrokerError(f"cannot cancel {correlation_id!r}: no known Dhan order id for it")
        raw = self.client.cancel_order(order_id)
        response = DhanApiResponse.from_raw(raw)
        if response.status != "success":
            return _order_from_broker_read(
                correlation_id, status=OrderStatus.UNKNOWN, broker_order_id=order_id
            )
        return _order_from_broker_read(
            correlation_id, status=OrderStatus.CANCELLED, broker_order_id=order_id
        )

    # --------------------------------------------------------- reconciliation
    def fetch_order_book(self) -> tuple[Order, ...]:
        raw = self.client.get_order_list()
        response = DhanApiResponse.from_raw(raw)
        if response.status != "success" or not isinstance(response.data, list):
            return ()
        orders = []
        for row in response.data:
            correlation_id = row.get("correlationId")
            order_id = row.get("orderId")
            if not correlation_id or order_id is None:
                continue
            orders.append(
                _order_from_broker_read(
                    correlation_id,
                    status=map_dhan_order_status(row.get("orderStatus")),
                    broker_order_id=str(order_id),
                )
            )
        return tuple(orders)

    def fetch_trades(self) -> tuple[Fill, ...]:
        """Not yet wired to a confirmed dhanhq trade-book method — see
        architecture report open decision (fetch trades exact SDK method).
        Returns empty rather than guessing an endpoint, so a reconciliation
        run that calls this fails closed (fewer broker trades than reality,
        never fabricated ones) instead of silently misreporting."""
        log.warning(
            "DhanLiveBroker.fetch_trades has no confirmed SDK method wired yet — "
            "returning empty; do not treat this as 'no trades exist'"
        )
        return ()

    def fetch_positions(self) -> tuple[BrokerPosition, ...]:
        raw = self.client.get_positions()
        response = DhanApiResponse.from_raw(raw)
        if response.status != "success" or not isinstance(response.data, list):
            return ()
        positions = []
        for row in response.data:
            security_id = row.get("securityId")
            quantity = row.get("netQty")
            if security_id is None or quantity is None:
                continue
            positions.append(
                BrokerPosition(
                    security_id=str(security_id),
                    quantity=int(quantity),
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
