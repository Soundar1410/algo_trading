"""``rolling_strangle_otm1`` pure decision logic, driven directly against
:class:`RollingStrangleOtm1Strategy` with hand-built ``Basket``/
``BasketRollState`` fixtures — no engine, no persistence, no broker.

Mirrors the acceptance matrix in ``ROLLING_STRANGLE_OTM1_ALGO_TRADING_SPEC.md``
section 17.2-17.5 for everything decidable from ``on_candle``/``on_leg_tick``
alone. Rows that require real contract resolution, durable claim commit, or
restart reconciliation (lot-size mismatch fail-closed, fresh-tick fill
gating, the full restart matrix) belong to the engine-level integration
suite (``tests/integration/test_rolling_strangle_otm1_engine.py``) instead —
this file only exercises what the strategy itself decides.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from common.config.models import ExecutionMode
from common.engine.models import Moneyness
from common.engine.multi_leg_models import (
    AdjustmentLifecycle,
    Basket,
    BasketAction,
    BasketRollState,
    LegInstance,
    LegRole,
    LegState,
    RollClaim,
)
from common.indicators.base import OHLC
from common.models import ExitReason, OrderSide, Tick
from strategies.intraday_options.rolling_strangle_otm1.strategy import RollingStrangleOtm1Strategy

IST = ZoneInfo("Asia/Kolkata")
STRATEGY_ID = "rolling_strangle_otm1"
TRADING_DATE = "2026-08-17"
BASKET_ID = f"{STRATEGY_ID}:{TRADING_DATE}"


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 17, hour, minute, second, tzinfo=IST)  # a Monday


def _candle(close: float) -> OHLC:
    return OHLC(high=close, low=close, close=close, open=close)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id, instrument=security_id, last_price=price,
        exchange_time=ts, received_at=ts,
    )


def _leg(
    role: LegRole,
    *,
    sequence: int = 1,
    state: LegState = LegState.OPEN,
    leg_id: str | None = None,
    entry_price: float = 100.0,
    last_price: float | None = None,
    realized_gross_pnl: float | None = None,
    quantity: int = 750,
) -> LegInstance:
    return LegInstance(
        leg_id=leg_id or f"{BASKET_ID}:{role.value}:{sequence}",
        basket_id=BASKET_ID,
        role=role,
        sequence=sequence,
        is_replacement=sequence > 1,
        side=OrderSide.SELL,
        quantity=quantity,
        state=state,
        entry_price=entry_price,
        last_price=last_price if last_price is not None else entry_price,
        realized_gross_pnl=realized_gross_pnl,
    )


def _claim(
    role: LegRole,
    roll_sequence: int,
    lifecycle_state: str,
    target_leg_id: str,
    *,
    claim_group_id: str = "g1",
    reference_price_at_claim: float = 24000.0,
    ts: datetime | None = None,
) -> RollClaim:
    when = ts or _ts(9, 30)
    return RollClaim(
        claim_group_id=claim_group_id,
        leg_role=role,
        roll_sequence=roll_sequence,
        lifecycle_state=lifecycle_state,
        target_leg_id=target_leg_id,
        close_correlation_id=None,
        close_intent_id=None,
        replacement_leg_id=None,
        reference_price_at_claim=reference_price_at_claim,
        claim_candle_ts=when,
        claimed_at=when,
    )


def _basket(
    *,
    legs: tuple[LegInstance, ...] = (),
    entries_consumed: bool = False,
    day_blocked_reason: str | None = None,
    reference_price: float | None = None,
    anchor_candle_ts: datetime | None = None,
    claims: tuple[RollClaim, ...] = (),
) -> Basket:
    basket = Basket(
        basket_id=BASKET_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        entries_consumed=entries_consumed,
        day_blocked_reason=day_blocked_reason,
        roll_state=BasketRollState(
            reference_price=reference_price, anchor_candle_ts=anchor_candle_ts, claims=claims
        ),
    )
    for leg in legs:
        basket.legs[leg.leg_id] = leg
    return basket


def _strategy(**kwargs: object) -> RollingStrangleOtm1Strategy:
    return RollingStrangleOtm1Strategy(cfg=None, **kwargs)


# ======================================================================
# Primary entry (spec 17.2)
# ======================================================================
def test_no_entry_before_0945() -> None:
    strategy = _strategy()
    basket = _basket()
    signal = strategy.on_candle(_candle(24000.0), _ts(9, 44, 59), basket=basket, vix=None)
    assert signal is None


def test_exactly_0945_is_eligible() -> None:
    strategy = _strategy()
    basket = _basket()
    signal = strategy.on_candle(_candle(24000.0), _ts(9, 45, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.action is BasketAction.ENTER_BASKET


def test_a_genuinely_utc_tick_at_0945_ist_is_also_eligible() -> None:
    """Regression: a real incident (31 August 2026, straddle_920 — same
    on_candle pattern here) found on_candle comparing a bare
    ``timestamp.time()`` (UTC clock-time, since ``tick.exchange_time`` is
    UTC-aware in production) against thresholds meant as IST wall-clock
    time. Every other fixture in this file uses ``_ts``, which is
    IST-tzinfo'd — a bare ``.time()`` also happens to get that right, so
    none of the tests above could have caught this. This one uses a
    genuinely UTC-tzinfo'd timestamp for the same instant, the way
    production actually labels it."""
    strategy = _strategy()
    basket = _basket()
    utc_tick = _ts(9, 45, 0).astimezone(UTC)
    signal = strategy.on_candle(_candle(24000.0), utc_tick, basket=basket, vix=None)
    assert signal is not None
    assert signal.action is BasketAction.ENTER_BASKET


def test_first_eligible_candle_durably_consumes_the_attempt() -> None:
    strategy = _strategy()
    basket = _basket()
    signal = strategy.on_candle(_candle(24000.0), _ts(9, 45, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.state_commit is not None
    assert signal.state_commit.consume_entry_attempt is True


def test_blackout_date_consumes_attempt_and_places_no_order() -> None:
    strategy = _strategy(blackout_dates=(TRADING_DATE,))
    basket = _basket()
    signal = strategy.on_candle(_candle(24000.0), _ts(9, 45, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.action is BasketAction.NONE
    assert signal.legs == ()
    assert signal.state_commit is not None
    assert signal.state_commit.consume_entry_attempt is True
    assert signal.state_commit.block_day_reason is not None


def test_empty_blackout_list_does_not_block() -> None:
    strategy = _strategy(blackout_dates=())
    basket = _basket()
    signal = strategy.on_candle(_candle(24000.0), _ts(9, 45, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.action is BasketAction.ENTER_BASKET


def test_blackout_date_not_matching_today_does_not_block() -> None:
    strategy = _strategy(blackout_dates=("2026-01-01",))
    basket = _basket()
    signal = strategy.on_candle(_candle(24000.0), _ts(9, 45, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.action is BasketAction.ENTER_BASKET


def test_primary_entry_sells_ce_and_pe_one_otm_step_by_default() -> None:
    strategy = _strategy()
    basket = _basket()
    signal = strategy.on_candle(_candle(24000.0), _ts(9, 45, 0), basket=basket, vix=None)
    assert signal is not None
    assert {leg.role for leg in signal.legs} == {LegRole.CE, LegRole.PE}
    for leg in signal.legs:
        assert leg.side is OrderSide.SELL
        assert leg.option_selection.moneyness is Moneyness.OTM
        assert leg.option_selection.steps == 1
        assert leg.is_replacement is False


def test_primary_entry_anchors_reference_spot_at_candle_close() -> None:
    strategy = _strategy()
    basket = _basket()
    signal = strategy.on_candle(_candle(24075.5), _ts(9, 45, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.state_commit is not None
    assert signal.state_commit.anchor is not None
    assert signal.state_commit.anchor.price == 24075.5
    assert signal.state_commit.anchor.candle_ts == _ts(9, 45, 0)


def test_no_second_primary_entry_attempt_after_consumption() -> None:
    strategy = _strategy()
    basket = _basket(entries_consumed=True)
    signal = strategy.on_candle(_candle(24000.0), _ts(10, 0, 0), basket=basket, vix=None)
    assert signal is None


def test_otm_steps_exact_multiple() -> None:
    strategy = _strategy(otm_distance_points=100, strike_step=50)  # 100/50 = 2
    basket = _basket()
    signal = strategy.on_candle(_candle(24000.0), _ts(9, 45, 0), basket=basket, vix=None)
    assert signal is not None
    assert all(leg.option_selection.steps == 2 for leg in signal.legs)


def test_otm_steps_sub_one_step_floors_to_one() -> None:
    strategy = _strategy(otm_distance_points=20, strike_step=50)  # round(0.4) = 0 -> floor 1
    basket = _basket()
    signal = strategy.on_candle(_candle(24000.0), _ts(9, 45, 0), basket=basket, vix=None)
    assert signal is not None
    assert all(leg.option_selection.steps == 1 for leg in signal.legs)


def test_otm_steps_half_step_uses_python_round() -> None:
    # 75 / 50 = 1.5 -> Python's banker's rounding gives 2.
    strategy = _strategy(otm_distance_points=75, strike_step=50)
    basket = _basket()
    signal = strategy.on_candle(_candle(24000.0), _ts(9, 45, 0), basket=basket, vix=None)
    assert signal is not None
    assert all(leg.option_selection.steps == 2 for leg in signal.legs)


def test_otm_steps_non_multiple_rounds_down() -> None:
    # 120 / 50 = 2.4 -> rounds to 2.
    strategy = _strategy(otm_distance_points=120, strike_step=50)
    basket = _basket()
    signal = strategy.on_candle(_candle(24000.0), _ts(9, 45, 0), basket=basket, vix=None)
    assert signal is not None
    assert all(leg.option_selection.steps == 2 for leg in signal.legs)


# ======================================================================
# Single-leg rolls (spec 17.3)
# ======================================================================
def test_move_of_59_99_does_not_roll_ce() -> None:
    strategy = _strategy()
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    basket = _basket(legs=(ce, pe), entries_consumed=True, reference_price=24000.0)
    signal = strategy.on_candle(_candle(24059.99), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is None


def test_move_of_exactly_60_rolls_ce() -> None:
    strategy = _strategy()
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    basket = _basket(legs=(ce, pe), entries_consumed=True, reference_price=24000.0)
    signal = strategy.on_candle(_candle(24060.0), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.action is BasketAction.ADJUST_LEGS
    assert signal.adjustment is not None
    assert len(signal.adjustment.targets) == 1
    assert signal.adjustment.targets[0].role is LegRole.CE
    assert signal.adjustment.targets[0].leg_id == ce.leg_id


def test_move_of_minus_59_99_does_not_roll_pe() -> None:
    strategy = _strategy()
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    basket = _basket(legs=(ce, pe), entries_consumed=True, reference_price=24000.0)
    signal = strategy.on_candle(_candle(23940.01), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is None


def test_move_of_exactly_minus_60_rolls_pe() -> None:
    strategy = _strategy()
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    basket = _basket(legs=(ce, pe), entries_consumed=True, reference_price=24000.0)
    signal = strategy.on_candle(_candle(23940.0), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.adjustment is not None
    assert len(signal.adjustment.targets) == 1
    assert signal.adjustment.targets[0].role is LegRole.PE
    assert signal.adjustment.targets[0].leg_id == pe.leg_id


def test_up_move_closes_only_ce_pe_untouched() -> None:
    strategy = _strategy()
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    basket = _basket(legs=(ce, pe), entries_consumed=True, reference_price=24000.0)
    signal = strategy.on_candle(_candle(24100.0), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.adjustment is not None
    roles = {t.role for t in signal.adjustment.targets}
    assert roles == {LegRole.CE}


def test_down_move_closes_only_pe_ce_untouched() -> None:
    strategy = _strategy()
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    basket = _basket(legs=(ce, pe), entries_consumed=True, reference_price=24000.0)
    signal = strategy.on_candle(_candle(23900.0), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.adjustment is not None
    roles = {t.role for t in signal.adjustment.targets}
    assert roles == {LegRole.PE}


def test_roll_reanchors_reference_spot_to_trigger_candle_close() -> None:
    strategy = _strategy()
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    basket = _basket(legs=(ce, pe), entries_consumed=True, reference_price=24000.0)
    signal = strategy.on_candle(_candle(24070.0), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.adjustment is not None
    assert signal.adjustment.anchor is not None
    assert signal.adjustment.anchor.price == 24070.0
    assert signal.adjustment.anchor.candle_ts == _ts(9, 50, 0)


def test_replacement_does_not_open_on_the_trigger_candle() -> None:
    # A fresh trigger produces ADJUST_LEGS, never ENTER_LEG, on this candle.
    strategy = _strategy()
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    basket = _basket(legs=(ce, pe), entries_consumed=True, reference_price=24000.0)
    signal = strategy.on_candle(_candle(24100.0), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.action is BasketAction.ADJUST_LEGS


def test_replacement_uses_next_completed_candle_spot_and_fresh_contract() -> None:
    strategy = _strategy()
    old_ce_leg_id = f"{BASKET_ID}:CE:1"
    claim = _claim(
        LegRole.CE, 1, AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value, old_ce_leg_id,
    )
    basket = _basket(
        legs=(_leg(LegRole.PE),),
        entries_consumed=True,
        reference_price=24070.0,
        claims=(claim,),
    )
    signal = strategy.on_candle(_candle(24080.0), _ts(9, 55, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.action is BasketAction.ENTER_LEG
    assert len(signal.legs) == 1
    leg_intent = signal.legs[0]
    assert leg_intent.role is LegRole.CE
    assert leg_intent.side is OrderSide.SELL
    assert leg_intent.is_replacement is True
    assert leg_intent.replaces_leg_id == old_ce_leg_id
    assert leg_intent.option_selection.moneyness is Moneyness.OTM
    assert leg_intent.option_selection.steps == 1


def test_ce_rolls_twice_and_not_a_third_time() -> None:
    strategy = _strategy(max_rolls_ce=2)
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    two_confirmed = (
        _claim(LegRole.CE, 1, AdjustmentLifecycle.REPLACEMENT_FILLED.value, "x1"),
        _claim(LegRole.CE, 2, AdjustmentLifecycle.REPLACEMENT_FILLED.value, "x2"),
    )
    basket = _basket(
        legs=(ce, pe), entries_consumed=True, reference_price=24000.0, claims=two_confirmed,
    )
    signal = strategy.on_candle(_candle(24060.0), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is None


def test_pe_rolls_twice_and_not_a_third_time() -> None:
    strategy = _strategy(max_rolls_pe=2)
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    two_confirmed = (
        _claim(LegRole.PE, 1, AdjustmentLifecycle.REPLACEMENT_FILLED.value, "x1"),
        _claim(LegRole.PE, 2, AdjustmentLifecycle.REPLACEMENT_FILLED.value, "x2"),
    )
    basket = _basket(
        legs=(ce, pe), entries_consumed=True, reference_price=24000.0, claims=two_confirmed,
    )
    signal = strategy.on_candle(_candle(23940.0), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is None


def test_exhausted_ce_budget_does_not_block_pe_budget() -> None:
    strategy = _strategy()
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    ce_exhausted = (
        _claim(LegRole.CE, 1, AdjustmentLifecycle.REPLACEMENT_FILLED.value, "x1"),
        _claim(LegRole.CE, 2, AdjustmentLifecycle.REPLACEMENT_FILLED.value, "x2"),
    )
    basket = _basket(
        legs=(ce, pe), entries_consumed=True, reference_price=24000.0, claims=ce_exhausted,
    )
    signal = strategy.on_candle(_candle(23940.0), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.adjustment is not None
    assert signal.adjustment.targets[0].role is LegRole.PE


def test_exhausted_pe_budget_does_not_block_ce_budget() -> None:
    strategy = _strategy()
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    pe_exhausted = (
        _claim(LegRole.PE, 1, AdjustmentLifecycle.REPLACEMENT_FILLED.value, "x1"),
        _claim(LegRole.PE, 2, AdjustmentLifecycle.REPLACEMENT_FILLED.value, "x2"),
    )
    basket = _basket(
        legs=(ce, pe), entries_consumed=True, reference_price=24000.0, claims=pe_exhausted,
    )
    signal = strategy.on_candle(_candle(24060.0), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.adjustment is not None
    assert signal.adjustment.targets[0].role is LegRole.CE


def test_exact_1510_blocks_a_new_roll() -> None:
    strategy = _strategy()
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    basket = _basket(legs=(ce, pe), entries_consumed=True, reference_price=24000.0)
    signal = strategy.on_candle(_candle(24100.0), _ts(15, 10, 0), basket=basket, vix=None)
    assert signal is None


def test_1509_59_still_allows_a_new_roll() -> None:
    strategy = _strategy()
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    basket = _basket(legs=(ce, pe), entries_consumed=True, reference_price=24000.0)
    signal = strategy.on_candle(_candle(24100.0), _ts(15, 9, 59), basket=basket, vix=None)
    assert signal is not None
    assert signal.action is BasketAction.ADJUST_LEGS


def test_pending_replacement_expires_when_next_candle_is_at_or_after_cutoff() -> None:
    strategy = _strategy()
    claim = _claim(
        LegRole.CE, 1, AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value, f"{BASKET_ID}:CE:1",
    )
    basket = _basket(
        legs=(_leg(LegRole.PE),), entries_consumed=True, reference_price=24070.0, claims=(claim,),
    )
    signal = strategy.on_candle(_candle(24080.0), _ts(15, 10, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.action is BasketAction.NONE
    assert signal.state_commit is not None
    assert signal.state_commit.expire_replacement_for == (LegRole.CE,)


def test_pending_replacement_not_expired_just_before_cutoff() -> None:
    strategy = _strategy()
    claim = _claim(
        LegRole.CE, 1, AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value, f"{BASKET_ID}:CE:1",
    )
    basket = _basket(
        legs=(_leg(LegRole.PE),), entries_consumed=True, reference_price=24070.0, claims=(claim,),
    )
    signal = strategy.on_candle(_candle(24080.0), _ts(15, 9, 59), basket=basket, vix=None)
    assert signal is not None
    assert signal.action is BasketAction.ENTER_LEG


# ======================================================================
# Both-leg mode (spec 17.4)
# ======================================================================
def test_shipped_config_default_is_single_leg_mode() -> None:
    strategy = _strategy()
    assert strategy._single_leg_roll is True


def test_both_leg_mode_qualifying_move_closes_both_legs() -> None:
    strategy = _strategy(single_leg_roll=False)
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    basket = _basket(legs=(ce, pe), entries_consumed=True, reference_price=24000.0)
    signal = strategy.on_candle(_candle(24060.0), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.action is BasketAction.ADJUST_LEGS
    assert signal.adjustment is not None
    roles = {t.role for t in signal.adjustment.targets}
    assert roles == {LegRole.CE, LegRole.PE}


def test_both_leg_mode_down_move_also_closes_both_legs() -> None:
    strategy = _strategy(single_leg_roll=False)
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    basket = _basket(legs=(ce, pe), entries_consumed=True, reference_price=24000.0)
    signal = strategy.on_candle(_candle(23940.0), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.adjustment is not None
    roles = {t.role for t in signal.adjustment.targets}
    assert roles == {LegRole.CE, LegRole.PE}


def test_both_leg_mode_requires_both_budgets_available() -> None:
    strategy = _strategy(single_leg_roll=False)
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    ce_exhausted = (
        _claim(LegRole.CE, 1, AdjustmentLifecycle.REPLACEMENT_FILLED.value, "x1"),
        _claim(LegRole.CE, 2, AdjustmentLifecycle.REPLACEMENT_FILLED.value, "x2"),
    )
    basket = _basket(
        legs=(ce, pe), entries_consumed=True, reference_price=24000.0, claims=ce_exhausted,
    )
    signal = strategy.on_candle(_candle(24060.0), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is None


def test_both_leg_mode_requires_both_legs_open() -> None:
    strategy = _strategy(single_leg_roll=False)
    ce = _leg(LegRole.CE, state=LegState.CLOSED)
    pe = _leg(LegRole.PE)
    basket = _basket(legs=(ce, pe), entries_consumed=True, reference_price=24000.0)
    signal = strategy.on_candle(_candle(24060.0), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is None


def test_both_leg_mode_replacement_waits_for_both_awaiting_next_candle() -> None:
    strategy = _strategy(single_leg_roll=False)
    ce_claim = _claim(
        LegRole.CE, 1, AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value, f"{BASKET_ID}:CE:1",
        claim_group_id="g1",
    )
    pe_claim = _claim(
        LegRole.PE, 1, AdjustmentLifecycle.AWAITING_NEXT_CANDLE.value, f"{BASKET_ID}:PE:1",
        claim_group_id="g1",
    )
    basket = _basket(
        legs=(), entries_consumed=True, reference_price=24060.0, claims=(ce_claim, pe_claim),
    )
    signal = strategy.on_candle(_candle(24065.0), _ts(9, 55, 0), basket=basket, vix=None)
    assert signal is not None
    assert signal.action is BasketAction.ENTER_LEG
    assert {leg.role for leg in signal.legs} == {LegRole.CE, LegRole.PE}
    assert all(leg.is_replacement for leg in signal.legs)


def test_both_leg_mode_one_role_still_unconfirmed_does_not_replace_the_other() -> None:
    # The engine's own group-confirmation gate (_maybe_advance_claim_group)
    # never advances *any* member of a claim_group_id to
    # AWAITING_NEXT_CANDLE until *every* member is EXIT_CONFIRMED — so a
    # durable read model can never show one role AWAITING_NEXT_CANDLE while
    # its group-mate is still EXIT_SUBMISSION_PENDING. The realistic
    # durable shape of "one role unconfirmed" is neither role having
    # reached AWAITING_NEXT_CANDLE yet — proving the strategy needs no
    # group-awareness of its own to avoid a partial replacement: it simply
    # never sees eligibility for either role until the engine's own gate
    # grants it to both together.
    strategy = _strategy(single_leg_roll=False)
    ce_claim = _claim(
        LegRole.CE, 1, AdjustmentLifecycle.EXIT_CONFIRMED.value, f"{BASKET_ID}:CE:1",
        claim_group_id="g1",
    )
    pe_claim = _claim(
        LegRole.PE, 1, AdjustmentLifecycle.EXIT_SUBMISSION_PENDING.value, f"{BASKET_ID}:PE:1",
        claim_group_id="g1",
    )
    basket = _basket(
        legs=(), entries_consumed=True, reference_price=24060.0, claims=(ce_claim, pe_claim),
    )
    signal = strategy.on_candle(_candle(24065.0), _ts(9, 55, 0), basket=basket, vix=None)
    assert signal is None


# ======================================================================
# Risk and exit (spec 17.5)
# ======================================================================
def test_gross_pnl_includes_realised_from_rolled_out_legs() -> None:
    strategy = _strategy()
    closed = _leg(LegRole.CE, state=LegState.CLOSED, realized_gross_pnl=-15_000.0)
    open_pe = _leg(LegRole.PE, entry_price=100.0, last_price=170.0)  # unrealised = -5250
    basket = _basket(legs=(closed, open_pe), entries_consumed=True, reference_price=24000.0)
    tick = _tick("SIM:PE", 170.0, _ts(10, 0, 0))
    signal = strategy.on_leg_tick(open_pe, tick, basket)
    assert signal is not None
    assert signal.action is BasketAction.EXIT_ALL
    assert signal.exit_reason is ExitReason.DAILY_LOSS_LIMIT


def test_combined_stop_minus_19999_does_not_trigger() -> None:
    strategy = _strategy(lots_per_leg=10, combined_stop_per_lot=2000.0)
    # unrealised = (entry - last) * qty = (100 - 126.665) * 750 = -19998.75
    leg = _leg(LegRole.CE, entry_price=100.0, last_price=126.665, quantity=750)
    basket = _basket(legs=(leg,), entries_consumed=True, reference_price=24000.0)
    tick = _tick("SIM:CE", 126.665, _ts(10, 0, 0))
    signal = strategy.on_leg_tick(leg, tick, basket)
    assert signal is None


def test_combined_stop_minus_20000_triggers() -> None:
    strategy = _strategy(lots_per_leg=10, combined_stop_per_lot=2000.0)
    leg = _leg(LegRole.CE, entry_price=100.0, last_price=126.6667, quantity=750)
    basket = _basket(legs=(leg,), entries_consumed=True, reference_price=24000.0)
    tick = _tick("SIM:CE", 126.6667, _ts(10, 0, 0))
    signal = strategy.on_leg_tick(leg, tick, basket)
    assert signal is not None
    assert signal.action is BasketAction.EXIT_ALL


def test_combined_stop_scales_exactly_with_lot_count() -> None:
    strategy = _strategy(lots_per_leg=5, combined_stop_per_lot=2000.0)
    assert strategy._sl_total == 10_000.0
    leg = _leg(LegRole.CE, entry_price=100.0, last_price=113.34, quantity=375)  # U ~= -5002.5
    basket = _basket(legs=(leg,), entries_consumed=True, reference_price=24000.0)
    tick = _tick("SIM:CE", 113.34, _ts(10, 0, 0))
    signal = strategy.on_leg_tick(leg, tick, basket)
    assert signal is None
    leg2 = _leg(LegRole.CE, entry_price=100.0, last_price=126.68, quantity=375)  # U ~= -10005
    basket2 = _basket(legs=(leg2,), entries_consumed=True, reference_price=24000.0)
    tick2 = _tick("SIM:CE", 126.68, _ts(10, 0, 0))
    signal2 = strategy.on_leg_tick(leg2, tick2, basket2)
    assert signal2 is not None


def test_combined_stop_does_not_fire_once_day_already_blocked() -> None:
    strategy = _strategy()
    leg = _leg(LegRole.CE, entry_price=100.0, last_price=500.0, quantity=750)
    basket = _basket(
        legs=(leg,), entries_consumed=True, reference_price=24000.0,
        day_blocked_reason="already blocked",
    )
    tick = _tick("SIM:CE", 500.0, _ts(10, 0, 0))
    signal = strategy.on_leg_tick(leg, tick, basket)
    assert signal is None


def test_on_candle_returns_none_once_day_already_blocked() -> None:
    strategy = _strategy()
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    basket = _basket(
        legs=(ce, pe), entries_consumed=True, reference_price=24000.0,
        day_blocked_reason="already blocked",
    )
    signal = strategy.on_candle(_candle(24100.0), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is None


# ======================================================================
# Durable-state discipline
# ======================================================================
def test_on_candle_never_mutates_basket_or_roll_state() -> None:
    strategy = _strategy()
    ce = _leg(LegRole.CE)
    pe = _leg(LegRole.PE)
    basket = _basket(legs=(ce, pe), entries_consumed=True, reference_price=24000.0)
    snapshot = copy.deepcopy(basket)
    signal = strategy.on_candle(_candle(24100.0), _ts(9, 50, 0), basket=basket, vix=None)
    assert signal is not None  # a real decision was made this call
    assert basket == snapshot


def test_on_candle_primary_entry_never_mutates_basket() -> None:
    strategy = _strategy()
    basket = _basket()
    snapshot = copy.deepcopy(basket)
    signal = strategy.on_candle(_candle(24000.0), _ts(9, 45, 0), basket=basket, vix=None)
    assert signal is not None
    assert basket == snapshot


def test_on_leg_tick_never_mutates_basket() -> None:
    strategy = _strategy()
    leg = _leg(LegRole.CE, entry_price=100.0, last_price=500.0, quantity=750)
    basket = _basket(legs=(leg,), entries_consumed=True, reference_price=24000.0)
    snapshot = copy.deepcopy(basket)
    tick = _tick("SIM:CE", 500.0, _ts(10, 0, 0))
    signal = strategy.on_leg_tick(leg, tick, basket)
    assert signal is not None
    assert basket == snapshot


def test_reset_is_a_true_noop() -> None:
    strategy = _strategy()
    strategy.reset()  # must not raise; no durable state of its own to clear


def test_quantity_lots_reflects_config() -> None:
    strategy = _strategy(lots_per_leg=15)
    assert strategy.quantity_lots == 15
