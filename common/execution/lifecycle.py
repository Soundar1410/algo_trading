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
from common.risk.account_reservations import AccountReservationGate, IllegalReservationTransition

from .correlation import build_correlation_id
from .repository import ExecutionRepository

_log = get_logger(__name__)

#: OrderStatus -> the account-reservation state it corresponds to, for
#: syncing the reservation after a live submission. Every reservation is
#: transitioned through SUBMITTED first (submit() genuinely was called,
#: regardless of outcome) and then, if different, on to the real target —
#: RESERVED has no direct edge to most of these, SUBMITTED does (see
#: common.risk.account_reservations._LEGAL_TRANSITIONS).
_ORDER_STATUS_TO_RESERVATION_STATE: dict[OrderStatus, str] = {
    OrderStatus.SUBMITTED: "SUBMITTED",
    OrderStatus.ACKNOWLEDGED: "ACKNOWLEDGED",
    OrderStatus.PARTIALLY_FILLED: "PARTIALLY_FILLED",
    OrderStatus.FILLED: "FILLED",
    OrderStatus.REJECTED: "REJECTED",
    OrderStatus.CANCELLED: "CANCELLED",
    OrderStatus.EXPIRED: "EXPIRED",
    OrderStatus.UNKNOWN: "UNKNOWN",
}


@dataclass(frozen=True, slots=True)
class LiveAccountRiskLimits:
    """The account-wide ceilings one live strategy's reservations are
    checked against — ``None`` in any field means that particular ceiling
    is not enforced (a runtime group can enforce a subset), never
    "unlimited by omission" for the ones that ARE configured."""

    max_deployed_capital: float | None = None
    max_open_positions: int | None = None
    max_open_legs: int | None = None
    max_daily_loss: float | None = None
    max_mtm_age_seconds: float | None = None


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
        account_reservation_gate: AccountReservationGate | None = None,
        account_key: str | None = None,
        account_risk_limits: LiveAccountRiskLimits | None = None,
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
        #: Phase 10. ``None`` for paper (the only value any paper construction
        #: ever supplies) — a paper OrderLifecycle never even imports the
        #: account-shared database, let alone reserves against it. A live
        #: OrderLifecycle without one is a construction bug, not a silent
        #: "unlimited": ``_check_and_reserve_account_risk`` below refuses
        #: closed rather than skip the check when mode is LIVE and this is None.
        self._account_reservation_gate = account_reservation_gate
        self._account_key = account_key
        self._account_risk_limits = account_risk_limits or LiveAccountRiskLimits()

    def handle_signal(
        self,
        signal: Signal,
        *,
        trading_date: str,
        stop_price: float | None = None,
        target_price: float | None = None,
        basket_id: str | None = None,
        leg_id: str | None = None,
        cycle_id: str | None = None,
    ) -> ExecutionResult:
        """Take one signal all the way to a persisted position.

        ``basket_id``/``leg_id`` (optional): a multi-leg caller's basket/leg
        identity, carried straight onto the persisted ``OrderIntent`` —
        ``order_intents.basket_id``/``.leg_id`` already have columns for
        this (added ahead of any consumer); this is the first real writer.
        ``None`` for every existing single-leg caller, unchanged.

        ``cycle_id`` (optional, spec review correction 4): a positional
        caller's durable cycle identity. Threaded through only to
        :meth:`~common.execution.repository.ExecutionRepository.apply_fill`,
        which is where a cycle-scoped position lookup actually differs from
        the ``trading_date``-scoped default — every other write this method
        makes (``signals``, ``order_intents``, ``orders``, ``fills``) stays
        keyed on ``trading_date`` exactly as before, unaffected by this
        parameter. The positional engine passes its own cycle's ``cycle_id``
        here *and* as ``basket_id`` above — the same value, two different
        reasons: ``basket_id`` is the generic multi-leg correlation identity
        (``order_intents``/``orders``/``fills``), ``cycle_id`` is what makes
        ``positions`` itself cross-day-resolvable.
        """
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
        quote = self._quote_for(signal)
        risk_reducing = self._is_risk_reducing(signal, trading_date=trading_date)
        risk_decision, risk_reason = self._check_and_reserve_account_risk(
            correlation_id=correlation_id,
            trading_date=trading_date,
            quantity=signal.quantity,
            quote=quote,
            risk_reducing=risk_reducing,
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
            basket_id=basket_id,
            leg_id=leg_id,
            config_fingerprint=self._fingerprint,
            risk_decision=risk_decision,
            risk_reason=risk_reason,
            risk_reducing=risk_reducing,
        )

        # Commits before the broker is called: a crash during submission leaves
        # a recoverable record, keyed by a correlation ID that already exists.
        # Persisted regardless of risk_decision — a blocked intent is a normal
        # recorded outcome, not something the audit trail should be missing.
        intent_id = self._repo.reserve_intent(session_id=self._session_id, intent=intent)

        if risk_decision is RiskDecision.BLOCKED:
            _log.warning(
                "live order blocked by account risk correlation_id=%s reason=%s",
                correlation_id,
                risk_reason,
            )
            return ExecutionResult(
                correlation_id=correlation_id,
                skipped_reason=f"account risk blocked: {risk_reason}",
            )

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
            self._sync_account_reservation(correlation_id, OrderStatus.REJECTED)
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
                cycle_id=cycle_id,
            )
            if self._account_reservation_gate is not None and self._account_key is not None:
                self._account_reservation_gate.record_fill(
                    account_key=self._account_key,
                    runtime_id=self._runtime_id,
                    strategy_id=self._strategy_id,
                    trading_date=trading_date,
                    security_id=signal.security_id,
                    side=signal.side,
                    fill=fill,
                    position=position,
                )

        self._sync_account_reservation(correlation_id, order.status)

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

    # ------------------------------------------------------- account risk (live)
    def _is_risk_reducing(self, signal: Signal, *, trading_date: str) -> bool:
        if self._mode is not ExecutionMode.LIVE:
            return False
        positions = self._repo.open_positions(
            strategy_id=self._strategy_id,
            execution_mode=self._mode,
            trading_date=trading_date,
        )
        matching = [
            position for position in positions if position.security_id == signal.security_id
        ]
        if len(matching) != 1:
            return False
        position = matching[0]
        return position.quantity * signal.side.sign < 0 and signal.quantity <= abs(
            position.quantity
        )

    def _check_and_reserve_account_risk(
        self,
        *,
        correlation_id: str,
        trading_date: str,
        quantity: int,
        quote: Quote,
        risk_reducing: bool,
    ) -> tuple[RiskDecision, str | None]:
        """Atomically check-and-reserve account-wide capacity for a live
        intent, *before* it is ever persisted or the broker is ever called
        (architecture report §4.2). Paper mode never reaches the reservation
        gate at all — it returns ``ALLOWED`` unconditionally, structurally
        excluded rather than filtered by a runtime flag.

        A live ``OrderLifecycle`` with no gate wired is a construction bug,
        not silent permission: it fails closed exactly like a live intent
        that failed the check, rather than skip the check because nothing
        was configured to run it (spec 2393's "no permissive default").
        """
        if self._mode is not ExecutionMode.LIVE:
            return RiskDecision.ALLOWED, None

        if self._account_reservation_gate is None or self._account_key is None:
            _log.error(
                "live OrderLifecycle for strategy_id=%s has no account reservation gate "
                "wired — blocking correlation_id=%s rather than silently permitting it",
                self._strategy_id,
                correlation_id,
            )
            return RiskDecision.BLOCKED, "no account reservation gate wired for a live strategy"

        # Worst-case capital at risk: quantity * price, the standard
        # premium-at-risk estimate for a bought option. A more precise
        # per-instrument model (margin-based, for sold/short exposure) is a
        # strategy/instrument-specific refinement, not something this
        # generic lifecycle can assume — see the reservation's own
        # projected_capital field for where that refinement would plug in.
        projected_capital = 0.0 if risk_reducing else quantity * quote.last_price

        decision = self._account_reservation_gate.check_and_reserve(
            account_key=self._account_key,
            runtime_id=self._runtime_id,
            strategy_id=self._strategy_id,
            correlation_id=correlation_id,
            trading_date=trading_date,
            projected_capital=projected_capital,
            projected_legs=0 if risk_reducing else 1,
            quantity=quantity,
            max_deployed_capital=self._account_risk_limits.max_deployed_capital,
            max_open_positions=self._account_risk_limits.max_open_positions,
            max_open_legs=self._account_risk_limits.max_open_legs,
            now=datetime.now(UTC),
            max_daily_loss=self._account_risk_limits.max_daily_loss,
            max_mtm_age_seconds=self._account_risk_limits.max_mtm_age_seconds,
            risk_reducing=risk_reducing,
        )
        if decision.allowed:
            return RiskDecision.ALLOWED, None
        return RiskDecision.BLOCKED, decision.reason

    def _sync_account_reservation(self, correlation_id: str, order_status: OrderStatus) -> None:
        """Move the reservation from RESERVED through SUBMITTED to the
        real outcome. A no-op for paper (no gate wired) and for any
        correlation ID this lifecycle never reserved (e.g. a risk-blocked
        intent, which never got past ``check_and_reserve`` and so has no
        RESERVED row to advance) — both are expected, not errors.
        """
        if self._account_reservation_gate is None or self._account_key is None:
            return
        target = _ORDER_STATUS_TO_RESERVATION_STATE.get(order_status)
        if target is None:
            return
        now = datetime.now(UTC)
        try:
            if target != "SUBMITTED":
                self._account_reservation_gate.transition(
                    account_key=self._account_key,
                    correlation_id=correlation_id,
                    new_state="SUBMITTED",
                    now=now,
                )
            self._account_reservation_gate.transition(
                account_key=self._account_key,
                correlation_id=correlation_id,
                new_state=target,
                now=now,
            )
        except IllegalReservationTransition as exc:
            _log.error(
                "could not sync account reservation for correlation_id=%s to %s: %s",
                correlation_id,
                target,
                exc,
            )
