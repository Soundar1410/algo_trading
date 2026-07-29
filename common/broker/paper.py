"""Simulated execution.

**Phase 1 scope (deviation D11).** The spec's full paper-fill model — bid/ask
depth, modelled latency against the *post-latency* quote, limit orders, partial
fills and the nine rejection rules — is Phase 4. Building it now would mean
building it against a recorded tape that has no depth, which would produce
realism that is merely asserted rather than observed.

What is implemented here is the honest subset:

* a fill at the submission-time quote plus configured adverse slippage,
  preferring ask/bid when the quote carries depth and recording the fallback
  method when it does not;
* a latency value that is recorded on the fill, so Phase 4 can start using it to
  *select* the quote without changing any downstream schema;
* exactly one rejection rule — duplicate correlation ID — because that one is a
  correctness property of idempotent submission, not a realism feature.

Slippage is adverse in both directions by construction: a buy fills higher and a
sell lower. A simulator that can accidentally fill you at a better price is
worse than useless, because it makes a losing strategy look profitable.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from common.models import Fill, Order, OrderIntent, OrderStatus, OrderType, Side

from .base import BrokerError, Quote
from .costs import ChargesCalculator, CostRates


class PaperRejection(BrokerError):
    """Raised when the simulator refuses an order."""


@dataclass(frozen=True)
class PaperFillConfig:
    """Fill-model configuration. Values come from strategy YAML."""

    #: Adverse move applied to every market fill, in price points.
    slippage_points: float = 0.05
    submission_latency_ms: int = 250
    #: Whether a fill may fall back to last price when there is no bid/ask.
    allow_ltp_fallback: bool = True

    @classmethod
    def from_mapping(cls, values: dict[str, Any] | None) -> PaperFillConfig:
        if not values:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(values) - known
        if unknown:
            raise ValueError(f"Unknown paper execution keys: {sorted(unknown)}")
        return cls(**values)


@dataclass
class PaperBroker:
    """Deterministic order simulator. Satisfies :class:`Broker`."""

    config: PaperFillConfig = field(default_factory=PaperFillConfig)
    charges: ChargesCalculator = field(default_factory=ChargesCalculator)
    _orders: dict[str, Order] = field(default_factory=dict, init=False)
    _fill_sequence: itertools.count[int] = field(
        default_factory=lambda: itertools.count(1), init=False
    )

    @classmethod
    def from_config(
        cls,
        *,
        paper_execution: dict[str, Any] | None = None,
        cost_rates: dict[str, Any] | None = None,
    ) -> PaperBroker:
        return cls(
            config=PaperFillConfig.from_mapping(paper_execution),
            charges=ChargesCalculator(CostRates.from_mapping(cost_rates)),
        )

    @property
    def name(self) -> str:
        return "paper"

    def is_healthy(self) -> bool:
        return True

    def order_by_correlation_id(self, correlation_id: str) -> Order | None:
        return self._orders.get(correlation_id)

    def submit(self, intent: OrderIntent, quote: Quote) -> Order:
        """Simulate submission and immediate full fill of a market order."""
        if intent.correlation_id in self._orders:
            # Idempotency is the point: a retried submission must surface as a
            # duplicate rather than quietly producing a second position.
            raise PaperRejection(
                f"Duplicate correlation ID {intent.correlation_id!r} — "
                "an order with this ID has already been submitted"
            )
        if intent.order_type is not OrderType.MARKET:
            # Limit-order simulation needs a post-submission quote stream to
            # decide whether an eligible price was ever reached (spec 5.3).
            raise PaperRejection(
                f"{intent.order_type.value} orders are not simulated in Phase 1 "
                "(limit fills arrive with the Phase 4 fill model)"
            )
        if quote.security_id != intent.security_id:
            raise PaperRejection(
                f"Quote is for {quote.security_id!r} but the order is for {intent.security_id!r}"
            )

        price, method = self._fill_price(intent.side, quote)
        filled_at = datetime.now(UTC)
        charges = self.charges.total_for_leg(
            side=intent.side, price=price, quantity=intent.quantity
        )

        fill = Fill(
            correlation_id=intent.correlation_id,
            broker_fill_id=f"paper-fill-{next(self._fill_sequence):06d}",
            strategy_id=intent.strategy_id,
            execution_mode=intent.execution_mode,
            quantity=intent.quantity,
            price=price,
            filled_at=filled_at,
            reference_price=quote.last_price,
            slippage_amount=round(abs(price - quote.last_price), 6),
            latency_ms=self.config.submission_latency_ms,
            fill_method=method,
            charges=charges,
        )
        order = Order(
            correlation_id=intent.correlation_id,
            strategy_id=intent.strategy_id,
            execution_mode=intent.execution_mode,
            status=OrderStatus.FILLED,
            updated_at=filled_at,
            broker_order_id=f"paper-{intent.correlation_id}",
            filled_quantity=fill.quantity,
            average_fill_price=fill.price,
            fills=(fill,),
        )
        self._orders[intent.correlation_id] = order
        return order

    def _fill_price(self, side: Side, quote: Quote) -> tuple[float, str]:
        """Price one market fill, always adversely."""
        slippage = self.config.slippage_points
        if quote.has_depth:
            assert quote.ask is not None and quote.bid is not None
            base = quote.ask if side is Side.BUY else quote.bid
            method = "bid_ask"
        else:
            if not self.config.allow_ltp_fallback:
                raise PaperRejection(
                    f"No bid/ask for {quote.security_id!r} and LTP fallback is disabled"
                )
            base = quote.last_price
            method = "ltp_fallback"

        price = base + slippage if side is Side.BUY else base - slippage
        if price <= 0:
            raise PaperRejection(
                f"Simulated fill price {price} for {quote.security_id!r} is not positive"
            )
        return round(price, 4), method
