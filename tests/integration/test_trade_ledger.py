"""trade_ledger: the durable per-trade record ExecutionRepository writes
alongside positions/fills — the source of truth the dashboard's Closed
Trades/Performance/Comparison tabs now read from instead of re-deriving
entry/exit price from positions+fills at read time.

Same real-lifecycle fixture pattern as
``tests/integration/test_execution_persistence.py``: every trade here goes
through the real ``OrderLifecycle``/``PaperBroker`` write path, not a
hand-typed row, so a query that only works against a shape this code never
actually produces would fail here the same way it would in production.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from common.broker import PaperBroker, PaperFillConfig, SlippageConfig
from common.config.models import ExecutionMode
from common.execution import ExecutionRepository, OrderLifecycle
from common.models import Candle, Fill, OrderStatus, Side, Signal
from common.persistence import Database, MigrationRunner

IST = ZoneInfo("Asia/Kolkata")
RUNTIME_ID = "intraday_options"
STRATEGY_ID = "st01"
SECURITY_ID = "99926000"
TRADING_DATE = "2026-08-14"


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


def _candle(minute: int, close: float) -> Candle:
    start = datetime(2026, 8, 14, 9, minute, tzinfo=IST)
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


def _signal(side: Side, minute: int, close: float, quantity: int = 50) -> Signal:
    candle = _candle(minute, close)
    return Signal(
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        instrument="NIFTY",
        security_id=SECURITY_ID,
        side=side,
        quantity=quantity,
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


def _create_order(
    repository: ExecutionRepository,
    *,
    correlation_id: str,
    side: Side,
    quantity: int,
    execution_mode: ExecutionMode,
    sequence: int,
    session_id: int,
) -> int:
    """A minimal order_intents + orders row pair, inserted directly —
    repository-level fill application (``apply_fill``) does not care how
    they got there, and this sidesteps ``OrderLifecycle``'s live-mode
    account-reservation gate entirely, which is exactly the point: this
    test proves the ledger's own mode separation, not the live order
    safety pipeline (covered elsewhere)."""
    now = datetime.now(UTC).isoformat()
    with repository.database.transaction() as conn:
        conn.execute(
            "INSERT INTO order_intents (correlation_id, correlation_namespace, session_id, "
            "runtime_id, strategy_id, execution_mode, trading_date, sequence_number, "
            "instrument, security_id, side, quantity, order_type, product_type, "
            "risk_decision, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'NIFTY', ?, ?, ?, "
            "'MARKET', 'INTRADAY', 'ALLOWED', ?)",
            (
                correlation_id,
                execution_mode.value,
                session_id,
                RUNTIME_ID,
                STRATEGY_ID,
                execution_mode.value,
                TRADING_DATE,
                sequence,
                SECURITY_ID,
                side.value,
                quantity,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO orders (intent_id, correlation_id, runtime_id, strategy_id, "
            "execution_mode, status, filled_quantity, updated_at) VALUES "
            "((SELECT id FROM order_intents WHERE correlation_id = ?), ?, ?, ?, ?, "
            "'PENDING', 0, ?)",
            (correlation_id, correlation_id, RUNTIME_ID, STRATEGY_ID, execution_mode.value, now),
        )
        return int(
            conn.execute(
                "SELECT id FROM orders WHERE correlation_id = ?", (correlation_id,)
            ).fetchone()["id"]
        )


def _direct_fill(
    repository: ExecutionRepository,
    *,
    order_id: int,
    correlation_id: str,
    broker_fill_id: str,
    side: Side,
    quantity: int,
    price: float,
    execution_mode: ExecutionMode,
    filled_at: datetime,
):
    fill = Fill(
        correlation_id=correlation_id,
        broker_fill_id=broker_fill_id,
        strategy_id=STRATEGY_ID,
        execution_mode=execution_mode,
        quantity=quantity,
        price=price,
        filled_at=filled_at,
        charges=1.0,
    )
    return repository.apply_fill(
        order_id=order_id,
        runtime_id=RUNTIME_ID,
        fill=fill,
        order_status=OrderStatus.FILLED,
        instrument="NIFTY",
        security_id=SECURITY_ID,
        side=side,
        trading_date=TRADING_DATE,
    )


def _ledger_rows(repository: ExecutionRepository) -> list:
    return list(
        repository.database.connect().execute(
            "SELECT * FROM trade_ledger ORDER BY id ASC"
        )
    )


# ================================================================= full close
def test_a_full_close_writes_one_exact_ledger_row(repository: ExecutionRepository, session):
    lifecycle = _lifecycle(repository, session)
    lifecycle.handle_signal(_signal(Side.BUY, minute=15, close=100.0), trading_date=TRADING_DATE)
    lifecycle.handle_signal(_signal(Side.SELL, minute=16, close=110.0), trading_date=TRADING_DATE)

    rows = _ledger_rows(repository)
    assert len(rows) == 1
    row = rows[0]
    assert row["runtime_id"] == RUNTIME_ID
    assert row["strategy_id"] == STRATEGY_ID
    assert row["execution_mode"] == "paper"
    assert row["trading_date"] == TRADING_DATE
    assert row["entry_side"] == "BUY"
    assert row["quantity"] == 50
    # PaperBroker applies its own minimum-tick slippage even with
    # market_order_points=0.0 — these assertions check the ledger reads
    # the real fill prices back correctly, not the broker's slippage model.
    assert row["entry_price"] == pytest.approx(100.0, abs=0.2)
    assert row["exit_price"] == pytest.approx(110.0, abs=0.2)
    assert row["gross_pnl"] == pytest.approx(10.0 * 50, abs=30.0)
    assert row["entry_charges"] >= 0.0
    assert row["exit_charges"] >= 0.0
    assert row["opened_at"] is not None
    assert row["closed_at"] is not None
    assert row["exit_correlation_id"] is not None
    assert row["exit_broker_fill_id"] is not None


def test_a_losing_trade_has_negative_gross_pnl(repository: ExecutionRepository, session):
    lifecycle = _lifecycle(repository, session)
    lifecycle.handle_signal(_signal(Side.BUY, minute=15, close=100.0), trading_date=TRADING_DATE)
    lifecycle.handle_signal(_signal(Side.SELL, minute=16, close=90.0), trading_date=TRADING_DATE)

    rows = _ledger_rows(repository)
    assert rows[0]["gross_pnl"] == pytest.approx(-10.0 * 50, abs=30.0)


def test_a_short_entry_realises_correctly(repository: ExecutionRepository, session):
    """entry_side reflects the leg that opened, not the fill that closed
    it — a short entry (SELL) covered by a BUY still reports entry_side
    SELL and the correct signed P&L."""
    lifecycle = _lifecycle(repository, session)
    lifecycle.handle_signal(_signal(Side.SELL, minute=15, close=100.0), trading_date=TRADING_DATE)
    lifecycle.handle_signal(_signal(Side.BUY, minute=16, close=90.0), trading_date=TRADING_DATE)

    rows = _ledger_rows(repository)
    assert rows[0]["entry_side"] == "SELL"
    assert rows[0]["entry_price"] == pytest.approx(100.0, abs=0.2)
    assert rows[0]["exit_price"] == pytest.approx(90.0, abs=0.2)
    # short profits when price falls
    assert rows[0]["gross_pnl"] == pytest.approx(10.0 * 50, abs=30.0)


# ============================================================== idempotency
def test_replaying_the_same_fill_does_not_duplicate_the_ledger_row(
    repository: ExecutionRepository, session
):
    lifecycle = _lifecycle(repository, session)
    lifecycle.handle_signal(_signal(Side.BUY, minute=15, close=100.0), trading_date=TRADING_DATE)
    exit_result = lifecycle.handle_signal(
        _signal(Side.SELL, minute=16, close=110.0), trading_date=TRADING_DATE
    )
    assert len(_ledger_rows(repository)) == 1

    # Replay the exact same closing fill through apply_fill directly —
    # apply_fill's own (order_id, broker_fill_id) guard returns early
    # before this code path is reached at all.
    order = exit_result.order
    fill = order.fills[-1]
    repository.apply_fill(
        order_id=repository.database.connect().execute(
            "SELECT id FROM orders WHERE correlation_id = ?", (order.correlation_id,)
        ).fetchone()["id"],
        runtime_id=RUNTIME_ID,
        fill=fill,
        order_status=order.status,
        instrument="NIFTY",
        security_id=SECURITY_ID,
        side=Side.SELL,
        trading_date=TRADING_DATE,
    )
    assert len(_ledger_rows(repository)) == 1


# ================================================================ the reopen fix
def test_reopening_after_a_full_close_writes_a_second_correctly_timestamped_row(
    repository: ExecutionRepository, session
):
    """The regression this migration exists to fix: a BUY -> SELL -> BUY ->
    SELL sequence on the same (strategy, mode, day, security) identity must
    produce two distinct ledger rows, each with its own entry price and
    entry time — not the first entry's stale values repeated, and not a
    single row silently overwritten out of existence."""
    lifecycle = _lifecycle(repository, session)
    lifecycle.handle_signal(_signal(Side.BUY, minute=15, close=100.0), trading_date=TRADING_DATE)
    lifecycle.handle_signal(_signal(Side.SELL, minute=16, close=110.0), trading_date=TRADING_DATE)
    lifecycle.handle_signal(_signal(Side.BUY, minute=30, close=200.0), trading_date=TRADING_DATE)
    lifecycle.handle_signal(_signal(Side.SELL, minute=31, close=190.0), trading_date=TRADING_DATE)

    rows = _ledger_rows(repository)
    assert len(rows) == 2
    first, second = rows
    assert first["entry_price"] == pytest.approx(100.0, abs=0.2)
    assert first["exit_price"] == pytest.approx(110.0, abs=0.2)
    assert first["gross_pnl"] == pytest.approx(10.0 * 50, abs=30.0)
    assert second["entry_price"] == pytest.approx(200.0, abs=0.2)
    assert second["exit_price"] == pytest.approx(190.0, abs=0.2)
    assert second["gross_pnl"] == pytest.approx(-10.0 * 50, abs=30.0)
    # The second round trip's own entry time, not the first's.
    assert second["opened_at"] != first["opened_at"]
    assert second["opened_at"] > first["opened_at"]
    assert second["entry_correlation_id"] != first["entry_correlation_id"]


def test_positions_own_opened_at_and_entry_correlation_id_reset_on_reopen(
    repository: ExecutionRepository, session
):
    """The same fix, checked directly against positions — not just its
    downstream effect on the ledger."""
    lifecycle = _lifecycle(repository, session)
    lifecycle.handle_signal(_signal(Side.BUY, minute=15, close=100.0), trading_date=TRADING_DATE)
    lifecycle.handle_signal(_signal(Side.SELL, minute=16, close=110.0), trading_date=TRADING_DATE)
    after_first_close = repository.database.connect().execute(
        "SELECT opened_at, entry_correlation_id FROM positions "
        "WHERE strategy_id = ? AND security_id = ?",
        (STRATEGY_ID, SECURITY_ID),
    ).fetchone()

    lifecycle.handle_signal(_signal(Side.BUY, minute=30, close=200.0), trading_date=TRADING_DATE)
    after_reopen = repository.database.connect().execute(
        "SELECT opened_at, entry_correlation_id, status FROM positions "
        "WHERE strategy_id = ? AND security_id = ?",
        (STRATEGY_ID, SECURITY_ID),
    ).fetchone()

    assert after_reopen["status"] == "OPEN"
    assert after_reopen["opened_at"] != after_first_close["opened_at"]
    assert after_reopen["opened_at"] > after_first_close["opened_at"]
    assert after_reopen["entry_correlation_id"] != after_first_close["entry_correlation_id"]


def test_scaling_into_an_open_position_does_not_reset_opened_at(
    repository: ExecutionRepository, session
):
    """Adding to an already-open position (not reopening from flat) must
    keep the original entry time — only a genuine reopen from zero resets
    it."""
    lifecycle = _lifecycle(repository, session)
    lifecycle.handle_signal(
        _signal(Side.BUY, minute=15, close=100.0, quantity=50), trading_date=TRADING_DATE
    )
    before_scale_in = repository.database.connect().execute(
        "SELECT opened_at, entry_correlation_id FROM positions "
        "WHERE strategy_id = ? AND security_id = ?",
        (STRATEGY_ID, SECURITY_ID),
    ).fetchone()

    lifecycle.handle_signal(
        _signal(Side.BUY, minute=16, close=102.0, quantity=25), trading_date=TRADING_DATE
    )
    after_scale_in = repository.database.connect().execute(
        "SELECT opened_at, entry_correlation_id, quantity FROM positions "
        "WHERE strategy_id = ? AND security_id = ?",
        (STRATEGY_ID, SECURITY_ID),
    ).fetchone()

    assert after_scale_in["quantity"] == 75
    assert after_scale_in["opened_at"] == before_scale_in["opened_at"]
    assert after_scale_in["entry_correlation_id"] == before_scale_in["entry_correlation_id"]


# =============================================================== mode isolation
def test_paper_and_live_ledger_rows_never_mix(repository: ExecutionRepository, session):
    paper_lifecycle = _lifecycle(repository, session)
    paper_lifecycle.handle_signal(
        _signal(Side.BUY, minute=15, close=100.0), trading_date=TRADING_DATE
    )
    paper_lifecycle.handle_signal(
        _signal(Side.SELL, minute=16, close=110.0), trading_date=TRADING_DATE
    )

    live_session = repository.open_session(
        runtime_id=RUNTIME_ID, strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.LIVE,
        process_role="worker", pid=2,
    )
    entry_order_id = _create_order(
        repository, correlation_id="l_st01_entry", side=Side.BUY, quantity=50,
        execution_mode=ExecutionMode.LIVE, sequence=1, session_id=live_session.id,
    )
    _direct_fill(
        repository, order_id=entry_order_id, correlation_id="l_st01_entry",
        broker_fill_id="l_fill_entry", side=Side.BUY, quantity=50, price=100.0,
        execution_mode=ExecutionMode.LIVE, filled_at=datetime(2026, 8, 14, 9, 15, tzinfo=UTC),
    )
    exit_order_id = _create_order(
        repository, correlation_id="l_st01_exit", side=Side.SELL, quantity=50,
        execution_mode=ExecutionMode.LIVE, sequence=2, session_id=live_session.id,
    )
    _direct_fill(
        repository, order_id=exit_order_id, correlation_id="l_st01_exit",
        broker_fill_id="l_fill_exit", side=Side.SELL, quantity=50, price=95.0,
        execution_mode=ExecutionMode.LIVE, filled_at=datetime(2026, 8, 14, 9, 16, tzinfo=UTC),
    )

    rows = _ledger_rows(repository)
    assert len(rows) == 2
    by_mode = {r["execution_mode"]: r for r in rows}
    assert by_mode["paper"]["gross_pnl"] == pytest.approx(10.0 * 50, abs=30.0)
    assert by_mode["live"]["gross_pnl"] == pytest.approx(-5.0 * 50)


# ==================================================================== charges
def test_entry_and_exit_charges_are_both_recorded_and_positive_when_paper_charges_apply(
    repository: ExecutionRepository, session
):
    lifecycle = _lifecycle(repository, session)
    lifecycle.handle_signal(_signal(Side.BUY, minute=15, close=100.0), trading_date=TRADING_DATE)
    lifecycle.handle_signal(_signal(Side.SELL, minute=16, close=110.0), trading_date=TRADING_DATE)

    row = _ledger_rows(repository)[0]
    # Whatever the paper charges model computes, the ledger's own total
    # must reconcile against positions.charges for a single, non-scaling
    # round trip (the only real shape today).
    position = repository.database.connect().execute(
        "SELECT charges FROM positions WHERE strategy_id = ? AND security_id = ?",
        (STRATEGY_ID, SECURITY_ID),
    ).fetchone()
    assert row["entry_charges"] + row["exit_charges"] == pytest.approx(position["charges"])
