"""OrderLifecycle's Phase 10 wiring: the reserve-before-submit check runs
before any broker call for live intents, paper never touches it, and a
submitted order's reservation is synced to the real outcome afterward."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from common.broker import BrokerError, PaperBroker
from common.broker.base import Order
from common.config.models import ExecutionMode
from common.execution import ExecutionRepository, LiveAccountRiskLimits, OrderLifecycle
from common.models import Candle, OrderStatus, RiskDecision, Side, Signal
from common.persistence import (
    Database,
    MigrationRunner,
    migrate_account_shared_database,
    open_account_shared_database,
)
from common.risk import AccountReservationGate

IST = ZoneInfo("Asia/Kolkata")
TRADING_DATE = "2026-08-13"


class _ScriptedBroker:
    """A minimal Broker double whose submit() outcome is scripted per test —
    real PaperBroker semantics are exercised elsewhere; this file is about
    the reservation-gate wiring, not fill mechanics."""

    def __init__(self, order: Order | None = None, error: BrokerError | None = None):
        self._order = order
        self._error = error
        self.submit_calls = 0

    @property
    def name(self) -> str:
        return "scripted"

    def is_healthy(self) -> bool:
        return True

    def submit(self, intent, quote):  # type: ignore[no-untyped-def]
        self.submit_calls += 1
        if self._error is not None:
            raise self._error
        assert self._order is not None
        return self._order

    def order_by_correlation_id(self, correlation_id):  # type: ignore[no-untyped-def]
        return None

    def modify(self, correlation_id, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def cancel(self, correlation_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def fetch_order_book(self):  # type: ignore[no-untyped-def]
        return ()

    def fetch_trades(self):  # type: ignore[no-untyped-def]
        return ()

    def fetch_positions(self):  # type: ignore[no-untyped-def]
        return ()


@pytest.fixture
def repository(database_path: Path) -> ExecutionRepository:
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    return ExecutionRepository(database)


@pytest.fixture
def account_db(tmp_path: Path):
    database = open_account_shared_database(tmp_path / "dhan_account_shared.db")
    migrate_account_shared_database(database)
    return database


def _signal(*, mode: ExecutionMode = ExecutionMode.PAPER, minute: int = 15) -> Signal:
    start = datetime(2026, 8, 13, 9, minute, tzinfo=IST)
    candle = Candle(
        security_id="49081",
        instrument="NIFTY",
        open=190.0,
        high=191.0,
        low=189.0,
        close=190.0,
        volume=100,
        start_at=start,
        end_at=start + timedelta(minutes=1),
        tick_count=4,
    )
    return Signal(
        strategy_id="st01",
        execution_mode=mode,
        instrument="NIFTY",
        security_id="49081",
        side=Side.BUY,
        quantity=75,
        candle=candle,
        reference_price=candle.close,
        evaluated_at=candle.end_at,
        reason="test",
    )


def _live_order(correlation_id: str, status: OrderStatus) -> Order:
    return Order(
        correlation_id=correlation_id,
        strategy_id="st01",
        execution_mode=ExecutionMode.LIVE,
        status=status,
        updated_at=datetime.now(UTC),
        broker_order_id="broker-1",
    )


def _lifecycle(
    repository: ExecutionRepository,
    session,
    *,
    broker,
    mode: ExecutionMode,
    reservation_gate: AccountReservationGate | None = None,
    account_key: str | None = None,
    limits: LiveAccountRiskLimits | None = None,
) -> OrderLifecycle:
    return OrderLifecycle(
        repository=repository,
        broker=broker,
        runtime_id="intraday_options",
        strategy_id="st01",
        execution_mode=mode,
        session_id=session.id,
        account_reservation_gate=reservation_gate,
        account_key=account_key,
        account_risk_limits=limits,
    )


def _session(repository: ExecutionRepository, mode: ExecutionMode):
    return repository.open_session(
        runtime_id="intraday_options",
        strategy_id="st01",
        execution_mode=mode,
        process_role="worker",
        pid=4242,
    )


# ------------------------------------------------------------------- paper
def test_paper_mode_never_touches_the_reservation_gate(repository, tmp_path: Path):
    """Structural exclusion: paper mode calling handle_signal must not even
    require a reservation gate to be supplied."""
    session = _session(repository, ExecutionMode.PAPER)
    broker = PaperBroker()
    lifecycle = _lifecycle(repository, session, broker=broker, mode=ExecutionMode.PAPER)

    result = lifecycle.handle_signal(_signal(mode=ExecutionMode.PAPER), trading_date=TRADING_DATE)

    assert result.traded


# -------------------------------------------------------------------- live
def test_live_mode_without_a_reservation_gate_is_blocked_not_silently_permitted(repository):
    session = _session(repository, ExecutionMode.LIVE)
    broker = _ScriptedBroker(order=_live_order("x", OrderStatus.FILLED))
    lifecycle = _lifecycle(repository, session, broker=broker, mode=ExecutionMode.LIVE)

    result = lifecycle.handle_signal(_signal(mode=ExecutionMode.LIVE), trading_date=TRADING_DATE)

    assert not result.traded
    assert "no account reservation gate" in result.skipped_reason
    assert broker.submit_calls == 0


def test_live_mode_with_capacity_reserves_then_submits(repository, account_db):
    session = _session(repository, ExecutionMode.LIVE)
    gate = AccountReservationGate(account_db)
    correlation_id_holder: list[str] = []

    class _CapturingBroker(_ScriptedBroker):
        def submit(self, intent, quote):  # type: ignore[no-untyped-def]
            self.submit_calls += 1
            correlation_id_holder.append(intent.correlation_id)
            return _live_order(intent.correlation_id, OrderStatus.FILLED)

    broker = _CapturingBroker()
    lifecycle = _lifecycle(
        repository,
        session,
        broker=broker,
        mode=ExecutionMode.LIVE,
        reservation_gate=gate,
        account_key="acct1",
        limits=LiveAccountRiskLimits(max_deployed_capital=1_000_000.0),
    )

    result = lifecycle.handle_signal(_signal(mode=ExecutionMode.LIVE), trading_date=TRADING_DATE)

    assert result.traded
    assert broker.submit_calls == 1
    state = gate.current_state(account_key="acct1", correlation_id=correlation_id_holder[0])
    assert state == "FILLED"


def test_live_mode_over_capacity_blocks_before_ever_calling_the_broker(repository, account_db):
    session = _session(repository, ExecutionMode.LIVE)
    gate = AccountReservationGate(account_db)
    broker = _ScriptedBroker(order=_live_order("x", OrderStatus.FILLED))
    lifecycle = _lifecycle(
        repository,
        session,
        broker=broker,
        mode=ExecutionMode.LIVE,
        reservation_gate=gate,
        account_key="acct1",
        # 75 qty * 190.0 price = 14,250 projected capital > this ceiling.
        limits=LiveAccountRiskLimits(max_deployed_capital=1000.0),
    )

    result = lifecycle.handle_signal(_signal(mode=ExecutionMode.LIVE), trading_date=TRADING_DATE)

    assert not result.traded
    assert "account risk blocked" in result.skipped_reason
    assert broker.submit_calls == 0


def test_a_risk_blocked_intent_is_still_persisted_for_audit(repository, account_db):
    """Spec 2333: a risk rejection is a normal recorded outcome, not an
    unhandled exception — and not a missing audit row either."""
    session = _session(repository, ExecutionMode.LIVE)
    gate = AccountReservationGate(account_db)
    broker = _ScriptedBroker(order=_live_order("x", OrderStatus.FILLED))
    lifecycle = _lifecycle(
        repository,
        session,
        broker=broker,
        mode=ExecutionMode.LIVE,
        reservation_gate=gate,
        account_key="acct1",
        limits=LiveAccountRiskLimits(max_deployed_capital=1000.0),
    )

    result = lifecycle.handle_signal(_signal(mode=ExecutionMode.LIVE), trading_date=TRADING_DATE)

    assert result.correlation_id is not None
    with repository.database.connect() as conn:
        row = conn.execute(
            "SELECT risk_decision, risk_reason FROM order_intents WHERE correlation_id = ?",
            (result.correlation_id,),
        ).fetchone()
    assert row["risk_decision"] == RiskDecision.BLOCKED.value
    assert row["risk_reason"] is not None


def test_a_broker_error_transitions_the_reservation_to_rejected(repository, account_db):
    session = _session(repository, ExecutionMode.LIVE)
    gate = AccountReservationGate(account_db)
    broker = _ScriptedBroker(error=BrokerError("Dhan refused: insufficient funds"))
    lifecycle = _lifecycle(
        repository,
        session,
        broker=broker,
        mode=ExecutionMode.LIVE,
        reservation_gate=gate,
        account_key="acct1",
        limits=LiveAccountRiskLimits(max_deployed_capital=1_000_000.0),
    )

    result = lifecycle.handle_signal(_signal(mode=ExecutionMode.LIVE), trading_date=TRADING_DATE)

    assert result.order is not None and result.order.status is OrderStatus.REJECTED
    state = gate.current_state(account_key="acct1", correlation_id=result.correlation_id)
    assert state == "REJECTED"


def test_an_unknown_outcome_leaves_the_reservation_conservatively_reserved(repository, account_db):
    """The reservation must remain active (never RELEASED) when the broker
    outcome is UNKNOWN — exactly the "never release on ambiguity" rule."""
    session = _session(repository, ExecutionMode.LIVE)
    gate = AccountReservationGate(account_db)
    broker = _ScriptedBroker(order=_live_order("x", OrderStatus.UNKNOWN))
    lifecycle = _lifecycle(
        repository,
        session,
        broker=broker,
        mode=ExecutionMode.LIVE,
        reservation_gate=gate,
        account_key="acct1",
        limits=LiveAccountRiskLimits(max_deployed_capital=1_000_000.0),
    )

    result = lifecycle.handle_signal(_signal(mode=ExecutionMode.LIVE), trading_date=TRADING_DATE)

    state = gate.current_state(account_key="acct1", correlation_id=result.correlation_id)
    assert state == "UNKNOWN"
