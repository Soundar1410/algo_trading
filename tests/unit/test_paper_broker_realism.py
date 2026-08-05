"""The Phase 4 Part 5 fill model: depth, latency, limits, partials, rejections.

Closes runbook limitation 5 and deviation D11. The properties Phase 1 already had
— adverse slippage, idempotency, auditable provenance — stay in
``test_paper_broker.py``; everything here is behaviour that did not exist before.

Three of these tests exist because of something the pre-work audit found rather
than because the plan asked for them:

* :func:`test_a_fill_never_lands_between_ticks` and its siblings, because Dhan
  publishes ``SEM_TICK_SIZE`` in **paise** — ``5.0000`` for a ₹0.05 index-option
  tick — so a model that trusted the column would price every fill on a ₹5 grid.
  The enforced tick is configuration and the rule fails open when none is known.
* :func:`test_the_latency_that_could_not_be_applied_is_counted`, because on a live
  feed the post-latency quote does not exist yet at submission time (D48). The
  model records that per fill instead of implying it applied.
* :func:`test_a_partially_filled_order_reports_what_actually_filled`, because
  ``FillOutcome`` carries no quantity and ``LifecycleGateway`` used to accept a
  ``PARTIALLY_FILLED`` order as though it were complete.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from common.broker import (
    InstrumentRules,
    PaperBroker,
    PaperFillConfig,
    PaperRejection,
    PaperRejectionCode,
    Quote,
    QuoteBook,
    SlippageConfig,
)
from common.config.models import ExecutionMode
from common.models import (
    OrderIntent,
    OrderStatus,
    OrderType,
    RiskDecision,
    Side,
    Tick,
)

SECURITY_ID = "49081"
T0 = datetime(2026, 8, 5, 4, 30, 0, tzinfo=UTC)  # 10:00 IST, mid-session


def _intent(
    *,
    side: Side = Side.BUY,
    correlation_id: str = "p_io_st01_20260805_0001",
    order_type: OrderType = OrderType.MARKET,
    quantity: int = 75,
    limit_price: float | None = None,
    security_id: str = SECURITY_ID,
    risk_decision: RiskDecision = RiskDecision.ALLOWED,
    risk_reason: str | None = None,
) -> OrderIntent:
    return OrderIntent(
        correlation_id=correlation_id,
        strategy_id="st01",
        runtime_id="intraday_options",
        execution_mode=ExecutionMode.PAPER,
        trading_date="2026-08-05",
        sequence_number=1,
        instrument="NIFTY 07 AUG 24000 CALL",
        security_id=security_id,
        side=side,
        quantity=quantity,
        order_type=order_type,
        product_type="INTRADAY",
        created_at=T0,
        limit_price=limit_price,
        risk_decision=risk_decision,
        risk_reason=risk_reason,
    )


def _quote(
    last: float = 187.45,
    *,
    bid: float | None = 187.40,
    ask: float | None = 187.50,
    at: datetime = T0,
    security_id: str = SECURITY_ID,
) -> Quote:
    return Quote(security_id=security_id, last_price=last, quoted_at=at, bid=bid, ask=ask)


def _tick(last: float, *, bid: float | None, ask: float | None, at: datetime) -> Tick:
    return Tick(
        security_id=SECURITY_ID,
        instrument="NIFTY 07 AUG 24000 CALL",
        last_price=last,
        exchange_time=at,
        received_at=at,
        bid_price=bid,
        ask_price=ask,
    )


def _broker(**kwargs: object) -> PaperBroker:
    """A broker whose freshness rule is off, so fixed timestamps stay usable."""
    config = kwargs.pop("config", None) or PaperFillConfig()
    return PaperBroker(config=config, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------- depth
def test_a_buy_takes_the_ask_and_a_sell_takes_the_bid():
    """The property the whole of Part 5 exists for. Before it, both sides priced
    off the same last price, so a round trip paid no spread at all — which is why
    limitation 5 said paper P&L was not yet a credible estimate of live P&L."""
    broker = _broker(config=PaperFillConfig(slippage=SlippageConfig(market_order_ticks=0)))

    buy = broker.submit(_intent(side=Side.BUY), _quote())
    sell = broker.submit(
        _intent(side=Side.SELL, correlation_id="p_io_st01_20260805_0002"), _quote()
    )

    assert buy.average_fill_price == 187.50
    assert sell.average_fill_price == 187.40
    assert buy.fills[0].fill_method == "bid_ask"


def test_a_round_trip_now_pays_the_spread():
    """Buy then sell at an unchanged book, and the simulated P&L is negative by
    the spread. Phase 1's model returned exactly zero here."""
    broker = _broker(config=PaperFillConfig(slippage=SlippageConfig(market_order_ticks=0)))
    entry = broker.submit(_intent(side=Side.BUY), _quote())
    exit_ = broker.submit(
        _intent(side=Side.SELL, correlation_id="p_io_st01_20260805_0002"), _quote()
    )

    assert entry.average_fill_price is not None and exit_.average_fill_price is not None
    assert exit_.average_fill_price - entry.average_fill_price == pytest.approx(-0.10)


def test_a_one_sided_book_falls_back_and_says_so():
    broker = _broker()
    order = broker.submit(_intent(side=Side.BUY), _quote(bid=187.40, ask=None))
    assert order.fills[0].fill_method == "ltp_fallback"
    assert order.fills[0].quote_ask is None


# ------------------------------------------------------------- tick rounding
def test_a_fill_never_lands_between_ticks():
    """An index option trades in ₹0.05 steps, so a price like 187.4712 is not a
    price anyone could have got."""
    broker = _broker(
        config=PaperFillConfig(
            slippage=SlippageConfig(mode="basis_points", market_order_bps=17.0),
            tick_size=0.05,
        )
    )
    order = broker.submit(_intent(side=Side.BUY), _quote())

    assert order.average_fill_price is not None
    steps = order.average_fill_price / 0.05
    assert steps == pytest.approx(round(steps)), order.average_fill_price


@pytest.mark.parametrize(
    ("side", "expected"),
    [(Side.BUY, 187.85), (Side.SELL, 187.05)],
)
def test_tick_rounding_is_adverse_in_both_directions(side: Side, expected: float):
    """Rounding must never improve a fill, for the same reason slippage must not:
    a simulator that can round in your favour flatters a losing strategy."""
    broker = _broker(
        config=PaperFillConfig(
            slippage=SlippageConfig(mode="points", market_order_points=0.33),
            tick_size=0.05,
        )
    )
    order = broker.submit(_intent(side=side), _quote())
    assert order.average_fill_price == expected


def test_a_price_already_on_a_tick_is_left_alone():
    """``187.50 / 0.05`` is ``3749.999999999999`` in binary floating point, so a
    naive ceiling would push an exact price a whole tick further away."""
    broker = _broker(config=PaperFillConfig(slippage=SlippageConfig(market_order_ticks=0)))
    order = broker.submit(_intent(side=Side.BUY), _quote())
    assert order.average_fill_price == 187.50


def test_with_no_tick_known_the_price_is_left_ungridded_rather_than_guessed():
    """Fails open. Dhan's ``SEM_TICK_SIZE`` is published in paise, so a model that
    guessed at the unit would put NIFTY options on a ₹5 grid and reject or
    misprice every order on the instrument."""
    broker = _broker(
        config=PaperFillConfig(
            slippage=SlippageConfig(mode="points", market_order_points=0.33),
            tick_size=None,
        )
    )
    order = broker.submit(_intent(side=Side.BUY), _quote())
    assert order.average_fill_price == pytest.approx(187.83)


def test_the_instruments_own_tick_beats_the_configured_default():
    broker = _broker(
        config=PaperFillConfig(
            slippage=SlippageConfig(mode="points", market_order_points=0.33),
            tick_size=0.05,
        ),
        instrument_rules=lambda _sid: InstrumentRules(tick_size=0.10),
    )
    order = broker.submit(_intent(side=Side.BUY), _quote())
    assert order.average_fill_price == 187.90  # on the 0.10 grid, not the 0.05 one


# ------------------------------------------------------------------ latency
def test_a_market_order_prices_against_the_post_latency_quote_when_one_exists():
    """Spec 5.2. The book has moved against us in the 250 ms since submission, and
    the fill reflects the price we would actually have got, not the one we saw."""
    quotes = QuoteBook()
    quotes.record(_tick(187.45, bid=187.40, ask=187.50, at=T0))
    quotes.record(_tick(187.95, bid=187.90, ask=188.00, at=T0 + timedelta(milliseconds=300)))

    broker = _broker(
        config=PaperFillConfig(
            slippage=SlippageConfig(market_order_ticks=0), submission_latency_ms=250
        ),
        quotes=quotes,
    )
    order = broker.submit(_intent(side=Side.BUY), _quote())

    assert order.average_fill_price == 188.00, "should have used the later ask"
    assert order.fills[0].latency_applied is True
    assert broker.latency_not_applied == 0


def test_the_first_quote_past_the_deadline_wins_not_the_best_one():
    """Oldest at or after, not *newest*: the order became live at the deadline
    and would have executed against the price available then. Taking the newest
    would be lookahead wearing a latency model's clothes."""
    quotes = QuoteBook()
    quotes.record(_tick(187.45, bid=187.40, ask=187.50, at=T0))
    quotes.record(_tick(188.45, bid=188.40, ask=188.50, at=T0 + timedelta(milliseconds=300)))
    quotes.record(_tick(180.05, bid=180.00, ask=180.10, at=T0 + timedelta(milliseconds=900)))

    broker = _broker(
        config=PaperFillConfig(
            slippage=SlippageConfig(market_order_ticks=0), submission_latency_ms=250
        ),
        quotes=quotes,
    )
    order = broker.submit(_intent(side=Side.BUY), _quote())
    assert order.average_fill_price == 188.50, "the cheaper later ask must not be used"


def test_the_latency_that_could_not_be_applied_is_counted():
    """Deviation D48, made observable. On a live feed the post-deadline quote does
    not exist when ``submit`` is called, so the fill uses the submission quote —
    and says so, per fill and in a counter, rather than the configuration implying
    a latency that was never applied."""
    quotes = QuoteBook()
    quotes.record(_tick(187.45, bid=187.40, ask=187.50, at=T0))

    broker = _broker(
        config=PaperFillConfig(
            slippage=SlippageConfig(market_order_ticks=0), submission_latency_ms=250
        ),
        quotes=quotes,
    )
    order = broker.submit(_intent(side=Side.BUY), _quote())

    assert order.average_fill_price == 187.50
    assert order.fills[0].latency_applied is False
    assert broker.latency_not_applied == 1


def test_with_no_quote_book_at_all_the_submission_quote_is_used():
    broker = _broker(config=PaperFillConfig(slippage=SlippageConfig(market_order_ticks=0)))
    order = broker.submit(_intent(side=Side.BUY), _quote())
    assert order.fills[0].latency_applied is False


# ------------------------------------------------------------- limit orders
def test_a_limit_order_rests_rather_than_filling_off_the_submission_quote():
    """Spec 5.3: an eligible price must arrive *after* submission. A limit buy at
    187.50 against an ask of 187.50 already on screen must not fill here — that
    would be a market order wearing a limit order's name."""
    broker = _broker()
    order = broker.submit(
        _intent(side=Side.BUY, order_type=OrderType.LIMIT, limit_price=187.50), _quote()
    )

    assert order.status is OrderStatus.SUBMITTED
    assert order.fills == ()


def test_a_limit_buy_fills_when_a_later_ask_reaches_it():
    broker = _broker()
    intent = _intent(side=Side.BUY, order_type=OrderType.LIMIT, limit_price=187.00)
    broker.submit(intent, _quote())

    too_high = broker.on_quote(_quote(187.45, bid=187.40, ask=187.50, at=T0 + timedelta(seconds=1)))
    assert too_high == (), "an ask above the limit must not fill it"

    filled = broker.on_quote(_quote(186.95, bid=186.90, ask=187.00, at=T0 + timedelta(seconds=2)))
    assert len(filled) == 1
    assert filled[0].status is OrderStatus.FILLED
    assert filled[0].average_fill_price == 187.00
    assert filled[0].fills[0].fill_method == "limit"
    assert broker.working_orders == ()


def test_a_limit_sell_fills_when_a_later_bid_reaches_it():
    broker = _broker()
    broker.submit(_intent(side=Side.SELL, order_type=OrderType.LIMIT, limit_price=188.00), _quote())

    assert broker.on_quote(_quote(at=T0 + timedelta(seconds=1))) == ()
    filled = broker.on_quote(_quote(188.05, bid=188.00, ask=188.10, at=T0 + timedelta(seconds=2)))
    assert len(filled) == 1 and filled[0].average_fill_price == 188.00


def test_a_limit_fill_takes_no_price_improvement():
    """A real limit buy against a cheaper ask does get the improvement — but only
    if it reaches the front of the queue, and queue position is exactly what a
    simulator with top-of-book data cannot know. Assuming it would hand every
    resting order a free edge."""
    broker = _broker()
    broker.submit(_intent(side=Side.BUY, order_type=OrderType.LIMIT, limit_price=187.00), _quote())
    filled = broker.on_quote(_quote(185.05, bid=185.00, ask=185.10, at=T0 + timedelta(seconds=2)))
    assert filled[0].average_fill_price == 187.00, "the limit, not the better ask"


def test_a_limit_order_does_not_fill_before_its_latency_deadline():
    """The same mechanism as market-order latency, not a second one."""
    broker = _broker(config=PaperFillConfig(submission_latency_ms=250))
    broker.submit(_intent(side=Side.BUY, order_type=OrderType.LIMIT, limit_price=187.50), _quote())

    early = broker.on_quote(_quote(at=T0 + timedelta(milliseconds=100)))
    assert early == (), "eligible on price, but the order is not live yet"

    late = broker.on_quote(_quote(at=T0 + timedelta(milliseconds=300)))
    assert len(late) == 1


def test_a_quote_for_another_instrument_leaves_the_order_resting():
    broker = _broker()
    broker.submit(_intent(side=Side.BUY, order_type=OrderType.LIMIT, limit_price=187.50), _quote())
    other = Quote(security_id="OTHER", last_price=1.0, quoted_at=T0 + timedelta(seconds=5), ask=0.5)
    assert broker.on_quote(other) == ()
    assert len(broker.working_orders) == 1


# ------------------------------------------------------------ partial fills
def _split(*sizes: int):
    return lambda _intent, _quote: sizes


def test_a_partially_filled_order_reports_what_actually_filled():
    broker = _broker(
        config=PaperFillConfig(slippage=SlippageConfig(market_order_ticks=0)),
        fill_quantity_policy=_split(25),
    )
    order = broker.submit(_intent(quantity=75), _quote())

    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == 25
    assert not order.status.is_terminal


def test_several_fills_accumulate_into_one_order():
    broker = _broker(
        config=PaperFillConfig(slippage=SlippageConfig(market_order_ticks=0)),
        fill_quantity_policy=_split(25, 25, 25),
    )
    order = broker.submit(_intent(quantity=75), _quote())

    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == 75
    assert len(order.fills) == 3
    assert len({fill.broker_fill_id for fill in order.fills}) == 3, "ids must be distinct"


def test_the_average_fill_price_is_quantity_weighted():
    broker = _broker(
        config=PaperFillConfig(slippage=SlippageConfig(market_order_ticks=0)),
        fill_quantity_policy=_split(50, 25),
    )
    order = broker.submit(_intent(quantity=75), _quote())
    assert order.average_fill_price == 187.50


def test_the_default_policy_still_fills_the_whole_order_in_one():
    """Spec 5.4 asks for model support and a test hook, not a default that
    produces partials. Every existing run must be unchanged."""
    broker = _broker()
    order = broker.submit(_intent(quantity=75), _quote())
    assert len(order.fills) == 1 and order.filled_quantity == 75


def test_a_policy_that_would_over_fill_is_a_programming_error_not_a_rejection():
    """No broker sent it and no operator configured it, so it is not something the
    simulator can meaningfully refuse on the exchange's behalf."""
    broker = _broker(fill_quantity_policy=_split(50, 50))
    with pytest.raises(ValueError, match="must not over-fill"):
        broker.submit(_intent(quantity=75), _quote())


# --------------------------------------------------------- rejection rules
def test_an_unknown_instrument_is_refused():
    broker = _broker(instrument_rules=lambda _sid: None)
    with pytest.raises(PaperRejection) as raised:
        broker.submit(_intent(), _quote())
    assert raised.value.code is PaperRejectionCode.INVALID_INSTRUMENT


def test_a_quantity_that_is_not_a_whole_number_of_lots_is_refused():
    broker = _broker(instrument_rules=lambda _sid: InstrumentRules(lot_size=75))
    with pytest.raises(PaperRejection, match="not a multiple") as raised:
        broker.submit(_intent(quantity=80), _quote())
    assert raised.value.code is PaperRejectionCode.INVALID_QUANTITY


def test_a_quantity_above_the_ceiling_is_refused():
    broker = _broker(instrument_rules=lambda _sid: InstrumentRules(lot_size=75, max_quantity=750))
    with pytest.raises(PaperRejection, match="exceeds") as raised:
        broker.submit(_intent(quantity=1500), _quote())
    assert raised.value.code is PaperRejectionCode.INVALID_QUANTITY


def test_a_limit_price_off_the_tick_grid_is_refused():
    broker = _broker(config=PaperFillConfig(tick_size=0.05))
    with pytest.raises(PaperRejection, match=r"not a multiple of the 0\.05 tick") as raised:
        broker.submit(_intent(order_type=OrderType.LIMIT, limit_price=187.43), _quote())
    assert raised.value.code is PaperRejectionCode.INVALID_TICK_PRICE


def test_a_limit_price_on_the_tick_grid_is_accepted():
    broker = _broker(config=PaperFillConfig(tick_size=0.05))
    order = broker.submit(_intent(order_type=OrderType.LIMIT, limit_price=187.45), _quote())
    assert order.status is OrderStatus.SUBMITTED


def test_a_market_order_is_not_tick_validated():
    """It submits no price. The price it fills at is the simulator's own output
    and is put on the grid rather than validated after the fact."""
    broker = _broker(config=PaperFillConfig(tick_size=0.05))
    broker.submit(_intent(), _quote(187.4712, bid=187.4712, ask=187.4713))  # must not raise


def test_a_stale_quote_is_refused():
    """Spec section 10's rule that a stale price is never treated as a fresh
    unchanged one, enforced at the point it would do damage."""
    broker = PaperBroker(config=PaperFillConfig(max_quote_age_ms=2000))
    old = _quote(at=datetime.now(UTC) - timedelta(seconds=30))
    with pytest.raises(PaperRejection, match="ms old") as raised:
        broker.submit(_intent(), old)
    assert raised.value.code is PaperRejectionCode.STALE_QUOTE


def test_a_fresh_quote_passes_the_freshness_rule():
    broker = PaperBroker(config=PaperFillConfig(max_quote_age_ms=2000))
    order = broker.submit(_intent(), _quote(at=datetime.now(UTC)))
    assert order.status is OrderStatus.FILLED


def test_a_replayed_tape_is_not_refused_as_stale_when_the_rule_is_off():
    """``max_quote_age_ms: null`` is what every recorded run needs: its timestamps
    are historic, so every quote would otherwise look ancient."""
    broker = PaperBroker(config=PaperFillConfig(max_quote_age_ms=None))
    ancient = _quote(at=datetime(2020, 1, 1, tzinfo=UTC))
    assert broker.submit(_intent(), ancient).status is OrderStatus.FILLED


def test_a_missing_book_with_the_fallback_disabled_is_refused():
    broker = _broker(config=PaperFillConfig(allow_ltp_fallback=False))
    with pytest.raises(PaperRejection) as raised:
        broker.submit(_intent(), _quote(bid=None, ask=None))
    assert raised.value.code is PaperRejectionCode.NO_DEPTH


def test_the_market_closed_rule_fires_only_when_switched_on():
    closed = _broker(
        config=PaperFillConfig(reject_when_market_closed=True),
        is_market_open=lambda _at: False,
    )
    with pytest.raises(PaperRejection, match="market is closed") as raised:
        closed.submit(_intent(), _quote())
    assert raised.value.code is PaperRejectionCode.MARKET_CLOSED


def test_the_market_closed_rule_is_off_by_default():
    """Deliberate, and the reason is a hazard rather than an omission: the engine
    already gates *entries* on the session, while an exit or a square-off fires at
    or after the square-off time — so a broker enforcing this by default would
    refuse exactly the orders that must never be refused."""
    assert PaperFillConfig().reject_when_market_closed is False
    broker = _broker(is_market_open=lambda _at: False)
    assert broker.submit(_intent(), _quote()).status is OrderStatus.FILLED


def test_an_intent_the_risk_gate_blocked_is_refused():
    broker = _broker()
    blocked = _intent(risk_decision=RiskDecision.BLOCKED, risk_reason="daily loss cap")
    with pytest.raises(PaperRejection, match="daily loss cap") as raised:
        broker.submit(blocked, _quote())
    assert raised.value.code is PaperRejectionCode.RISK_BLOCKED


def test_a_configured_failure_injection_is_deterministic():
    """Deterministic rather than probabilistic, so a test asserts an outcome
    instead of a distribution."""
    broker = _broker(config=PaperFillConfig(reject_correlation_ids=("p_io_st01_20260805_0002",)))
    assert broker.submit(_intent(), _quote()).status is OrderStatus.FILLED
    with pytest.raises(PaperRejection) as raised:
        broker.submit(_intent(correlation_id="p_io_st01_20260805_0002"), _quote())
    assert raised.value.code is PaperRejectionCode.INJECTED_FAILURE


def test_every_rejection_rule_the_spec_lists_has_a_code():
    """Spec 5.5 names nine. The enum is what stops one being quietly dropped."""
    assert len(PaperRejectionCode) == 9


def test_rejections_are_counted_by_code():
    broker = _broker()
    broker.submit(_intent(), _quote())
    for _ in range(2):
        with pytest.raises(PaperRejection):
            broker.submit(_intent(), _quote())
    assert broker.rejections == {PaperRejectionCode.DUPLICATE_CORRELATION_ID: 2}


def test_the_code_travels_in_the_message_so_the_persisted_reason_carries_it():
    """``orders.rejection_reason`` is free text and gains no column here, so the
    code is prefixed onto the message rather than stored separately."""
    broker = _broker(instrument_rules=lambda _sid: None)
    with pytest.raises(PaperRejection) as raised:
        broker.submit(_intent(), _quote())
    assert str(raised.value).startswith("INVALID_INSTRUMENT: ")


# ------------------------------------------------------------- the defaults
def test_rules_that_need_an_injected_dependency_are_inactive_without_one():
    """The only safe default. A broker that refused everything it could not verify
    would refuse every order in a runtime with no scrip master — which is every
    offline test and every simulated-contract run."""
    broker = _broker()
    assert broker.submit(_intent(quantity=7), _quote()).status is OrderStatus.FILLED
    assert broker.rejections == {}
