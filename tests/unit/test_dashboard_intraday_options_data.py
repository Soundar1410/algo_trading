"""DB-backed tests for ``dashboards/data/intraday_options.py``.

Fixtures go through the real write path (``ExecutionRepository`` /
``OrderLifecycle`` / ``PaperBroker`` — the same lifecycle
``tests/integration/test_execution_persistence.py`` exercises) so every
read-model query here runs against exactly the schema and value shapes the
real worker produces, not a hand-typed row that might drift from it. Every
read goes through ``connect_readonly`` — the real dashboard connection path.
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
from common.persistence import Database, MigrationRunner, connect_readonly
from dashboards.data.calendar_stats import DailyOutcome
from dashboards.data.intraday_options import (
    build_strategy_comparison,
    load_capital_base,
    load_closed_trades,
    load_daily_outcomes,
    load_errors,
    load_inception_date,
    load_live_positions,
    load_notifications,
    load_orders,
    load_overview,
    load_signals,
)

IST = ZoneInfo("Asia/Kolkata")
RUNTIME_ID = "intraday_options"
STRATEGY_ID = "st01"
TRADING_DATE = "2026-08-10"


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
    start = datetime(2026, 8, 10, 9, minute, tzinfo=IST)
    return Candle(
        security_id="13",
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


def _signal(side: Side, minute: int, close: float) -> Signal:
    candle = _candle(minute, close)
    return Signal(
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        instrument="NIFTY",
        security_id="13",
        side=side,
        quantity=75,
        candle=candle,
        reference_price=candle.close,
        evaluated_at=candle.end_at,
        reason="ema_cross",
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


def _round_trip(repository: ExecutionRepository, session, *, entry: float, exit: float):
    lifecycle = _lifecycle(repository, session)
    entry_result = lifecycle.handle_signal(
        _signal(Side.BUY, minute=15, close=entry), trading_date=TRADING_DATE
    )
    exit_result = lifecycle.handle_signal(
        _signal(Side.SELL, minute=16, close=exit), trading_date=TRADING_DATE
    )
    return entry_result, exit_result


def _ro(database_path: Path):
    return connect_readonly(database_path)


# ================================================================ closed trades
def test_closed_trade_derives_entry_and_exit_price_and_side_from_fills(
    repository: ExecutionRepository, session, database_path: Path
):
    _round_trip(repository, session, entry=100.0, exit=110.0)

    conn = _ro(database_path)
    trades = load_closed_trades(
        conn, RUNTIME_ID, start_date=TRADING_DATE, end_date=TRADING_DATE
    )
    conn.close()

    assert len(trades) == 1
    trade = trades[0]
    assert trade.side == "BUY"
    # PaperBroker applies its own minimum-tick slippage even with
    # market_order_points=0.0 — these assertions check the read model reads
    # the real fill prices back correctly, not the broker's slippage model.
    assert trade.entry_price == pytest.approx(100.0, abs=0.2)
    assert trade.exit_price == pytest.approx(110.0, abs=0.2)
    assert trade.points == pytest.approx(10.0, abs=0.4)
    assert trade.quantity == 75
    assert trade.gross_pnl == pytest.approx(10.0 * 75, abs=30.0)
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.charges)


def test_closed_trades_filter_by_execution_mode_never_mix_paper_and_live(
    repository: ExecutionRepository, session, database_path: Path
):
    """Spec: paper and live P&L/orders/positions never mix."""
    _round_trip(repository, session, entry=100.0, exit=105.0)

    conn = _ro(database_path)
    paper = load_closed_trades(
        conn, RUNTIME_ID, execution_mode="paper", start_date=TRADING_DATE, end_date=TRADING_DATE
    )
    live = load_closed_trades(
        conn, RUNTIME_ID, execution_mode="live", start_date=TRADING_DATE, end_date=TRADING_DATE
    )
    conn.close()

    assert len(paper) == 1
    assert live == ()


def test_a_losing_trade_has_a_negative_points_and_net_pnl(
    repository: ExecutionRepository, session, database_path: Path
):
    _round_trip(repository, session, entry=100.0, exit=90.0)
    conn = _ro(database_path)
    trades = load_closed_trades(conn, RUNTIME_ID, start_date=TRADING_DATE, end_date=TRADING_DATE)
    conn.close()
    assert trades[0].points == pytest.approx(-10.0, abs=0.4)
    assert trades[0].net_pnl < 0


# ================================================================ orders/fills
def test_orders_carry_correlation_id_side_and_nested_fills(
    repository: ExecutionRepository, session, database_path: Path
):
    _round_trip(repository, session, entry=100.0, exit=110.0)
    conn = _ro(database_path)
    orders = load_orders(conn, RUNTIME_ID, TRADING_DATE)
    conn.close()

    assert len(orders) == 2  # entry + exit
    sides = {o.side for o in orders}
    assert sides == {"BUY", "SELL"}
    for order in orders:
        assert order.correlation_id
        assert order.status == "FILLED"
        assert len(order.fills) == 1
        assert order.fills[0].price > 0


# ============================================================ live positions
def test_an_open_position_shows_up_in_live_positions_until_closed(
    repository: ExecutionRepository, session, database_path: Path
):
    lifecycle = _lifecycle(repository, session)
    lifecycle.handle_signal(_signal(Side.BUY, minute=15, close=100.0), trading_date=TRADING_DATE)

    conn = _ro(database_path)
    open_positions = load_live_positions(conn, RUNTIME_ID, TRADING_DATE)
    conn.close()

    assert len(open_positions) == 1
    assert open_positions[0].side == "BUY"
    assert open_positions[0].quantity == 75
    assert open_positions[0].entry_price == pytest.approx(100.0, abs=0.2)

    lifecycle.handle_signal(_signal(Side.SELL, minute=16, close=105.0), trading_date=TRADING_DATE)
    conn = _ro(database_path)
    open_after_exit = load_live_positions(conn, RUNTIME_ID, TRADING_DATE)
    conn.close()
    assert open_after_exit == ()


def test_live_positions_mode_filter_separates_paper_from_live(
    repository: ExecutionRepository, session, database_path: Path
):
    """The Open Positions tab's Mode dropdown (All/Paper/Live), same filter
    shape Orders & Fills and Closed Trades already have.

    The live half is a direct row insert, not a real ``OrderLifecycle`` run:
    a live-mode lifecycle refuses to fill at all without an account
    reservation gate wired (Phase 10 safety machinery, deliberately absent
    here) — this test only needs a genuine ``positions`` row with
    ``execution_mode='live'`` to prove the read-model's filter, not a full
    live order round trip."""
    paper_lifecycle = _lifecycle(repository, session)
    paper_lifecycle.handle_signal(
        _signal(Side.BUY, minute=15, close=100.0), trading_date=TRADING_DATE
    )

    now = datetime.now(IST).isoformat()
    with repository.database.transaction() as conn:
        conn.execute(
            "INSERT INTO positions (runtime_id, strategy_id, execution_mode, trading_date, "
            "instrument, security_id, quantity, average_price, status, opened_at, updated_at) "
            "VALUES (?, ?, 'live', ?, 'NIFTY', '21', 75, 200.0, 'OPEN', ?, ?)",
            (RUNTIME_ID, STRATEGY_ID, TRADING_DATE, now, now),
        )

    conn = _ro(database_path)
    everyone = load_live_positions(conn, RUNTIME_ID, TRADING_DATE)
    paper_only = load_live_positions(conn, RUNTIME_ID, TRADING_DATE, execution_mode="paper")
    live_only = load_live_positions(conn, RUNTIME_ID, TRADING_DATE, execution_mode="live")
    conn.close()

    assert {p.security_id for p in everyone} == {"13", "21"}
    assert {p.security_id for p in paper_only} == {"13"}
    assert {p.security_id for p in live_only} == {"21"}


# =================================================================== overview
def test_overview_reflects_open_position_and_todays_closed_pnl(
    repository: ExecutionRepository, session, database_path: Path
):
    repository.record_heartbeat(
        session_id=session.id, runtime_id=RUNTIME_ID, strategy_id=STRATEGY_ID,
        health_state="RUNNING_PAPER",
    )
    _round_trip(repository, session, entry=100.0, exit=110.0)

    conn = _ro(database_path)
    rows = load_overview(conn, RUNTIME_ID, TRADING_DATE)
    conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row.strategy_id == STRATEGY_ID
    assert row.execution_mode == "paper"
    assert row.today_trade_count == 1
    assert row.today_net_pnl > 0
    assert row.current_position_instrument is None  # closed, nothing open


# ==================================================================== signals
def test_signals_carry_the_exact_candle_and_matching_order(
    repository: ExecutionRepository, session, database_path: Path
):
    _round_trip(repository, session, entry=100.0, exit=110.0)
    conn = _ro(database_path)
    signals = load_signals(conn, RUNTIME_ID, TRADING_DATE)
    conn.close()

    assert len(signals) == 2
    entry_signal = next(s for s in signals if s.side == "BUY")
    assert entry_signal.candle_close == pytest.approx(100.0)
    assert entry_signal.order_correlation_id is not None


# ============================================================ calendar/daily
def test_daily_outcomes_mark_the_trading_date_as_ran_and_executed(
    repository: ExecutionRepository, session, database_path: Path
):
    # open_session always stamps "now" as started_at; a real supervisor's
    # session date and the trading_date it processes are the same in
    # production (opened fresh each morning) — backdate here to reproduce
    # that alignment for a fixture that deliberately uses a fixed historical
    # TRADING_DATE.
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE runtime_sessions SET started_at = ? WHERE id = ?",
            (f"{TRADING_DATE}T09:00:00+00:00", session.id),
        )
    _round_trip(repository, session, entry=100.0, exit=110.0)
    conn = _ro(database_path)
    outcomes = load_daily_outcomes(
        conn, RUNTIME_ID, strategy_id=STRATEGY_ID, execution_mode=None,
        start_date=TRADING_DATE, end_date=TRADING_DATE,
    )
    inception = load_inception_date(conn, RUNTIME_ID)
    conn.close()

    assert len(outcomes) == 1
    assert outcomes[0] == DailyOutcome(
        trading_date=outcomes[0].trading_date, ran=True, trade_count=1,
        net_pnl=outcomes[0].net_pnl,
    )
    assert outcomes[0].net_pnl > 0
    assert inception is not None
    assert inception.isoformat() == TRADING_DATE


def test_inception_date_is_none_when_nothing_has_ever_run(database_path: Path):
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    conn = _ro(database_path)
    inception = load_inception_date(conn, RUNTIME_ID)
    conn.close()
    assert inception is None


# ============================================================= notifications
def test_notifications_and_errors_round_trip(
    repository: ExecutionRepository, session, database_path: Path
):
    repository.record_notification(
        runtime_id=RUNTIME_ID, strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER,
        channel="telegram", event_type="order_filled", message="filled", delivered=False,
        failure_reason="timeout",
    )
    repository.record_error(
        runtime_id=RUNTIME_ID, strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER,
        severity="ERROR", component="engine", message="boom",
    )

    conn = _ro(database_path)
    notifications = load_notifications(conn, RUNTIME_ID)
    errors = load_errors(conn, RUNTIME_ID)
    conn.close()

    assert len(notifications) == 1
    assert notifications[0].delivered is False
    assert notifications[0].failure_reason == "timeout"
    assert len(errors) == 1
    assert errors[0].component == "engine"


# ========================================================= strategy comparison
def test_capital_base_is_read_from_parameters_block(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir(parents=True)
    (strategies_dir / f"{STRATEGY_ID}.yaml").write_text(
        "strategy_id: st01\nparameters:\n  capital_base: 1000000\n", encoding="utf-8"
    )
    assert load_capital_base(tmp_path, STRATEGY_ID) == 1000000.0


def test_capital_base_is_none_when_not_declared(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir(parents=True)
    (strategies_dir / f"{STRATEGY_ID}.yaml").write_text(
        "strategy_id: st01\nparameters:\n  instrument: NIFTY\n", encoding="utf-8"
    )
    assert load_capital_base(tmp_path, STRATEGY_ID) is None


def test_strategy_comparison_computes_roi_only_when_capital_base_declared(
    repository: ExecutionRepository, session, database_path: Path, tmp_path: Path
):
    _round_trip(repository, session, entry=100.0, exit=110.0)
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir(parents=True)
    (strategies_dir / f"{STRATEGY_ID}.yaml").write_text(
        "strategy_id: st01\nparameters:\n  capital_base: 1000000\n", encoding="utf-8"
    )

    conn = _ro(database_path)
    rows = build_strategy_comparison(
        conn, RUNTIME_ID, tmp_path, (STRATEGY_ID,),
        start_date=TRADING_DATE, end_date=TRADING_DATE,
    )
    conn.close()

    assert len(rows) == 1
    assert rows[0].metrics.sample_size == 1
    assert rows[0].roi_pct == pytest.approx(rows[0].metrics.net_profit / 1_000_000 * 100.0)


def test_strategy_comparison_roi_is_none_without_a_declared_capital_base(
    repository: ExecutionRepository, session, database_path: Path, tmp_path: Path
):
    _round_trip(repository, session, entry=100.0, exit=110.0)
    (tmp_path / "strategies").mkdir(parents=True)

    conn = _ro(database_path)
    rows = build_strategy_comparison(
        conn, RUNTIME_ID, tmp_path, (STRATEGY_ID,),
        start_date=TRADING_DATE, end_date=TRADING_DATE,
    )
    conn.close()

    assert rows[0].roi_pct is None
