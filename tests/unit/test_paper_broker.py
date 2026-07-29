"""PaperBroker: adverse slippage, idempotency and honest fill provenance."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from common.broker import ChargesCalculator, PaperBroker, PaperFillConfig, PaperRejection, Quote
from common.broker.costs import CostRates
from common.config.models import ExecutionMode
from common.models import OrderIntent, OrderStatus, OrderType, Side


def _intent(
    *,
    side: Side = Side.BUY,
    correlation_id: str = "p_io_st01_20260729_0001",
    order_type: OrderType = OrderType.MARKET,
    quantity: int = 50,
) -> OrderIntent:
    return OrderIntent(
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


def _quote(last: float = 100.0, bid: float | None = None, ask: float | None = None) -> Quote:
    return Quote(
        security_id="99926000",
        last_price=last,
        quoted_at=datetime.now(UTC),
        bid=bid,
        ask=ask,
    )


# ------------------------------------------------------------- slippage
def test_a_buy_fills_above_the_reference_price():
    broker = PaperBroker(config=PaperFillConfig(slippage_points=0.5))
    order = broker.submit(_intent(side=Side.BUY), _quote(100.0))
    assert order.average_fill_price == 100.5


def test_a_sell_fills_below_the_reference_price():
    broker = PaperBroker(config=PaperFillConfig(slippage_points=0.5))
    order = broker.submit(_intent(side=Side.SELL), _quote(100.0))
    assert order.average_fill_price == 99.5


def test_slippage_is_never_favourable_in_either_direction():
    """A simulator that can fill you better than the quote flatters bad strategies."""
    broker = PaperBroker(config=PaperFillConfig(slippage_points=0.25))
    buy_intent = _intent(side=Side.BUY, correlation_id="p_io_st01_20260729_0001")
    buy = broker.submit(buy_intent, _quote(100.0))
    sell_intent = _intent(side=Side.SELL, correlation_id="p_io_st01_20260729_0002")
    sell = broker.submit(sell_intent, _quote(100.0))

    assert buy.average_fill_price is not None and buy.average_fill_price > 100.0
    assert sell.average_fill_price is not None and sell.average_fill_price < 100.0


def test_bid_ask_is_preferred_when_the_quote_has_depth():
    broker = PaperBroker(config=PaperFillConfig(slippage_points=0.1))
    order = broker.submit(_intent(side=Side.BUY), _quote(100.0, bid=99.5, ask=100.5))

    assert order.average_fill_price == 100.6  # ask + slippage
    assert order.fills[0].fill_method == "bid_ask"


def test_falling_back_to_last_price_is_recorded_not_hidden():
    broker = PaperBroker()
    order = broker.submit(_intent(), _quote(100.0))
    assert order.fills[0].fill_method == "ltp_fallback"


def test_fallback_can_be_disabled():
    broker = PaperBroker(config=PaperFillConfig(allow_ltp_fallback=False))
    with pytest.raises(PaperRejection, match="LTP fallback is disabled"):
        broker.submit(_intent(), _quote(100.0))


# ---------------------------------------------------------- idempotency
def test_a_repeated_correlation_id_is_rejected():
    """Idempotent submission: a retry must not become a second position."""
    broker = PaperBroker()
    broker.submit(_intent(), _quote(100.0))
    with pytest.raises(PaperRejection, match="Duplicate correlation ID"):
        broker.submit(_intent(), _quote(100.0))


def test_an_order_can_be_recovered_by_correlation_id():
    broker = PaperBroker()
    submitted = broker.submit(_intent(), _quote(100.0))
    assert broker.order_by_correlation_id(submitted.correlation_id) == submitted


def test_an_unknown_correlation_id_returns_none():
    assert PaperBroker().order_by_correlation_id("p_io_st01_20260729_9999") is None


# ------------------------------------------------------------ provenance
def test_the_fill_records_everything_needed_to_audit_it():
    broker = PaperBroker(config=PaperFillConfig(slippage_points=0.5, submission_latency_ms=250))
    order = broker.submit(_intent(), _quote(100.0))
    fill = order.fills[0]

    assert fill.reference_price == 100.0
    assert fill.slippage_amount == 0.5
    assert fill.latency_ms == 250
    assert fill.charges > 0
    assert fill.execution_mode is ExecutionMode.PAPER


def test_a_filled_order_reports_filled_status_and_quantity():
    broker = PaperBroker()
    order = broker.submit(_intent(quantity=75), _quote(100.0))
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == 75
    assert order.status.is_terminal


# ------------------------------------------------------------- rejections
def test_a_limit_order_is_refused_rather_than_silently_filled_at_market():
    """Phase 1 has no limit simulation; pretending otherwise would be a lie."""
    broker = PaperBroker()
    with pytest.raises(PaperRejection, match="not simulated in Phase 1"):
        broker.submit(_intent(order_type=OrderType.LIMIT), _quote(100.0))


def test_a_quote_for_the_wrong_instrument_is_refused():
    broker = PaperBroker()
    wrong = Quote(security_id="OTHER", last_price=100.0, quoted_at=datetime.now(UTC))
    with pytest.raises(PaperRejection, match="Quote is for"):
        broker.submit(_intent(), wrong)


def test_a_fill_price_driven_negative_is_refused():
    broker = PaperBroker(config=PaperFillConfig(slippage_points=50.0))
    with pytest.raises(PaperRejection, match="not positive"):
        broker.submit(_intent(side=Side.SELL), _quote(1.0))


# ------------------------------------------------------------- config
def test_unknown_paper_execution_keys_are_rejected_not_ignored():
    with pytest.raises(ValueError, match="Unknown paper execution keys"):
        PaperFillConfig.from_mapping({"slippage_points": 0.1, "slipage_points": 0.2})


def test_the_broker_reports_itself_as_paper():
    broker = PaperBroker()
    assert broker.name == "paper"
    assert broker.is_healthy()


# ---------------------------------------------------------------- costs
def test_sell_legs_attract_stt_and_buy_legs_do_not():
    calculator = ChargesCalculator()
    buy = calculator.for_leg(side=Side.BUY, price=100.0, quantity=50)
    sell = calculator.for_leg(side=Side.SELL, price=100.0, quantity=50)

    assert buy.stt == 0.0
    assert sell.stt > 0.0
    assert buy.stamp_duty > 0.0
    assert sell.stamp_duty == 0.0


def test_charges_are_positive_and_itemised():
    breakdown = ChargesCalculator().for_leg(side=Side.BUY, price=100.0, quantity=50)
    assert breakdown.total > 0
    assert breakdown.total == pytest.approx(
        breakdown.brokerage
        + breakdown.exchange_charges
        + breakdown.stt
        + breakdown.sebi_charges
        + breakdown.gst
        + breakdown.stamp_duty,
        abs=1e-4,
    )


def test_rates_are_configurable_not_hard_coded():
    cheap = ChargesCalculator(CostRates(brokerage_per_order=0.0, gst_rate=0.0))
    default = ChargesCalculator()
    assert cheap.total_for_leg(side=Side.BUY, price=100.0, quantity=50) < default.total_for_leg(
        side=Side.BUY, price=100.0, quantity=50
    )


def test_unknown_cost_keys_are_rejected():
    with pytest.raises(ValueError, match="Unknown cost rate keys"):
        CostRates.from_mapping({"brokerage_per_order": 10.0, "mystery_fee": 1.0})


def test_charges_require_a_positive_price_and_quantity():
    with pytest.raises(ValueError, match="positive"):
        ChargesCalculator().for_leg(side=Side.BUY, price=0.0, quantity=50)
