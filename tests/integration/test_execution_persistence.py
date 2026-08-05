"""Execution persistence: transaction boundaries, idempotency, mode separation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from common.broker import PaperBroker, PaperFillConfig, QuoteBook, SlippageConfig
from common.config.models import ExecutionMode
from common.execution import ExecutionRepository, OrderLifecycle
from common.models import Candle, Fill, OrderStatus, PositionStatus, Side, Signal, Tick
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


def _lifecycle(
    repository: ExecutionRepository, session, broker=None, quotes=None
) -> OrderLifecycle:
    return OrderLifecycle(
        repository=repository,
        broker=broker
        or PaperBroker(
            config=PaperFillConfig(slippage=SlippageConfig(mode="points", market_order_points=0.5))
        ),
        runtime_id="intraday_options",
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        session_id=session.id,
        quotes=quotes,
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
    # No quote book is injected here, so the lifecycle synthesises a quote from
    # the signal's reference price alone and the fill model has no book to price
    # against. Each leg therefore pays the 0.5 configured slippage **and** the
    # conservative one-tick extra that Phase 4 Part 5 attaches to the LTP fallback
    # (spec 5.1) — buy at 100.55, sell at 109.45. Phase 1 charged only the 0.5,
    # which is precisely the understatement limitation 5 described.
    assert result.position.realised_pnl == pytest.approx(50 * (109.45 - 100.55))


# ---------------------------------------- accumulation across several fills
def _order_row(repository: ExecutionRepository):
    return (
        repository.database.connect()
        .execute("SELECT filled_quantity, average_fill_price, status FROM orders")
        .fetchone()
    )


def test_two_fills_on_one_order_accumulate_rather_than_overwrite(repository, session):
    """The defect Phase 4 Part 5 found and fixed.

    ``apply_fill`` used to write ``fill.quantity`` and ``fill.price`` straight onto
    the ``orders`` row, so an order with two fills reported the **last** fill's
    quantity and price as though they were the order's own. It was invisible for
    three phases only because nothing produced two fills; the partial-fill model
    does, so it would have shipped a row saying 25 of 75 filled at the second
    price.
    """
    broker = PaperBroker(
        config=PaperFillConfig(slippage=SlippageConfig(market_order_ticks=0)),
        fill_quantity_policy=lambda intent, quote: (30, 20),
    )
    result = _lifecycle(repository, session, broker=broker).handle_signal(
        _signal(), trading_date=TRADING_DATE
    )

    assert result.order is not None and len(result.order.fills) == 2
    row = _order_row(repository)
    assert row["filled_quantity"] == 50, "the running total, not the last fill's 20"
    assert row["status"] == OrderStatus.FILLED.value
    assert result.position is not None and result.position.quantity == 50


def test_the_persisted_average_is_quantity_weighted(repository, session):
    """Two fills at different prices, and the row must not report either one."""
    result = _lifecycle(repository, session).handle_signal(_signal(), trading_date=TRADING_DATE)
    assert result.correlation_id is not None
    order_id = int(repository.database.connect().execute("SELECT id FROM orders").fetchone()["id"])

    repository.apply_fill(
        order_id=order_id,
        runtime_id="intraday_options",
        fill=Fill(
            correlation_id=result.correlation_id,
            broker_fill_id="manual-second-fill",
            strategy_id="st01",
            execution_mode=ExecutionMode.PAPER,
            quantity=50,
            price=200.55,
            filled_at=datetime.now(UTC),
        ),
        order_status=OrderStatus.FILLED,
        instrument="NIFTY",
        security_id="99926000",
        side=Side.BUY,
        trading_date=TRADING_DATE,
    )

    first_price = result.order.fills[0].price if result.order else 0.0
    row = _order_row(repository)
    assert row["filled_quantity"] == 100
    # The mean of the two, not the second alone and not the first alone.
    assert row["average_fill_price"] == pytest.approx((first_price + 200.55) / 2)
    assert row["average_fill_price"] != pytest.approx(200.55)


def test_a_partially_filled_order_is_persisted_as_partially_filled(repository, session):
    broker = PaperBroker(
        config=PaperFillConfig(slippage=SlippageConfig(market_order_ticks=0)),
        fill_quantity_policy=lambda intent, quote: (20,),
    )
    _lifecycle(repository, session, broker=broker).handle_signal(
        _signal(), trading_date=TRADING_DATE
    )

    row = _order_row(repository)
    assert row["status"] == OrderStatus.PARTIALLY_FILLED.value
    assert row["filled_quantity"] == 20


# --------------------------------------------- the quote behind the fill
def test_the_lifecycle_prices_against_the_live_book_when_one_is_available(repository, session):
    """The four-place gap Part 5 closed, seen from the far end: before it, the
    lifecycle built its quote from ``signal.reference_price`` alone and every fill
    was an LTP fallback no matter what the feed carried."""
    quotes = QuoteBook()
    quotes.record(
        Tick(
            security_id="99926000",
            instrument="NIFTY",
            last_price=100.5,
            exchange_time=datetime(2026, 7, 30, 9, 16, tzinfo=UTC),
            received_at=datetime(2026, 7, 30, 9, 16, tzinfo=UTC),
            bid_price=100.40,
            ask_price=100.60,
        )
    )
    broker = PaperBroker(config=PaperFillConfig(slippage=SlippageConfig(market_order_ticks=0)))
    result = _lifecycle(repository, session, broker=broker, quotes=quotes).handle_signal(
        _signal(), trading_date=TRADING_DATE
    )

    assert result.order is not None
    fill = result.order.fills[0]
    assert fill.fill_method == "bid_ask"
    assert result.order.average_fill_price == 100.60, "the ask, not the reference price"


def test_the_submission_time_quote_is_persisted_beside_the_fill(repository, session):
    """Spec section 6's record list. ``fills`` could not be widened — the runner
    needs replay-safe statements — so it lands in ``paper_fill_quotes``."""
    quotes = QuoteBook()
    quotes.record(
        Tick(
            security_id="99926000",
            instrument="NIFTY",
            last_price=100.5,
            exchange_time=datetime(2026, 7, 30, 9, 16, tzinfo=UTC),
            received_at=datetime(2026, 7, 30, 9, 16, tzinfo=UTC),
            bid_price=100.40,
            ask_price=100.60,
        )
    )
    _lifecycle(repository, session, quotes=quotes).handle_signal(
        _signal(), trading_date=TRADING_DATE
    )

    row = (
        repository.database.connect()
        .execute("SELECT quote_bid, quote_ask, latency_applied, fill_method FROM paper_fill_quotes")
        .fetchone()
    )
    assert (row["quote_bid"], row["quote_ask"]) == (100.40, 100.60)
    assert row["fill_method"] == "bid_ask"
    assert row["latency_applied"] == 0, "no post-deadline quote existed — deviation D48"


def test_a_live_style_fill_with_no_quote_detail_leaves_no_misleading_row(repository, session):
    """A future live adapter that cannot report the book must not write a row of
    NULLs that reads like "we looked and there was nothing there"."""
    result = _lifecycle(repository, session).handle_signal(_signal(), trading_date=TRADING_DATE)
    order_id = int(repository.database.connect().execute("SELECT id FROM orders").fetchone()["id"])
    repository.apply_fill(
        order_id=order_id,
        runtime_id="intraday_options",
        fill=Fill(
            correlation_id=result.correlation_id or "",
            broker_fill_id="live-style-fill",
            strategy_id="st01",
            execution_mode=ExecutionMode.PAPER,
            quantity=50,
            price=100.5,
            filled_at=datetime.now(UTC),
        ),
        order_status=OrderStatus.FILLED,
        instrument="NIFTY",
        security_id="99926000",
        side=Side.BUY,
        trading_date=TRADING_DATE,
    )

    ids = [
        row["broker_fill_id"]
        for row in repository.database.connect().execute(
            "SELECT broker_fill_id FROM paper_fill_quotes"
        )
    ]
    assert "live-style-fill" not in ids


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
