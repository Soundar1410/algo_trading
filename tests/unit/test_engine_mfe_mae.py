"""MFE/MAE tracking and its propagation into a closed Trade.

**A port of the reference repository's ``tests/test_mfe_mae.py``** (7 tests), from
Phase 3 Part 2b-i. Names and assertions are the reference's; three mechanical
substitutions were unavoidable and are the only changes:

* ``Position`` -> :class:`~common.engine.models.OpenPosition` (deviation D19 —
  this repository's ``Position`` name is taken by the persisted model).
* ``PaperBroker(PaperTradingConfig(...))`` -> :class:`~common.engine.positions.
  InMemoryGateway`, the position seam. The reference built a *zero-charge*
  ``PaperBroker`` purely so charges would not perturb the MFE/MAE arithmetic;
  ``InMemoryGateway(slippage_points=0, rates=_ZERO_RATES)`` is the same thing said
  in this repository's vocabulary.
* ``pm.open(contract, side, ref_price=..., ts=...)`` keeps its exact signature.

Everything the file actually *tests* — the running best/worst unrealised P&L, that
the exit fill itself can set a new extreme, and that ``close_all`` keeps them
per-position — is unchanged, which is what makes this evidence that the port
preserved behaviour rather than merely compiled.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.broker.costs import CostRates
from common.engine.models import (
    ExitReason,
    OpenPosition,
    OptionContract,
    OptionType,
    OrderSide,
)
from common.engine.positions import InMemoryGateway, PositionManager

IST = ZoneInfo("Asia/Kolkata")

#: The reference's all-zero ``ChargesConfig``, field for field.
_ZERO_RATES = CostRates(
    brokerage_per_order=0.0,
    exchange_charge_rate=0.0,
    stt_rate_on_sell=0.0,
    sebi_charge_rate=0.0,
    gst_rate=0.0,
    stamp_duty_rate_on_buy=0.0,
)


def _contract(strike: float = 20000) -> OptionContract:
    return OptionContract(
        symbol=f"NIFTY WEEKLY {int(strike)} CE",
        security_id=f"SIM:{int(strike)}:CE",
        strike=strike,
        option_type=OptionType.CE,
        expiry="2026-07-09",
        lot_size=65,
    )


def _zero_charge_gateway() -> InMemoryGateway:
    return InMemoryGateway(slippage_points=0.0, rates=_ZERO_RATES)


# --------------------------------------------------------------- OpenPosition
def test_position_tracks_mfe_and_mae_for_long() -> None:
    pos = OpenPosition(
        contract=_contract(),
        side=OrderSide.BUY,
        lots=1,
        entry_price=100.0,
        entry_time=datetime.now(IST),
    )
    assert pos.max_favorable_pnl == 0.0
    assert pos.max_adverse_pnl == 0.0

    pos.update_price(110.0)  # +10 * 65 = +650 favorable
    assert pos.max_favorable_pnl == 650.0
    assert pos.max_adverse_pnl == 0.0

    pos.update_price(90.0)  # -10 * 65 = -650 adverse
    assert pos.max_favorable_pnl == 650.0  # best point stays recorded
    assert pos.max_adverse_pnl == -650.0

    pos.update_price(105.0)  # partial recovery, not a new extreme either way
    assert pos.max_favorable_pnl == 650.0
    assert pos.max_adverse_pnl == -650.0


def test_position_tracks_mfe_and_mae_for_short() -> None:
    # SELL/written option profits as price falls.
    pos = OpenPosition(
        contract=_contract(),
        side=OrderSide.SELL,
        lots=1,
        entry_price=100.0,
        entry_time=datetime.now(IST),
    )
    pos.update_price(80.0)  # price fell -> favorable for a short: +20*65
    assert pos.max_favorable_pnl == 1300.0
    pos.update_price(120.0)  # price rose -> adverse for a short: -20*65
    assert pos.max_adverse_pnl == -1300.0


def test_position_never_favorable_if_only_moves_against() -> None:
    pos = OpenPosition(
        contract=_contract(),
        side=OrderSide.BUY,
        lots=1,
        entry_price=100.0,
        entry_time=datetime.now(IST),
    )
    pos.update_price(80.0)
    pos.update_price(70.0)
    assert pos.max_favorable_pnl == 0.0  # never went above entry
    assert pos.max_adverse_pnl == -1950.0  # (70-100)*65


# --------------------------------------------------------- Trade / capital_used
def test_trade_capital_used_is_entry_price_times_quantity() -> None:
    pm = PositionManager(_zero_charge_gateway(), lots=2)
    contract = _contract()
    t0 = datetime.now(IST)

    pm.open(contract, OrderSide.BUY, ref_price=150.0, ts=t0)
    trade = pm.close(
        contract.security_id,
        ref_price=150.0,
        ts=t0 + timedelta(minutes=1),
        reason=ExitReason.MANUAL,
    )
    assert trade.capital_used == 150.0 * 2 * 65


# ----------------------------------------------------- PositionManager.close()
def test_close_propagates_mfe_mae_from_position_ticks() -> None:
    pm = PositionManager(_zero_charge_gateway(), lots=1)
    contract = _contract()
    t0 = datetime.now(IST)

    pm.open(contract, OrderSide.BUY, ref_price=100.0, ts=t0)
    pos = pm.get(contract.security_id)
    assert pos is not None

    # Simulate ticks the way the engine does (the engine calls pos.update_price).
    pos.update_price(130.0)  # ran up +30*65 = 1950
    pos.update_price(90.0)  # then dropped to -10*65 = -650

    trade = pm.close(
        contract.security_id,
        ref_price=105.0,
        ts=t0 + timedelta(minutes=10),
        reason=ExitReason.TARGET_PROFIT,
    )
    assert trade.mfe == 1950.0
    assert trade.mae == -650.0


def test_close_folds_exit_fill_into_mfe_mae() -> None:
    """The exit fill itself can set a new extreme not seen in prior ticks."""
    pm = PositionManager(_zero_charge_gateway(), lots=1)
    contract = _contract()
    t0 = datetime.now(IST)

    pm.open(contract, OrderSide.BUY, ref_price=100.0, ts=t0)
    pos = pm.get(contract.security_id)
    assert pos is not None
    pos.update_price(105.0)  # modest +5*65 = 325 so far

    # Exit fills much higher than any prior tick -> should become the new MFE.
    trade = pm.close(
        contract.security_id,
        ref_price=200.0,
        ts=t0 + timedelta(minutes=5),
        reason=ExitReason.TARGET_PROFIT,
    )
    assert trade.mfe == pytest.approx((200.0 - 100.0) * 65)


def test_close_all_propagates_mfe_mae_per_position() -> None:
    pm = PositionManager(_zero_charge_gateway(), lots=1)
    c1, c2 = _contract(20000), _contract(20100)
    t0 = datetime.now(IST)

    pm.open(c1, OrderSide.BUY, ref_price=100.0, ts=t0)
    pm.open(c2, OrderSide.BUY, ref_price=50.0, ts=t0)
    first, second = pm.get(c1.security_id), pm.get(c2.security_id)
    assert first is not None and second is not None
    first.update_price(140.0)
    second.update_price(30.0)

    trades = pm.close_all(
        {c1.security_id: 140.0, c2.security_id: 30.0},
        t0 + timedelta(minutes=1),
        ExitReason.SQUARE_OFF,
    )
    by_symbol = {t.contract.security_id: t for t in trades}
    assert by_symbol[c1.security_id].mfe == pytest.approx((140.0 - 100.0) * 65)
    assert by_symbol[c2.security_id].mae == pytest.approx((30.0 - 50.0) * 65)
