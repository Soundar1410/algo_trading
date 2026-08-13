"""Mode-transition safety: paper->live checks ALL trading dates, not just
today; live->paper/disabled requires a fresh broker-backed reconciliation,
never a local-only check."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from common.config.models import ExecutionMode
from common.execution import ExecutionRepository, check_mode_transition_safety
from common.models import Fill, Order, OrderStatus, OrderType, RiskDecision, Side
from common.models import OrderIntent as _OrderIntent
from common.persistence import Database, MigrationRunner
from common.reconciliation import ReconciliationRunner


class _FakeBroker:
    def __init__(self, orders=(), positions=()):
        self._orders = orders
        self._positions = positions

    @property
    def name(self) -> str:
        return "fake"

    def is_healthy(self) -> bool:
        return True

    def submit(self, intent, quote):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def order_by_correlation_id(self, correlation_id):  # type: ignore[no-untyped-def]
        return None

    def modify(self, correlation_id, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def cancel(self, correlation_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def fetch_order_book(self):  # type: ignore[no-untyped-def]
        return self._orders

    def fetch_trades(self):  # type: ignore[no-untyped-def]
        return ()

    def fetch_positions(self):  # type: ignore[no-untyped-def]
        return self._positions


def _repository(tmp_path: Path) -> ExecutionRepository:
    database = Database(tmp_path / "operational" / "intraday_options.db")
    MigrationRunner(database).run_pending()
    return ExecutionRepository(database)


def _open_a_position(
    repository: ExecutionRepository, *, mode: ExecutionMode, trading_date: str, correlation_id: str
) -> None:
    session = repository.open_session(
        runtime_id="intraday_options",
        strategy_id="st01",
        execution_mode=mode,
        process_role="worker",
        pid=1,
    )
    intent = _OrderIntent(
        correlation_id=correlation_id,
        strategy_id="st01",
        runtime_id="intraday_options",
        execution_mode=mode,
        trading_date=trading_date,
        sequence_number=1,
        instrument="NIFTY",
        security_id="sec1",
        side=Side.BUY,
        quantity=75,
        order_type=OrderType.MARKET,
        product_type="INTRADAY",
        created_at=datetime.now(UTC),
        risk_decision=RiskDecision.ALLOWED,
    )
    intent_id = repository.reserve_intent(session_id=session.id, intent=intent)
    order = Order(
        correlation_id=correlation_id,
        strategy_id="st01",
        execution_mode=mode,
        status=OrderStatus.FILLED,
        updated_at=datetime.now(UTC),
        broker_order_id="b1",
    )
    order_id = repository.record_submission(
        intent_id=intent_id, order=order, runtime_id="intraday_options"
    )
    fill = Fill(
        correlation_id=correlation_id,
        broker_fill_id=f"bf_{correlation_id}",
        strategy_id="st01",
        execution_mode=mode,
        quantity=75,
        price=190.0,
        filled_at=datetime.now(UTC),
    )
    repository.apply_fill(
        order_id=order_id,
        runtime_id="intraday_options",
        fill=fill,
        order_status=OrderStatus.FILLED,
        instrument="NIFTY",
        security_id="sec1",
        side=Side.BUY,
        trading_date=trading_date,
        last_candle_end_at=datetime.now(UTC).isoformat(),
    )


# ------------------------------------------------------------ paper -> live
def test_paper_to_live_is_allowed_with_no_open_paper_positions(tmp_path: Path):
    repository = _repository(tmp_path)
    decision = check_mode_transition_safety(
        repository,
        strategy_id="st01",
        runtime_id="intraday_options",
        new_mode=ExecutionMode.LIVE,
        broker=None,
        reconciliation_runner=None,
    )
    assert decision.allowed


def test_paper_to_live_is_rejected_with_an_open_paper_position_today(tmp_path: Path):
    repository = _repository(tmp_path)
    _open_a_position(
        repository, mode=ExecutionMode.PAPER, trading_date="2026-08-13", correlation_id="p1"
    )
    decision = check_mode_transition_safety(
        repository,
        strategy_id="st01",
        runtime_id="intraday_options",
        new_mode=ExecutionMode.LIVE,
        broker=None,
        reconciliation_runner=None,
    )
    assert not decision.allowed
    assert "open paper position" in decision.reason


def test_paper_to_live_is_rejected_with_a_prior_day_open_position(tmp_path: Path):
    """The all-dates requirement: an open position from a much earlier
    trading date must still block, not only "today"."""
    repository = _repository(tmp_path)
    _open_a_position(
        repository, mode=ExecutionMode.PAPER, trading_date="2026-07-01", correlation_id="p1"
    )
    decision = check_mode_transition_safety(
        repository,
        strategy_id="st01",
        runtime_id="intraday_options",
        new_mode=ExecutionMode.LIVE,
        broker=None,
        reconciliation_runner=None,
    )
    assert not decision.allowed
    assert "2026-07-01" in decision.reason


# ---------------------------------------------------------- live -> paper
def test_live_to_paper_is_allowed_with_no_live_history_at_all(tmp_path: Path):
    repository = _repository(tmp_path)
    decision = check_mode_transition_safety(
        repository,
        strategy_id="st01",
        runtime_id="intraday_options",
        new_mode=ExecutionMode.PAPER,
        broker=None,
        reconciliation_runner=None,
    )
    assert decision.allowed


def test_live_to_paper_with_live_history_but_no_broker_wired_is_refused(tmp_path: Path):
    repository = _repository(tmp_path)
    _open_a_position(
        repository, mode=ExecutionMode.LIVE, trading_date="2026-08-13", correlation_id="l1"
    )
    decision = check_mode_transition_safety(
        repository,
        strategy_id="st01",
        runtime_id="intraday_options",
        new_mode=ExecutionMode.PAPER,
        broker=None,
        reconciliation_runner=None,
    )
    assert not decision.allowed
    assert "cannot prove" in decision.reason


def test_live_to_paper_requires_a_fresh_successful_reconciliation(tmp_path: Path):
    """A clean, matching reconciliation permits the transition."""
    repository = _repository(tmp_path)
    _open_a_position(
        repository, mode=ExecutionMode.LIVE, trading_date="2026-08-13", correlation_id="l1"
    )
    from common.broker.base import BrokerPosition

    broker = _FakeBroker(
        orders=(
            Order(
                correlation_id="l1",
                strategy_id="st01",
                execution_mode=ExecutionMode.LIVE,
                status=OrderStatus.FILLED,
                updated_at=datetime.now(UTC),
                broker_order_id="b1",
            ),
        ),
        positions=(
            BrokerPosition(security_id="sec1", quantity=75, average_price=190.0, product_type=""),
        ),
    )
    reconciliation_runner = ReconciliationRunner(repository.database)

    decision = check_mode_transition_safety(
        repository,
        strategy_id="st01",
        runtime_id="intraday_options",
        new_mode=ExecutionMode.PAPER,
        broker=broker,
        reconciliation_runner=reconciliation_runner,
    )
    assert decision.allowed


def test_live_to_paper_is_blocked_by_a_critical_mismatch(tmp_path: Path):
    repository = _repository(tmp_path)
    _open_a_position(
        repository, mode=ExecutionMode.LIVE, trading_date="2026-08-13", correlation_id="l1"
    )
    # Broker reports nothing at all for this strategy's order -> LOCAL_ONLY
    # for the order is non-critical, but the broker having NO matching
    # position for an OPEN local one -> LOCAL_OPEN_BROKER_CLOSED (non-critical)
    # -- use a broker-only extra position instead to force a critical one.
    from common.broker.base import BrokerPosition

    broker = _FakeBroker(
        orders=(
            Order(
                correlation_id="l1",
                strategy_id="st01",
                execution_mode=ExecutionMode.LIVE,
                status=OrderStatus.FILLED,
                updated_at=datetime.now(UTC),
                broker_order_id="b1",
            ),
            Order(
                correlation_id="l_unexpected",
                strategy_id="st01",
                execution_mode=ExecutionMode.LIVE,
                status=OrderStatus.ACKNOWLEDGED,
                updated_at=datetime.now(UTC),
                broker_order_id="b2",
            ),
        ),
        positions=(
            BrokerPosition(security_id="sec1", quantity=75, average_price=190.0, product_type=""),
        ),
    )
    reconciliation_runner = ReconciliationRunner(repository.database)

    decision = check_mode_transition_safety(
        repository,
        strategy_id="st01",
        runtime_id="intraday_options",
        new_mode=ExecutionMode.PAPER,
        broker=broker,
        reconciliation_runner=reconciliation_runner,
    )
    assert not decision.allowed
    assert "critical mismatch" in decision.reason


def test_live_to_disabled_uses_the_same_check_as_live_to_paper(tmp_path: Path):
    """new_mode=None (disabled) must not bypass the check just because it
    is not technically a mode."""
    repository = _repository(tmp_path)
    _open_a_position(
        repository, mode=ExecutionMode.LIVE, trading_date="2026-08-13", correlation_id="l1"
    )
    decision = check_mode_transition_safety(
        repository,
        strategy_id="st01",
        runtime_id="intraday_options",
        new_mode=None,
        broker=None,
        reconciliation_runner=None,
    )
    assert not decision.allowed
