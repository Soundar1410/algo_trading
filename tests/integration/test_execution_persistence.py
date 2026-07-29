"""Execution persistence: transaction boundaries, idempotency, mode separation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from common.broker import PaperBroker, PaperFillConfig
from common.config.models import ExecutionMode
from common.execution import ExecutionRepository, OrderLifecycle
from common.models import Candle, Fill, OrderStatus, PositionStatus, Side, Signal
from common.persistence import Database, MigrationRunner

IST = ZoneInfo("Asia/Kolkata")
TRADING_DATE = "2026-07-29"


@pytest.fixture
def repository(database_path: Path) -> ExecutionRepository:
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    return ExecutionRepository(database)


@pytest.fixture
def session(repository: ExecutionRepository):
    return repository.open_session(
        runtime_id="intraday_options",
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=4242,
    )


def _candle(minute: int = 15, close: float = 100.5) -> Candle:
    """A valid bar whose range brackets ``close`` — Candle enforces that itself."""
    start = datetime(2026, 7, 29, 9, minute, tzinfo=IST)
    return Candle(
        security_id="99926000",
        instrument="NIFTY",
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=100,
        start_at=start,
        end_at=start + timedelta(minutes=1),
        tick_count=4,
    )


def _signal(side: Side = Side.BUY, minute: int = 15, close: float = 100.5) -> Signal:
    candle = _candle(minute, close)
    return Signal(
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        instrument="NIFTY",
        security_id="99926000",
        side=side,
        quantity=50,
        candle=candle,
        reference_price=candle.close,
        evaluated_at=candle.end_at,
        reason="test",
    )


def _lifecycle(repository: ExecutionRepository, session, broker=None) -> OrderLifecycle:
    return OrderLifecycle(
        repository=repository,
        broker=broker or PaperBroker(config=PaperFillConfig(slippage_points=0.5)),
        runtime_id="intraday_options",
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        session_id=session.id,
    )


# --------------------------------------------------------------- sessions
def test_a_session_is_recorded_as_open_until_closed(repository, session):
    previous = repository.previous_incomplete_session(
        runtime_id="intraday_options", strategy_id="st01"
    )
    assert previous is not None and previous["id"] == session.id

    repository.close_session(session.id)
    assert (
        repository.previous_incomplete_session(runtime_id="intraday_options", strategy_id="st01")
        is None
    )


# ---------------------------------------------------------------- signals
def test_one_candle_produces_exactly_one_order(repository, session):
    lifecycle = _lifecycle(repository, session)
    first = lifecycle.handle_signal(_signal(), trading_date=TRADING_DATE)
    second = lifecycle.handle_signal(_signal(), trading_date=TRADING_DATE)

    assert first.traded
    assert not second.traded
    assert second.skipped_reason == "duplicate signal for this candle"

    count = repository.database.connect().execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert count == 1


def test_a_different_candle_produces_a_second_order(repository, session):
    lifecycle = _lifecycle(repository, session)
    lifecycle.handle_signal(_signal(minute=15), trading_date=TRADING_DATE)
    second = lifecycle.handle_signal(_signal(minute=16), trading_date=TRADING_DATE)
    assert second.traded


def test_the_exact_candle_is_recorded_with_the_signal(repository, session):
    _lifecycle(repository, session).handle_signal(_signal(), trading_date=TRADING_DATE)
    row = repository.database.connect().execute("SELECT * FROM signals").fetchone()

    assert row["candle_open"] == 100.5
    assert row["candle_high"] == 101.5
    assert row["candle_low"] == 99.5
    assert row["candle_close"] == 100.5


# ----------------------------------------------------------- correlation
def test_the_correlation_id_is_paper_namespaced(repository, session):
    result = _lifecycle(repository, session).handle_signal(_signal(), trading_date=TRADING_DATE)
    assert result.correlation_id is not None
    assert result.correlation_id.startswith("p_")


def test_sequence_numbers_increment_per_strategy_day(repository, session):
    lifecycle = _lifecycle(repository, session)
    lifecycle.handle_signal(_signal(minute=15), trading_date=TRADING_DATE)
    lifecycle.handle_signal(_signal(minute=16), trading_date=TRADING_DATE)

    sequences = [
        row["sequence_number"]
        for row in repository.database.connect().execute(
            "SELECT sequence_number FROM order_intents ORDER BY id"
        )
    ]
    assert sequences == [1, 2]


def test_the_sequence_continues_after_a_restart(repository, session):
    _lifecycle(repository, session).handle_signal(_signal(minute=15), trading_date=TRADING_DATE)

    # A fresh repository stands in for a restarted process.
    restarted = ExecutionRepository(repository.database)
    assert (
        restarted.next_sequence_number(
            strategy_id="st01", execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE
        )
        == 2
    )


# -------------------------------------------------------- ordering rules
def test_the_intent_is_persisted_before_the_broker_is_called(repository, session):
    """A crash during submission must leave a recoverable, correlated record."""

    class _CrashingBroker:
        name = "crashing"

        def submit(self, intent, quote):
            # By now the intent must already be committed and readable.
            row = (
                repository.database.connect()
                .execute(
                    "SELECT submission_reserved FROM order_intents WHERE correlation_id = ?",
                    (intent.correlation_id,),
                )
                .fetchone()
            )
            assert row is not None
            assert row["submission_reserved"] == 1
            raise RuntimeError("network died mid-submission")

        def order_by_correlation_id(self, correlation_id):
            return None

        def is_healthy(self) -> bool:
            return True

    lifecycle = _lifecycle(repository, session, broker=_CrashingBroker())
    with pytest.raises(RuntimeError, match="network died"):
        lifecycle.handle_signal(_signal(), trading_date=TRADING_DATE)

    orphan = repository.database.connect().execute("SELECT * FROM order_intents").fetchone()
    assert orphan is not None  # recoverable by correlation ID


# ---------------------------------------------------------------- fills
def test_a_fill_creates_a_position_with_execution_mode_paper(repository, session):
    result = _lifecycle(repository, session).handle_signal(_signal(), trading_date=TRADING_DATE)

    assert result.position is not None
    assert result.position.execution_mode is ExecutionMode.PAPER
    assert result.position.quantity == 50

    row = repository.database.connect().execute("SELECT * FROM positions").fetchone()
    assert row["execution_mode"] == "paper"


def test_every_persisted_row_carries_paper_mode(repository, session):
    _lifecycle(repository, session).handle_signal(_signal(), trading_date=TRADING_DATE)
    conn = repository.database.connect()

    for table in ("signals", "order_intents", "orders", "fills", "positions"):
        modes = {r["execution_mode"] for r in conn.execute(f"SELECT execution_mode FROM {table}")}
        assert modes == {"paper"}, table


def test_replaying_a_fill_does_not_double_the_position(repository, session):
    """Idempotency on (order_id, broker_fill_id)."""
    result = _lifecycle(repository, session).handle_signal(_signal(), trading_date=TRADING_DATE)
    assert result.position is not None and result.position.quantity == 50

    order_row = repository.database.connect().execute("SELECT id FROM orders").fetchone()
    fill = Fill(
        correlation_id=result.correlation_id or "",
        broker_fill_id="paper-fill-000001",  # the same fill again
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        quantity=50,
        price=100.5,
        filled_at=datetime.now(UTC),
    )
    again = repository.apply_fill(
        order_id=int(order_row["id"]),
        runtime_id="intraday_options",
        fill=fill,
        order_status=OrderStatus.FILLED,
        instrument="NIFTY",
        security_id="99926000",
        side=Side.BUY,
        trading_date=TRADING_DATE,
    )
    assert again.quantity == 50  # unchanged

    fills = repository.database.connect().execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    assert fills == 1


def test_an_opposing_fill_closes_the_position_and_realises_pnl(repository, session):
    lifecycle = _lifecycle(repository, session)
    lifecycle.handle_signal(_signal(Side.BUY, minute=15, close=100.0), trading_date=TRADING_DATE)
    result = lifecycle.handle_signal(
        _signal(Side.SELL, minute=16, close=110.0), trading_date=TRADING_DATE
    )

    assert result.position is not None
    assert result.position.quantity == 0
    assert result.position.status is PositionStatus.CLOSED
    # Bought at 100.0+0.5 slippage, sold at 110.0-0.5.
    assert result.position.realised_pnl == pytest.approx(50 * (109.5 - 100.5))


def test_a_rejected_order_is_recorded_without_a_position(repository, session):
    broker = PaperBroker(config=PaperFillConfig(allow_ltp_fallback=False))
    result = _lifecycle(repository, session, broker=broker).handle_signal(
        _signal(), trading_date=TRADING_DATE
    )

    assert not result.traded or result.order.status is OrderStatus.REJECTED
    conn = repository.database.connect()
    positions = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    assert positions == 0
    errors = repository.database.connect().execute("SELECT COUNT(*) FROM errors").fetchone()[0]
    assert errors == 1


# ------------------------------------------------------------- recovery
def test_open_positions_are_queryable_for_recovery(repository, session):
    _lifecycle(repository, session).handle_signal(_signal(), trading_date=TRADING_DATE)

    open_positions = repository.open_positions(
        strategy_id="st01", execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE
    )
    assert len(open_positions) == 1
    assert open_positions[0].is_open


def test_positions_do_not_leak_across_trading_dates(repository, session):
    """State must never leak between days (spec section 12)."""
    _lifecycle(repository, session).handle_signal(_signal(), trading_date=TRADING_DATE)

    other_day = repository.open_positions(
        strategy_id="st01", execution_mode=ExecutionMode.PAPER, trading_date="2026-07-30"
    )
    assert other_day == []


def test_paper_and_live_positions_are_queried_separately(repository, session):
    _lifecycle(repository, session).handle_signal(_signal(), trading_date=TRADING_DATE)

    live = repository.open_positions(
        strategy_id="st01", execution_mode=ExecutionMode.LIVE, trading_date=TRADING_DATE
    )
    assert live == []


def test_strategy_state_records_the_last_processed_candle(repository, session):
    _lifecycle(repository, session).handle_signal(_signal(), trading_date=TRADING_DATE)

    state = repository.load_strategy_state(
        strategy_id="st01", execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE
    )
    assert state is not None
    assert state["last_candle_end_at"] is not None


def test_square_off_state_survives_a_reload(repository):
    repository.save_strategy_state(
        runtime_id="intraday_options",
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        square_off_state="COMPLETED",
        entries_blocked=True,
    )
    state = repository.load_strategy_state(
        strategy_id="st01", execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE
    )
    assert state is not None
    assert state["square_off_state"] == "COMPLETED"
    assert state["entries_blocked"] == 1


# ------------------------------------------------------- notifications
def test_notifications_are_persisted(repository):
    repository.record_notification(
        runtime_id="intraday_options",
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        channel="telegram",
        event_type="order_filled",
        message="BUY 50 NIFTY",
        delivered=True,
    )
    row = repository.database.connect().execute("SELECT * FROM notifications").fetchone()
    assert row["event_type"] == "order_filled"
    assert row["delivered"] == 1


def test_the_database_stays_consistent_after_a_full_cycle(repository, session):
    lifecycle = _lifecycle(repository, session)
    lifecycle.handle_signal(_signal(Side.BUY, minute=15), trading_date=TRADING_DATE)
    lifecycle.handle_signal(_signal(Side.SELL, minute=16), trading_date=TRADING_DATE)

    assert repository.database.integrity_check() == []
    assert repository.database.foreign_key_check() == []
