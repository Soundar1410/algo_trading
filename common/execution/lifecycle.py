"""Signal to position, in the order the spec requires.

    signal → persist signal → reserve intent (+correlation ID, committed)
           → call broker (outside any transaction)
           → persist response → apply fill + position + state (one transaction)

The ordering is the safety property. Everything that must survive a crash is on
disk *before* the broker call; nothing that requires a network round trip
happens inside a write transaction.

Duplicate suppression is delegated to the database rather than re-implemented
here: if the signal's candle already produced a signal row, the unique
constraint rejects it and this returns early. That is what makes "one completed
candle creates exactly one order" hold even when a candle is delivered twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from common.broker.base import Broker, BrokerError, Quote
from common.broker.quotes import QuoteBook
from common.config.models import ExecutionMode
from common.logging import get_logger
from common.models import (
    Order,
    OrderIntent,
    OrderStatus,
    OrderType,
    Position,
    RiskDecision,
    Signal,
)

from .correlation import build_correlation_id
from .repository import ExecutionRepository

_log = get_logger(__name__)


@dataclass(frozen=True)
class ExecutionResult:
    """What one signal produced. ``skipped_reason`` set means nothing was traded."""

    correlation_id: str | None = None
    order: Order | None = None
    position: Position | None = None
    skipped_reason: str | None = None

    @property
    def traded(self) -> bool:
        return self.order is not None


class OrderLifecycle:
    """Drives one strategy's signals through to persisted positions."""

    def __init__(
        self,
        *,
        repository: ExecutionRepository,
        broker: Broker,
        runtime_id: str,
        strategy_id: str,
        execution_mode: ExecutionMode,
        session_id: int,
        product_type: str = "INTRADAY",
        config_fingerprint: str | None = None,
        quotes: QuoteBook | None = None,
    ) -> None:
        self._repo = repository
        self._broker = broker
        self._runtime_id = runtime_id
        self._strategy_id = strategy_id
        self._mode = execution_mode
        self._session_id = session_id
        self._product_type = product_type
        self._fingerprint = config_fingerprint
        #: Optional on purpose (Phase 4 Part 5). With a book, the broker is handed
        #: a real quote carrying bid/ask and its own timestamp; without one it
        #: still gets the pre-Part-5 quote synthesised from the signal, which is
        #: what every offline construction and the fixture worker want. ``None``
        #: is therefore a supported configuration, not a missing dependency.
        self._quotes = quotes

    def handle_signal(
        self,
        signal: Signal,
        *,
        trading_date: str,
        stop_price: float | None = None,
        target_price: float | None = None,
    ) -> ExecutionResult:
        """Take one signal all the way to a persisted position."""
        signal_id = self._repo.record_signal(
            session_id=self._session_id,
            runtime_id=self._runtime_id,
            signal=signal,
            trading_date=trading_date,
            config_fingerprint=self._fingerprint,
        )
        if signal_id is None:
            _log.info(
                "duplicate signal suppressed strategy_id=%s candle_end=%s",
                signal.strategy_id,
                signal.candle.end_at.isoformat(),
            )
            return ExecutionResult(skipped_reason="duplicate signal for this candle")

        sequence = self._repo.next_sequence_number(
            strategy_id=self._strategy_id,
            execution_mode=self._mode,
            trading_date=trading_date,
        )
        correlation_id = build_correlation_id(
            execution_mode=self._mode,
            runtime_id=self._runtime_id,
            strategy_id=self._strategy_id,
            trading_date=trading_date,
            sequence_number=sequence,
        )
        intent = OrderIntent(
            correlation_id=correlation_id,
            strategy_id=self._strategy_id,
            runtime_id=self._runtime_id,
            execution_mode=self._mode,
            trading_date=trading_date,
            sequence_number=sequence,
            instrument=signal.instrument,
            security_id=signal.security_id,
            side=signal.side,
            quantity=signal.quantity,
            order_type=OrderType.MARKET,
            product_type=self._product_type,
            created_at=datetime.now(UTC),
            signal_id=signal_id,
            config_fingerprint=self._fingerprint,
            risk_decision=RiskDecision.ALLOWED,
        )

        # Commits before the broker is called: a crash during submission leaves
        # a recoverable record, keyed by a correlation ID that already exists.
        intent_id = self._repo.reserve_intent(session_id=self._session_id, intent=intent)

        quote = self._quote_for(signal)

        try:
            order = self._broker.submit(intent, quote)
        except BrokerError as exc:
            self._repo.record_error(
                runtime_id=self._runtime_id,
                strategy_id=self._strategy_id,
                execution_mode=self._mode,
                severity="ERROR",
                component="broker.submit",
                message=str(exc),
                context=correlation_id,
            )
            rejected = Order(
                correlation_id=correlation_id,
                strategy_id=self._strategy_id,
                execution_mode=self._mode,
                status=OrderStatus.REJECTED,
                updated_at=datetime.now(UTC),
                rejection_reason=str(exc),
            )
            self._repo.record_submission(
                intent_id=intent_id, order=rejected, runtime_id=self._runtime_id
            )
            return ExecutionResult(
                correlation_id=correlation_id,
                order=rejected,
                skipped_reason=f"broker rejected: {exc}",
            )

        order_id = self._repo.record_submission(
            intent_id=intent_id, order=order, runtime_id=self._runtime_id
        )

        position: Position | None = None
        for fill in order.fills:
            position = self._repo.apply_fill(
                order_id=order_id,
                runtime_id=self._runtime_id,
                fill=fill,
                order_status=order.status,
                instrument=signal.instrument,
                security_id=signal.security_id,
                side=signal.side,
                trading_date=trading_date,
                stop_price=stop_price,
                target_price=target_price,
                last_candle_end_at=signal.candle.end_at.isoformat(),
            )

        _log.info(
            "order filled correlation_id=%s side=%s qty=%d price=%s",
            correlation_id,
            signal.side.value,
            order.filled_quantity,
            order.average_fill_price,
        )
        return ExecutionResult(
            correlation_id=correlation_id,
            order=order,
            position=position,
        )

    def _quote_for(self, signal: Signal) -> Quote:
        """The quote the broker prices against.

        Prefers the live book, which carries bid/ask and the exchange's own
        timestamp — the two things a credible fill needs and the signal cannot
        supply. Falls back to the signal's reference price, which is the whole
        quote this method used to build unconditionally.

        The fallback is deliberately *silent* rather than an error. It is the
        correct answer in three real situations: no book was injected at all
        (every offline test), the book has nothing yet for a contract subscribed
        moments ago, or a runtime is replaying a tape that carries no depth. The
        fill records which basis it used in ``Fill.fill_method``, so the
        distinction is visible per fill rather than assumed per deployment.
        """
        if self._quotes is not None:
            live = self._quotes.latest(signal.security_id)
            if live is not None:
                return live
        return Quote(
            security_id=signal.security_id,
            last_price=signal.reference_price,
            quoted_at=signal.evaluated_at,
        )
