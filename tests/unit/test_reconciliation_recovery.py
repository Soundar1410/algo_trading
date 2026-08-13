"""Crash-window recovery from broker-confirmed order and trade evidence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from common.config.models import ExecutionMode
from common.execution import ExecutionRepository
from common.models import Fill, Order, OrderIntent, OrderStatus, OrderType, Side
from common.persistence import (
    Database,
    MigrationRunner,
    migrate_account_shared_database,
    open_account_shared_database,
)
from common.reconciliation import (
    LocalOrderState,
    ReconciliationRunner,
    RuntimeGroupSnapshot,
    rebuild_account_shared_state,
)
from common.reconciliation.recovery import load_local_reconciliation_state

NOW = datetime(2026, 8, 13, 4, 0, tzinfo=UTC)


class _Broker:
    def __init__(self, *, orders=(), trades=(), positions=()):
        self.orders = tuple(orders)
        self.trades = tuple(trades)
        self.positions = tuple(positions)

    @property
    def name(self) -> str:
        return "recovery-double"

    def fetch_order_book(self):  # type: ignore[no-untyped-def]
        return self.orders

    def fetch_trades(self):  # type: ignore[no-untyped-def]
        return self.trades

    def fetch_positions(self):  # type: ignore[no-untyped-def]
        return self.positions


def _repository(tmp_path: Path) -> tuple[ExecutionRepository, int]:
    database = Database(tmp_path / "runtime.db")
    MigrationRunner(database).run_pending()
    repository = ExecutionRepository(database)
    session = repository.open_session(
        runtime_id="intraday_options",
        strategy_id="st01",
        execution_mode=ExecutionMode.LIVE,
        process_role="worker",
        pid=123,
    )
    return repository, session.id


def _intent(repository: ExecutionRepository, session_id: int, correlation_id: str, side: Side):
    intent = OrderIntent(
        correlation_id=correlation_id,
        strategy_id="st01",
        runtime_id="intraday_options",
        execution_mode=ExecutionMode.LIVE,
        trading_date="2026-08-13",
        sequence_number=1 if side is Side.BUY else 2,
        instrument="NIFTY OPTION",
        security_id="49081",
        side=side,
        quantity=75,
        order_type=OrderType.MARKET,
        product_type="INTRADAY",
        created_at=NOW,
    )
    return repository.reserve_intent(session_id=session_id, intent=intent)


def _order(correlation_id: str, *, status: OrderStatus = OrderStatus.FILLED) -> Order:
    return Order(
        correlation_id=correlation_id,
        strategy_id="st01",
        execution_mode=ExecutionMode.LIVE,
        status=status,
        updated_at=NOW,
        broker_order_id=f"broker-{correlation_id}",
        filled_quantity=75 if status is OrderStatus.FILLED else 0,
        average_fill_price=100.0 if status is OrderStatus.FILLED else None,
    )


def _fill(correlation_id: str, fill_id: str, *, price: float, minute: int = 0) -> Fill:
    return Fill(
        correlation_id=correlation_id,
        broker_fill_id=fill_id,
        strategy_id="broker-token",
        execution_mode=ExecutionMode.LIVE,
        quantity=75,
        price=price,
        filled_at=NOW + timedelta(minutes=minute),
        fill_method="dhan_trade_book",
    )


def test_crash_after_broker_fill_recovers_order_fill_position_and_is_idempotent(tmp_path: Path):
    repository, session_id = _repository(tmp_path)
    _intent(repository, session_id, "l_entry", Side.BUY)
    broker = _Broker(
        orders=(_order("l_entry"),), trades=(_fill("l_entry", "trade-1", price=100.0),)
    )
    runner = ReconciliationRunner(repository.database, recovery_repository=repository)

    first = runner.run(
        runtime_id="intraday_options",
        strategy_id="st01",
        broker=broker,
        local_orders=[LocalOrderState("l_entry", OrderStatus.UNKNOWN)],
        local_positions=[],
        trigger="startup",
    )
    second = runner.run(
        runtime_id="intraday_options",
        strategy_id="st01",
        broker=broker,
        local_orders=[],
        local_positions=[],
        trigger="startup",
    )

    assert first.status == second.status == "completed"
    assert first.critical_mismatch_count == second.critical_mismatch_count == 0
    conn = repository.database.connect()
    assert conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM fills").fetchone()["n"] == 1
    position = repository.open_positions_all_dates(
        strategy_id="st01", execution_mode=ExecutionMode.LIVE
    )[0]
    assert position.quantity == 75
    resolution_rows = conn.execute(
        "SELECT resolution_action, resolved_at FROM reconciliation_mismatches "
        "WHERE resolution_action IS NOT NULL"
    ).fetchall()
    assert {row["resolution_action"] for row in resolution_rows} == {
        "adopt_broker_order",
        "update_traded_quantity",
    }
    assert all(row["resolved_at"] for row in resolution_rows)


def test_broker_rejection_resolves_unknown_only_from_positive_evidence(tmp_path: Path):
    repository, session_id = _repository(tmp_path)
    intent_id = _intent(repository, session_id, "l_rejected", Side.BUY)
    repository.record_submission(
        intent_id=intent_id,
        order=Order(
            correlation_id="l_rejected",
            strategy_id="st01",
            execution_mode=ExecutionMode.LIVE,
            status=OrderStatus.UNKNOWN,
            updated_at=NOW,
        ),
        runtime_id="intraday_options",
    )
    runner = ReconciliationRunner(repository.database, recovery_repository=repository)
    result = runner.run(
        runtime_id="intraday_options",
        strategy_id="st01",
        broker=_Broker(orders=(_order("l_rejected", status=OrderStatus.REJECTED),)),
        local_orders=[],
        local_positions=[],
        trigger="startup",
    )
    assert result.critical_mismatch_count == 0
    row = (
        repository.database.connect()
        .execute("SELECT status FROM orders WHERE correlation_id = 'l_rejected'")
        .fetchone()
    )
    assert row["status"] == "REJECTED"


def test_missing_trade_book_quantity_blocks_and_does_not_manufacture_a_fill(tmp_path: Path):
    repository, session_id = _repository(tmp_path)
    _intent(repository, session_id, "l_missing", Side.BUY)
    runner = ReconciliationRunner(repository.database, recovery_repository=repository)
    result = runner.run(
        runtime_id="intraday_options",
        strategy_id="st01",
        broker=_Broker(orders=(_order("l_missing"),), trades=()),
        local_orders=[],
        local_positions=[],
        trigger="startup",
    )
    assert result.entries_blocked
    assert any(m.category == "QUANTITY_MISMATCH" for m in result.mismatches)
    assert (
        repository.database.connect().execute("SELECT COUNT(*) AS n FROM fills").fetchone()["n"]
        == 0
    )


def test_recovery_applies_entry_before_exit_even_when_order_book_is_reversed(tmp_path: Path):
    repository, session_id = _repository(tmp_path)
    _intent(repository, session_id, "l_entry", Side.BUY)
    _intent(repository, session_id, "l_exit", Side.SELL)
    runner = ReconciliationRunner(repository.database, recovery_repository=repository)
    result = runner.run(
        runtime_id="intraday_options",
        strategy_id="st01",
        broker=_Broker(
            orders=(_order("l_exit"), _order("l_entry")),
            trades=(
                _fill("l_exit", "trade-exit", price=90.0, minute=1),
                _fill("l_entry", "trade-entry", price=100.0),
            ),
        ),
        local_orders=[],
        local_positions=[],
        trigger="startup",
    )
    assert result.critical_mismatch_count == 0
    assert (
        repository.open_positions_all_dates(strategy_id="st01", execution_mode=ExecutionMode.LIVE)
        == []
    )
    state = repository.load_strategy_state(
        strategy_id="st01",
        execution_mode=ExecutionMode.LIVE,
        trading_date="2026-08-13",
    )
    assert state["daily_realised_pnl"] == -750.0

    account_database = open_account_shared_database(tmp_path / "account.db")
    migrate_account_shared_database(account_database)
    local_orders, local_positions = load_local_reconciliation_state(repository, strategy_id="st01")
    rebuild = rebuild_account_shared_state(
        account_database,
        account_key="acct1",
        runtime_groups=(
            RuntimeGroupSnapshot(
                runtime_id="intraday_options",
                strategy_id="st01",
                broker=_Broker(),
                local_orders=local_orders,
                local_positions=local_positions,
                reconciliation_runner=ReconciliationRunner(
                    repository.database, recovery_repository=repository
                ),
                repository=repository,
            ),
        ),
        lock_path=tmp_path / "account-rebuild.lock",
    )
    assert rebuild.status == "reconciled"
    account_row = (
        account_database.connect()
        .execute(
            "SELECT SUM(realised_pnl_delta) AS pnl, COUNT(*) AS n "
            "FROM live_realised_pnl_events WHERE account_key = 'acct1'"
        )
        .fetchone()
    )
    assert account_row["pnl"] == -750.0
    assert account_row["n"] == 1
