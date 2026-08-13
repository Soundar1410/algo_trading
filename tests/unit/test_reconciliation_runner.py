"""ReconciliationRunner: persists runs/mismatches, blocks new entries on
any critical mismatch, fails closed on a broker fetch failure, and never
silently deletes local records."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from common.broker.base import BrokerPosition
from common.models import Order, OrderStatus
from common.persistence import Database, MigrationRunner
from common.reconciliation import LocalOrderState, LocalPositionState, ReconciliationRunner


class _FakeBroker:
    def __init__(self, orders=(), positions=(), trades=(), raise_on_fetch: Exception | None = None):
        self._orders = orders
        self._positions = positions
        self._trades = trades
        self._raise = raise_on_fetch

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
        if self._raise is not None:
            raise self._raise
        return self._orders

    def fetch_trades(self):  # type: ignore[no-untyped-def]
        return self._trades

    def fetch_positions(self):  # type: ignore[no-untyped-def]
        return self._positions


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "operational" / "intraday_options.db")
    MigrationRunner(database).run_pending()
    return database


def _broker_order(correlation_id: str, status: OrderStatus) -> Order:
    return Order(
        correlation_id=correlation_id,
        strategy_id="st01",
        execution_mode=None,  # type: ignore[arg-type]
        status=status,
        updated_at=datetime.now(UTC),
        broker_order_id="b1",
    )


def test_a_clean_run_is_completed_with_no_mismatches_and_entries_not_blocked(tmp_path: Path):
    db = _database(tmp_path)
    runner = ReconciliationRunner(db)
    broker = _FakeBroker(
        orders=(_broker_order("c1", OrderStatus.FILLED),),
        positions=(),
    )
    result = runner.run(
        runtime_id="intraday_options",
        strategy_id="st01",
        broker=broker,
        local_orders=[LocalOrderState(correlation_id="c1", status=OrderStatus.FILLED)],
        local_positions=[],
    )
    assert result.status == "completed"
    assert result.critical_mismatch_count == 0
    assert not result.entries_blocked


def test_a_critical_mismatch_blocks_new_entries(tmp_path: Path):
    db = _database(tmp_path)
    runner = ReconciliationRunner(db)
    broker = _FakeBroker(orders=(_broker_order("c_unknown", OrderStatus.ACKNOWLEDGED),))
    result = runner.run(
        runtime_id="intraday_options",
        strategy_id="st01",
        broker=broker,
        local_orders=[],
        local_positions=[],
    )
    assert result.critical_mismatch_count == 1
    assert result.entries_blocked


def test_a_broker_fetch_failure_fails_the_run_closed(tmp_path: Path):
    db = _database(tmp_path)
    runner = ReconciliationRunner(db)
    broker = _FakeBroker(raise_on_fetch=ConnectionError("Dhan unreachable"))
    result = runner.run(
        runtime_id="intraday_options",
        strategy_id="st01",
        broker=broker,
        local_orders=[],
        local_positions=[],
    )
    assert result.status == "failed"
    assert result.entries_blocked
    assert "Dhan unreachable" in result.error_message


def test_runs_and_mismatches_are_persisted(tmp_path: Path):
    db = _database(tmp_path)
    runner = ReconciliationRunner(db)
    broker = _FakeBroker(orders=(_broker_order("c_unknown", OrderStatus.ACKNOWLEDGED),))
    result = runner.run(
        runtime_id="intraday_options",
        strategy_id="st01",
        broker=broker,
        local_orders=[],
        local_positions=[],
    )

    with db.connect() as conn:
        run_row = conn.execute(
            "SELECT * FROM reconciliation_runs WHERE id = ?", (result.run_id,)
        ).fetchone()
        mismatch_rows = conn.execute(
            "SELECT * FROM reconciliation_mismatches WHERE run_id = ?", (result.run_id,)
        ).fetchall()

    assert run_row["status"] == "completed"
    assert run_row["critical_mismatch_count"] == 1
    assert run_row["entries_blocked"] == 1
    assert len(mismatch_rows) == 1
    assert mismatch_rows[0]["category"] == "BROKER_ONLY"
    assert mismatch_rows[0]["resolved_at"] is None  # never silently resolved


def test_latest_run_returns_the_most_recent(tmp_path: Path):
    db = _database(tmp_path)
    runner = ReconciliationRunner(db)
    broker = _FakeBroker()
    first = runner.run(
        runtime_id="intraday_options", strategy_id="st01", broker=broker,
        local_orders=[], local_positions=[],
    )
    second = runner.run(
        runtime_id="intraday_options", strategy_id="st01", broker=broker,
        local_orders=[], local_positions=[],
    )
    latest = runner.latest_run(runtime_id="intraday_options", strategy_id="st01")
    assert latest["id"] == second.run_id
    assert latest["id"] != first.run_id


def test_open_critical_mismatches_only_returns_unresolved_critical_rows(tmp_path: Path):
    db = _database(tmp_path)
    runner = ReconciliationRunner(db)
    broker = _FakeBroker(
        orders=(_broker_order("c_unknown", OrderStatus.ACKNOWLEDGED),),
        positions=(
            BrokerPosition(
                security_id="sec1", quantity=75, average_price=190.0, product_type="INTRADAY"
            ),
        ),
    )
    result = runner.run(
        runtime_id="intraday_options",
        strategy_id="st01",
        broker=broker,
        local_orders=[],
        local_positions=[
            LocalPositionState(
                security_id="sec1",
                quantity=75,
                average_price=195.0,
                product_type="INTRADAY",
                status="OPEN",
            ),
        ],
    )
    open_critical = runner.open_critical_mismatches(
        runtime_id="intraday_options", strategy_id="st01"
    )
    # BROKER_ONLY order (critical) + PRICE_MISMATCH position (not critical) = 1 open critical row
    assert len(open_critical) == 1
    assert open_critical[0]["category"] == "BROKER_ONLY"
    assert result.critical_mismatch_count == 1
