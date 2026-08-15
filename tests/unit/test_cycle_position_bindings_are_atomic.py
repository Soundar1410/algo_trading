"""Cross-day, cycle-scoped position resolution (spec review correction 4).

``ExecutionRepository.apply_fill``/``_upsert_position``/``_read_position``
gained an optional ``cycle_id`` parameter: when given, the position row is
resolved through ``cycle_position_bindings`` instead of
``(trading_date, security_id)``, so a fill on a later trading_date can still
reach a position row opened on an earlier one. ``positions.trading_date``
itself is never rewritten — it stays the row's opening date for its whole
life; the event's own date belongs on that event's own row (``order_intents``/
``orders``/``fills``), still written exactly as before.

Every existing intraday caller passes no ``cycle_id`` at all — this file
proves both halves: the new cycle-scoped behaviour, and that the
trading_date-scoped default is untouched (covered already by the existing
``tests/integration/test_execution_persistence.py`` suite, run as a
regression gate; not repeated here).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from common.broker import PaperBroker, PaperFillConfig, SlippageConfig
from common.config.models import ExecutionMode
from common.execution import ExecutionRepository, OrderLifecycle
from common.models import Candle, Side, Signal
from common.persistence import Database, MigrationRunner

IST = ZoneInfo("Asia/Kolkata")
STRATEGY_ID = "weekly_delta_neutral"
RUNTIME_ID = "positional_options"
SECURITY_ID = "54321"
CYCLE_ID = "wdn:2026-08-19"


@pytest.fixture
def repository(database_path: Path) -> ExecutionRepository:
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    return ExecutionRepository(database)


@pytest.fixture
def session(repository: ExecutionRepository):
    return repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=4242,
    )


def _candle(day: int, minute: int, close: float) -> Candle:
    start = datetime(2026, 8, day, 9, minute, tzinfo=IST)
    return Candle(
        security_id=SECURITY_ID,
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


def _signal(side: Side, day: int, minute: int, close: float) -> Signal:
    candle = _candle(day, minute, close)
    return Signal(
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        instrument="NIFTY",
        security_id=SECURITY_ID,
        side=side,
        quantity=75,
        candle=candle,
        reference_price=candle.close,
        evaluated_at=candle.end_at,
        reason="test",
    )


def _lifecycle(repository: ExecutionRepository, session) -> OrderLifecycle:
    return OrderLifecycle(
        repository=repository,
        broker=PaperBroker(
            config=PaperFillConfig(slippage=SlippageConfig(mode="points", market_order_points=0.0))
        ),
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        session_id=session.id,
    )


def test_a_later_day_fill_updates_the_same_row_opened_on_an_earlier_day(
    repository: ExecutionRepository, session
):
    """A short opened Wednesday and closed Friday must move the *same*
    positions row — never create a second, orphaned one dated Wednesday."""
    lifecycle = _lifecycle(repository, session)

    wed = lifecycle.handle_signal(
        _signal(Side.SELL, 19, 25, 100.0),
        trading_date="2026-08-19",
        basket_id=CYCLE_ID,
        leg_id="leg-short-call",
        cycle_id=CYCLE_ID,
    )
    assert wed.position is not None
    assert wed.position.trading_date == "2026-08-19"
    assert wed.position.quantity == -75

    fri = lifecycle.handle_signal(
        _signal(Side.BUY, 21, 15, 40.0),
        trading_date="2026-08-21",
        basket_id=CYCLE_ID,
        leg_id="leg-short-call",
        cycle_id=CYCLE_ID,
    )
    assert fri.position is not None
    # Closed flat, and — the whole point — trading_date is still Wednesday's:
    # this is the SAME row, updated in place, not a second Friday-dated one.
    assert fri.position.quantity == 0
    assert fri.position.trading_date == "2026-08-19"

    with repository.database.transaction() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE strategy_id = ? AND security_id = ?",
            (STRATEGY_ID, SECURITY_ID),
        ).fetchone()[0]
    assert rows == 1, "cross-day fills against the same cycle must never fragment into two rows"

    # Friday's own event date lives on Friday's own order_intents/orders/fills
    # rows — never on the position.
    with repository.database.transaction() as conn:
        dates = {
            row[0]
            for row in conn.execute(
                "SELECT trading_date FROM order_intents WHERE basket_id = ?", (CYCLE_ID,)
            ).fetchall()
        }
    assert dates == {"2026-08-19", "2026-08-21"}


def test_a_binding_is_created_atomically_with_the_first_fill(
    repository: ExecutionRepository, session
):
    lifecycle = _lifecycle(repository, session)
    lifecycle.handle_signal(
        _signal(Side.SELL, 19, 25, 100.0),
        trading_date="2026-08-19",
        basket_id=CYCLE_ID,
        leg_id="leg-short-call",
        cycle_id=CYCLE_ID,
    )
    with repository.database.transaction() as conn:
        row = conn.execute(
            "SELECT cycle_id, security_id, position_id FROM cycle_position_bindings "
            "WHERE cycle_id = ? AND security_id = ?",
            (CYCLE_ID, SECURITY_ID),
        ).fetchone()
    assert row is not None
    assert row["cycle_id"] == CYCLE_ID
    assert row["security_id"] == SECURITY_ID


def test_a_contradictory_binding_write_rolls_back_the_position_mutation(
    repository: ExecutionRepository, session
):
    """A dangling/contradictory binding (pointing at a position_id that does
    not exist, or a mismatched one) must fail the *whole* apply_fill
    transaction closed — the position INSERT it attempted must not survive
    the binding INSERT's own UNIQUE-constraint failure. This exercises the
    real production path (apply_fill -> _upsert_position), not a hand-rolled
    transaction, so the rollback is the actual one Database.transaction()
    performs."""
    # A pre-existing binding for (CYCLE_ID, SECURITY_ID) pointing at a
    # position_id that does not exist — the "dangling" shape a genuinely
    # contradictory prior write (or corrupted state) would leave behind.
    with repository.database.transaction() as conn:
        conn.execute(
            "INSERT INTO cycle_position_bindings (cycle_id, security_id, position_id, "
            "created_at) VALUES (?, ?, 999999, 'now')",
            (CYCLE_ID, SECURITY_ID),
        )
    before_count = _position_count(repository)

    lifecycle = _lifecycle(repository, session)
    # _read_position resolves the binding to position_id=999999, finds no
    # such row, and returns None -> _upsert_position takes the INSERT
    # branch, creates a position row, then tries to INSERT a *second*
    # binding for the same (cycle_id, security_id) — refused by the table's
    # own UNIQUE constraint, since one already exists (pointing elsewhere).
    with pytest.raises(Exception, match="UNIQUE constraint failed"):
        lifecycle.handle_signal(
            _signal(Side.SELL, 19, 25, 100.0),
            trading_date="2026-08-19",
            basket_id=CYCLE_ID,
            leg_id="leg-short-call",
            cycle_id=CYCLE_ID,
        )

    assert _position_count(repository) == before_count, (
        "the failed binding INSERT must have rolled back the position row "
        "apply_fill's own transaction had just created"
    )


def _position_count(repository: ExecutionRepository) -> int:
    with repository.database.transaction() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0])


def test_cycle_scoped_lookup_never_falls_back_to_trading_date_matching(
    repository: ExecutionRepository, session
):
    """An unrelated, ordinary position for the same security on an earlier
    trading_date must never be adopted by a cycle-scoped lookup that has no
    binding of its own yet — a binding-only lookup must return None (forcing
    a fresh row + a fresh binding), never fall back to matching on
    (trading_date, security_id)."""
    lifecycle = _lifecycle(repository, session)
    # An ordinary, non-cycle-scoped position on an unrelated earlier date.
    earlier = lifecycle.handle_signal(
        _signal(Side.BUY, 18, 25, 100.0),
        trading_date="2026-08-18",
    )
    assert earlier.position is not None
    assert earlier.position.trading_date == "2026-08-18"

    # A cycle-scoped entry for the *same security*, a different date, no
    # binding yet — must create a fresh row, never adopt the one above.
    result = lifecycle.handle_signal(
        _signal(Side.SELL, 19, 30, 100.0),
        trading_date="2026-08-19",
        basket_id=CYCLE_ID,
        leg_id="leg-short-call",
        cycle_id=CYCLE_ID,
    )
    assert result.position is not None
    assert result.position.quantity == -75
    assert result.position.trading_date == "2026-08-19"
    assert _position_count(repository) == 2

    with repository.database.transaction() as conn:
        unbound = conn.execute(
            "SELECT trading_date FROM positions WHERE trading_date = '2026-08-18'"
        ).fetchone()
    assert unbound is not None, "the earlier, unrelated position row must be untouched"
