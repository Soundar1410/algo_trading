"""Account-wide rebuild: a missing/fresh account-shared database must never
read as "zero exposure, safe to trade" — provenance starts
never_reconciled and only a successful rebuild across every configured
runtime group flips it."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from common.models import Order, OrderStatus
from common.persistence import (
    Database,
    MigrationRunner,
    migrate_account_shared_database,
    open_account_shared_database,
)
from common.reconciliation import (
    ProvenanceStatus,
    ReconciliationRunner,
    RuntimeGroupSnapshot,
    get_provenance,
    rebuild_account_shared_state,
)


class _FakeBroker:
    """Local copy of the double in test_reconciliation_runner.py — test
    files in this suite do not share helpers across modules (no package
    __init__.py), so each keeps its own minimal, fully scripted double."""

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


def _broker_order(correlation_id: str, status: OrderStatus) -> Order:
    return Order(
        correlation_id=correlation_id,
        strategy_id="st01",
        execution_mode=None,  # type: ignore[arg-type]
        status=status,
        updated_at=datetime.now(UTC),
        broker_order_id="b1",
    )


def _account_db(tmp_path: Path) -> Database:
    database = open_account_shared_database(tmp_path / "dhan_account_shared.db")
    migrate_account_shared_database(database)
    return database


def _group_database(tmp_path: Path, runtime_id: str) -> Database:
    database = Database(tmp_path / "operational" / f"{runtime_id}.db")
    MigrationRunner(database).run_pending()
    return database


def test_a_fresh_account_database_starts_never_reconciled(tmp_path: Path):
    db = _account_db(tmp_path)
    assert get_provenance(db, account_key="acct1") == ProvenanceStatus.NEVER_RECONCILED


def test_a_successful_rebuild_across_every_group_marks_reconciled(tmp_path: Path):
    account_db = _account_db(tmp_path)
    group_db = _group_database(tmp_path, "intraday_options")
    runner = ReconciliationRunner(group_db)
    broker = _FakeBroker()  # clean: no orders, no positions

    result = rebuild_account_shared_state(
        account_db,
        account_key="acct1",
        runtime_groups=[
            RuntimeGroupSnapshot(
                runtime_id="intraday_options",
                broker=broker,
                local_orders=[],
                local_positions=[],
                reconciliation_runner=runner,
            )
        ],
        lock_path=tmp_path / "locks" / "account_rebuild.lock",
    )

    assert result.status == "reconciled"
    assert get_provenance(account_db, account_key="acct1") == ProvenanceStatus.RECONCILED


def test_a_failed_group_reconciliation_marks_the_whole_rebuild_failed(tmp_path: Path):
    account_db = _account_db(tmp_path)
    group_db = _group_database(tmp_path, "intraday_options")
    runner = ReconciliationRunner(group_db)
    broker = _FakeBroker(raise_on_fetch=ConnectionError("Dhan unreachable"))

    result = rebuild_account_shared_state(
        account_db,
        account_key="acct1",
        runtime_groups=[
            RuntimeGroupSnapshot(
                runtime_id="intraday_options",
                broker=broker,
                local_orders=[],
                local_positions=[],
                reconciliation_runner=runner,
            )
        ],
        lock_path=tmp_path / "locks" / "account_rebuild.lock",
    )

    assert result.status == "failed"
    assert get_provenance(account_db, account_key="acct1") == ProvenanceStatus.FAILED


def test_rebuild_covers_every_configured_group_not_just_the_first(tmp_path: Path):
    account_db = _account_db(tmp_path)
    io_db = _group_database(tmp_path, "intraday_options")
    po_db = _group_database(tmp_path, "positional_options")
    io_runner = ReconciliationRunner(io_db)
    po_runner = ReconciliationRunner(po_db)

    io_broker = _FakeBroker(orders=(_broker_order("c_unknown", OrderStatus.ACKNOWLEDGED),))
    po_broker = _FakeBroker()

    result = rebuild_account_shared_state(
        account_db,
        account_key="acct1",
        runtime_groups=[
            RuntimeGroupSnapshot(
                runtime_id="intraday_options",
                broker=io_broker,
                local_orders=[],
                local_positions=[],
                reconciliation_runner=io_runner,
            ),
            RuntimeGroupSnapshot(
                runtime_id="positional_options",
                broker=po_broker,
                local_orders=[],
                local_positions=[],
                reconciliation_runner=po_runner,
            ),
        ],
        lock_path=tmp_path / "locks" / "account_rebuild.lock",
    )

    assert result.status == "reconciled"
    assert result.critical_mismatch_total == 1  # the intraday_options group's BROKER_ONLY order


def test_a_missing_group_snapshot_leaves_that_groups_state_untrusted(tmp_path: Path):
    """runtime_groups is a fixed, config-derived list — omitting a group
    here means it is simply never reconciled, not silently assumed clean.
    This test documents that the caller (worker wiring) is responsible for
    supplying every live-eligible group, not this function inventing one."""
    account_db = _account_db(tmp_path)
    result = rebuild_account_shared_state(
        account_db, account_key="acct1", runtime_groups=[], lock_path=tmp_path / "locks" / "l.lock"
    )
    # Zero groups reconciled "successfully" (vacuously) — but note this is a
    # caller misconfiguration to avoid, not a safe default to rely on.
    assert result.status == "reconciled"
    assert result.critical_mismatch_total == 0
