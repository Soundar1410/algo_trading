"""Live mark-to-market for positional cycle legs (migration 0015).

The number persisted here is not a new one — it is what the exit ladder
already runs on (`LegInstance.unrealised_pnl` -> `Cycle.unrealised_gross_pnl`
-> `compute_pnl`). Until this table it never left the worker's memory, so a
read-only dashboard could show what a leg cost and what it eventually made,
but never what it was worth right now.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from common.config.models import ExecutionMode
from common.engine.multi_leg_models import LegInstance, LegRole, LegState
from common.execution import ExecutionRepository
from common.models import OrderSide
from common.persistence import Database, MigrationRunner, connect_readonly
from dashboards.data.positional import load_legs_for_cycle

RUNTIME_ID = "positional_options"
STRATEGY_ID = "weekly_delta_neutral"
CYCLE_ID = "positional_options:weekly_delta_neutral:paper:NIFTY:2026-09-15"


@pytest.fixture
def repository(database_path: Path) -> ExecutionRepository:
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    return ExecutionRepository(database)


def _write(
    repository: ExecutionRepository,
    leg_id: str,
    *,
    last_price: float,
    unrealised: float,
    favorable: float = 0.0,
    adverse: float = 0.0,
) -> None:
    repository.update_cycle_leg_marks(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        cycle_id=CYCLE_ID,
        leg_id=leg_id,
        last_price=last_price,
        unrealised_pnl=unrealised,
        max_favorable_pnl=favorable,
        max_adverse_pnl=adverse,
    )


def _marks(database_path: Path) -> list[tuple]:
    conn = connect_readonly(database_path)
    try:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT leg_id, last_price, unrealised_pnl, max_favorable_pnl, "
                "max_adverse_pnl FROM cycle_leg_marks ORDER BY leg_id"
            )
        ]
    finally:
        conn.close()


# ================================================================= migration
def test_the_table_exists_after_migration(repository: ExecutionRepository, database_path: Path):
    conn = connect_readonly(database_path)
    try:
        (name,) = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cycle_leg_marks'"
        ).fetchone()
    finally:
        conn.close()
    assert name == "cycle_leg_marks"


def test_running_migrations_again_is_a_no_op(repository: ExecutionRepository, database_path: Path):
    """``MigrationRunner`` replayability, the reason this is a new table
    rather than an ``ALTER TABLE`` on ``strategy_cycle_legs``."""
    _write(repository, "leg-1", last_price=10.0, unrealised=100.0)
    MigrationRunner(Database(database_path)).run_pending()
    assert len(_marks(database_path)) == 1


def test_the_table_carries_its_own_strategy_id_so_a_purge_can_see_it(
    repository: ExecutionRepository, database_path: Path
):
    """The lesson from the 9 September 2026 purge, which orphaned 46
    ``paper_fill_quotes`` rows because that table hangs off a parent id while
    carrying no ``strategy_id`` of its own. This one carries the full
    identity, so the purge script sweeps it with no special case."""
    conn = connect_readonly(database_path)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(cycle_leg_marks)")}
        foreign_keys = list(conn.execute("PRAGMA foreign_key_list(cycle_leg_marks)"))
    finally:
        conn.close()

    assert "strategy_id" in columns
    assert foreign_keys == [], "a foreign key here would reintroduce the orphaning risk"


# ==================================================================== upsert
def test_a_second_mark_replaces_the_first_rather_than_appending(
    repository: ExecutionRepository, database_path: Path
):
    """A cycle's worth is the sum over this table, so an append would make
    every re-mark double-count it."""
    _write(repository, "leg-1", last_price=10.0, unrealised=100.0)
    _write(repository, "leg-1", last_price=12.0, unrealised=-250.0)

    assert _marks(database_path) == [("leg-1", 12.0, -250.0, 0.0, 0.0)]


def test_marks_are_kept_separately_per_leg(
    repository: ExecutionRepository, database_path: Path
):
    _write(repository, "leg-1", last_price=10.0, unrealised=100.0)
    _write(repository, "leg-2", last_price=20.0, unrealised=-50.0)

    assert [m[0] for m in _marks(database_path)] == ["leg-1", "leg-2"]


def test_the_excursion_extremes_round_trip(
    repository: ExecutionRepository, database_path: Path
):
    _write(repository, "leg-1", last_price=9.0, unrealised=-30.0, favorable=120.0, adverse=-80.0)
    assert _marks(database_path) == [("leg-1", 9.0, -30.0, 120.0, -80.0)]


def test_as_of_is_stamped_by_the_repository_not_the_caller(
    repository: ExecutionRepository, database_path: Path
):
    """A caller cannot backdate a mark, so a reader's freshness judgement is
    always about when the write really happened."""
    before = datetime.now(UTC)
    _write(repository, "leg-1", last_price=10.0, unrealised=0.0)

    conn = connect_readonly(database_path)
    try:
        (as_of,) = conn.execute("SELECT as_of FROM cycle_leg_marks").fetchone()
    finally:
        conn.close()
    assert datetime.fromisoformat(as_of) >= before.replace(microsecond=0)


# ================================================= the sign convention itself
def _leg(side: OrderSide, entry: float, last: float) -> LegInstance:
    leg = LegInstance(
        leg_id="leg-1",
        basket_id=CYCLE_ID,
        role=LegRole.SHORT_CALL if side is OrderSide.SELL else LegRole.HEDGE_CALL,
        sequence=1,
        is_replacement=False,
        side=side,
        quantity=1300,
        state=LegState.OPEN,
        entry_price=entry,
    )
    leg.update_price(last)
    return leg


def test_a_short_leg_profits_as_the_option_cheapens():
    """Two of a condor's four legs are short and carry most of its P&L, so
    an inverted sign here would misreport the whole position."""
    sold_at_38_now_20 = _leg(OrderSide.SELL, entry=38.55, last=20.0)
    assert sold_at_38_now_20.unrealised_pnl == pytest.approx((38.55 - 20.0) * 1300)
    assert sold_at_38_now_20.unrealised_pnl > 0


def test_a_long_hedge_profits_as_the_option_richens():
    bought_at_13_now_25 = _leg(OrderSide.BUY, entry=13.85, last=25.0)
    assert bought_at_13_now_25.unrealised_pnl == pytest.approx((25.0 - 13.85) * 1300)
    assert bought_at_13_now_25.unrealised_pnl > 0


def test_a_leg_with_no_price_yet_reports_zero_not_a_guess():
    leg = LegInstance(
        leg_id="leg-1",
        basket_id=CYCLE_ID,
        role=LegRole.SHORT_PUT,
        sequence=1,
        is_replacement=False,
        side=OrderSide.SELL,
        quantity=1300,
        state=LegState.OPEN,
        entry_price=38.55,
    )
    assert leg.last_price is None
    assert leg.unrealised_pnl == 0.0


# ============================================================ dashboard read
def _persist_leg(repository: ExecutionRepository, leg_id: str, *, side: str, state: str) -> None:
    with repository.database.transaction() as conn:
        conn.execute(
            "INSERT INTO strategy_cycle_legs (runtime_id, strategy_id, execution_mode, "
            "cycle_id, leg_id, leg_role, leg_sequence, is_replacement, side, quantity, "
            "entry_price, entry_correlation_id, state, version, created_at, updated_at) "
            "VALUES (?, ?, 'paper', ?, ?, 'SHORT_CALL', 1, 0, ?, 1300, 38.55, ?, ?, 1, ?, ?)",
            (
                RUNTIME_ID,
                STRATEGY_ID,
                CYCLE_ID,
                leg_id,
                side,
                f"p_po_week_{leg_id}",
                state,
                "2026-09-09T05:27:52+00:00",
                "2026-09-09T05:27:52+00:00",
            ),
        )


def _fill_with_charges(
    repository: ExecutionRepository, correlation_id: str, charges: float
) -> None:
    """One ``fills`` row carrying real charges, plus the session/intent/order
    chain its foreign keys require.

    Written directly rather than through ``OrderLifecycle`` because these
    tests care only about the charges join; the real fill path is covered by
    the positional integration suite.
    """
    session = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=4242,
    )
    with repository.database.transaction() as conn:
        intent = conn.execute(
            "INSERT INTO order_intents (correlation_id, correlation_namespace, session_id, "
            "runtime_id, strategy_id, execution_mode, trading_date, sequence_number, "
            "instrument, security_id, side, quantity, order_type, product_type, "
            "risk_decision, created_at) "
            "VALUES (?, 'paper', ?, ?, ?, 'paper', '2026-09-09', 1, 'NIFTY', '47311', "
            "'SELL', 1300, 'LIMIT', 'MARGIN', 'ALLOW', ?)",
            (correlation_id, session.id, RUNTIME_ID, STRATEGY_ID, "2026-09-09T05:28:06+00:00"),
        ).lastrowid
        order = conn.execute(
            "INSERT INTO orders (intent_id, correlation_id, runtime_id, strategy_id, "
            "execution_mode, status, updated_at) "
            "VALUES (?, ?, ?, ?, 'paper', 'FILLED', ?)",
            (
                intent,
                correlation_id,
                RUNTIME_ID,
                STRATEGY_ID,
                "2026-09-09T05:28:06+00:00",
            ),
        ).lastrowid
        conn.execute(
            "INSERT INTO fills (order_id, correlation_id, runtime_id, strategy_id, "
            "execution_mode, broker_fill_id, quantity, price, charges, filled_at) "
            "VALUES (?, ?, ?, ?, 'paper', ?, 1300, 38.55, ?, ?)",
            (
                order,
                correlation_id,
                RUNTIME_ID,
                STRATEGY_ID,
                f"bf-{order}",
                charges,
                "2026-09-09T05:28:06+00:00",
            ),
        )


def test_the_dashboard_joins_the_mark_onto_the_leg(
    repository: ExecutionRepository, database_path: Path
):
    _persist_leg(repository, "leg-1", side="SELL", state="OPEN")
    _write(repository, "leg-1", last_price=20.0, unrealised=24115.0)

    conn = connect_readonly(database_path)
    try:
        (leg,) = load_legs_for_cycle(conn, cycle_id=CYCLE_ID)
    finally:
        conn.close()

    assert leg.last_price == pytest.approx(20.0)
    assert leg.unrealised_pnl == pytest.approx(24115.0)
    assert leg.mark_as_of is not None


def test_a_leg_with_no_mark_reports_none_never_zero(
    repository: ExecutionRepository, database_path: Path
):
    """The distinction the whole read model turns on: "not known" is not
    "worth nothing"."""
    _persist_leg(repository, "leg-1", side="SELL", state="OPEN")

    conn = connect_readonly(database_path)
    try:
        (leg,) = load_legs_for_cycle(conn, cycle_id=CYCLE_ID)
    finally:
        conn.close()

    assert leg.last_price is None
    assert leg.unrealised_pnl is None
    assert leg.net_pnl is None, "an unmarked open leg must not report its charges as a loss"


def test_charges_come_from_the_legs_own_fills(
    repository: ExecutionRepository, database_path: Path
):
    _persist_leg(repository, "leg-1", side="SELL", state="OPEN")
    _fill_with_charges(repository, "p_po_week_leg-1", 75.6963)

    conn = connect_readonly(database_path)
    try:
        (leg,) = load_legs_for_cycle(conn, cycle_id=CYCLE_ID)
    finally:
        conn.close()

    assert leg.entry_charges == pytest.approx(75.6963)
    assert leg.total_charges == pytest.approx(75.6963)
    assert leg.exit_charges is None


def test_a_leg_with_no_fill_reports_no_charges_rather_than_zero(
    repository: ExecutionRepository, database_path: Path
):
    _persist_leg(repository, "leg-1", side="SELL", state="OPEN")

    conn = connect_readonly(database_path)
    try:
        (leg,) = load_legs_for_cycle(conn, cycle_id=CYCLE_ID)
    finally:
        conn.close()

    assert leg.entry_charges is None
    assert leg.total_charges is None


def test_net_pnl_nets_the_live_mark_against_real_charges(
    repository: ExecutionRepository, database_path: Path
):
    _persist_leg(repository, "leg-1", side="SELL", state="OPEN")
    _write(repository, "leg-1", last_price=20.0, unrealised=24115.0)
    _fill_with_charges(repository, "p_po_week_leg-1", 75.6963)

    conn = connect_readonly(database_path)
    try:
        (leg,) = load_legs_for_cycle(conn, cycle_id=CYCLE_ID)
    finally:
        conn.close()

    assert leg.net_pnl == pytest.approx(24115.0 - 75.6963)


def test_a_closed_leg_nets_its_realised_pnl_not_its_stale_mark(
    repository: ExecutionRepository, database_path: Path
):
    """Once a leg closes, the mark is history and the realised figure is the
    truth — netting the mark instead would keep reporting a position that no
    longer exists."""
    _persist_leg(repository, "leg-1", side="SELL", state="CLOSED")
    _write(repository, "leg-1", last_price=20.0, unrealised=24115.0)
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE strategy_cycle_legs SET realized_gross_pnl = 30000.0 WHERE leg_id = 'leg-1'"
        )

    conn = connect_readonly(database_path)
    try:
        (leg,) = load_legs_for_cycle(conn, cycle_id=CYCLE_ID)
    finally:
        conn.close()

    assert leg.net_pnl == pytest.approx(30000.0)
