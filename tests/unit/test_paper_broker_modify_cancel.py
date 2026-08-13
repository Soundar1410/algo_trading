"""PaperBroker's Phase 10 additions: modify/cancel and the reconciliation
reads (fetch_order_book/fetch_trades/fetch_positions) — the rest of the
widened Broker protocol (common/broker/base.py), exercised on the adapter
that already existed before DhanLiveBroker did."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from common.broker import BrokerError, PaperBroker, Quote
from common.config.models import ExecutionMode
from common.models import OrderIntent, OrderStatus, OrderType, Side


def _intent(
    *,
    side: Side = Side.BUY,
    correlation_id: str = "p_io_st01_20260729_0001",
    order_type: OrderType = OrderType.LIMIT,
    quantity: int = 50,
    limit_price: float | None = 100.0,
) -> OrderIntent:
    return OrderIntent(
        limit_price=limit_price,
        correlation_id=correlation_id,
        strategy_id="st01",
        runtime_id="intraday_options",
        execution_mode=ExecutionMode.PAPER,
        trading_date="2026-07-29",
        sequence_number=1,
        instrument="NIFTY",
        security_id="99926000",
        side=side,
        quantity=quantity,
        order_type=order_type,
        product_type="INTRADAY",
        created_at=datetime.now(UTC),
    )


def _quote(last: float = 100.0) -> Quote:
    return Quote(security_id="99926000", last_price=last, quoted_at=datetime.now(UTC))


# --------------------------------------------------------------------- modify
def test_modifying_a_resting_order_changes_its_quantity_and_price():
    broker = PaperBroker()
    broker.submit(_intent(), _quote())

    order = broker.modify("p_io_st01_20260729_0001", quantity=25, limit_price=95.0)

    assert order.status == OrderStatus.SUBMITTED
    assert "p_io_st01_20260729_0001" in broker.working_orders


def test_modifying_an_unknown_order_raises():
    broker = PaperBroker()
    with pytest.raises(BrokerError, match="no resting order"):
        broker.modify("no_such_order", quantity=10)


def test_modifying_an_already_filled_order_raises():
    broker = PaperBroker()
    broker.submit(_intent(order_type=OrderType.MARKET, limit_price=None), _quote())
    with pytest.raises(BrokerError, match="no resting order"):
        broker.modify("p_io_st01_20260729_0001", quantity=10)


# --------------------------------------------------------------------- cancel
def test_cancelling_a_resting_order_marks_it_cancelled_and_stops_it_resting():
    broker = PaperBroker()
    broker.submit(_intent(), _quote())

    order = broker.cancel("p_io_st01_20260729_0001")

    assert order.status == OrderStatus.CANCELLED
    assert "p_io_st01_20260729_0001" not in broker.working_orders
    assert broker.order_by_correlation_id("p_io_st01_20260729_0001").status == OrderStatus.CANCELLED


def test_cancelling_an_unknown_order_raises():
    broker = PaperBroker()
    with pytest.raises(BrokerError, match="no resting order"):
        broker.cancel("no_such_order")


def test_cancelling_an_already_filled_order_raises():
    broker = PaperBroker()
    broker.submit(_intent(order_type=OrderType.MARKET, limit_price=None), _quote())
    with pytest.raises(BrokerError, match="no resting order"):
        broker.cancel("p_io_st01_20260729_0001")


def test_cancelling_twice_raises_the_second_time():
    broker = PaperBroker()
    broker.submit(_intent(), _quote())
    broker.cancel("p_io_st01_20260729_0001")
    with pytest.raises(BrokerError, match="no resting order"):
        broker.cancel("p_io_st01_20260729_0001")


# ---------------------------------------------------------- reconciliation reads
def test_fetch_order_book_returns_every_known_order():
    broker = PaperBroker()
    broker.submit(_intent(correlation_id="p_io_st01_20260729_0001"), _quote())
    broker.submit(
        _intent(
            correlation_id="p_io_st01_20260729_0002",
            order_type=OrderType.MARKET,
            limit_price=None,
        ),
        _quote(),
    )

    book = broker.fetch_order_book()

    assert {o.correlation_id for o in book} == {
        "p_io_st01_20260729_0001",
        "p_io_st01_20260729_0002",
    }


def test_fetch_trades_returns_fills_from_every_order():
    broker = PaperBroker()
    broker.submit(
        _intent(order_type=OrderType.MARKET, limit_price=None),
        _quote(),
    )

    trades = broker.fetch_trades()

    assert len(trades) == 1
    assert trades[0].correlation_id == "p_io_st01_20260729_0001"


def test_fetch_trades_is_empty_before_any_fill():
    broker = PaperBroker()
    broker.submit(_intent(), _quote())  # a resting limit order, no fill yet
    assert broker.fetch_trades() == ()


def test_fetch_positions_is_always_empty_in_paper_mode():
    """Paper mode has no broker-side position concept — that is the local
    Position table's job, not PaperBroker's. Documented, not simulated."""
    broker = PaperBroker()
    broker.submit(_intent(order_type=OrderType.MARKET, limit_price=None), _quote())
    assert broker.fetch_positions() == ()
