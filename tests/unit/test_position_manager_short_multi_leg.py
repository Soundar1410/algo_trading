"""``PositionManager`` proven for short (SELL) multi-leg use, not assumed.

The straddle_920 port's own architecture correction: ``PositionManager``
*looked* reusable unmodified by ``MultiLegEngine`` (already a
``dict[str, OpenPosition]`` keyed by ``security_id``, already supports
``OrderSide.SELL``) — but that must be proven with tests before being treated
as unmodified infrastructure, not merely asserted from reading the source. If
any of these reveal a genuine limitation in the ``security_id``-keyed identity
model, the fix belongs in ``PositionManager`` generically, never as a
``straddle_920``-specific workaround.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from common.engine.models import ExitReason, OptionContract, OptionType, OrderSide
from common.engine.positions import InMemoryGateway, PositionManager

IST = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 8, 17, 9, 21, tzinfo=IST)
LOT_SIZE = 75
LOTS = 10
QUANTITY = LOTS * LOT_SIZE


def _contract(strike: float, option_type: OptionType, expiry: str = "2026-08-21") -> OptionContract:
    return OptionContract(
        symbol=f"NIFTY {expiry} {strike:g} {option_type.value}",
        security_id=f"SIM:{expiry}:{strike:g}:{option_type.value}",
        strike=strike,
        option_type=option_type,
        expiry=expiry,
        lot_size=LOT_SIZE,
    )


def _manager(slippage: float = 0.0) -> PositionManager:
    return PositionManager(InMemoryGateway(slippage_points=slippage), lots=LOTS)


CE = _contract(24000.0, OptionType.CE)
PE = _contract(24000.0, OptionType.PE)


# ------------------------------------------------------- 1. simultaneous shorts
def test_simultaneous_short_ce_and_pe_positions_coexist() -> None:
    positions = _manager()
    positions.open(CE, OrderSide.SELL, 100.0, T0)
    positions.open(PE, OrderSide.SELL, 95.0, T0)

    assert len(positions.positions) == 2
    ids = {p.contract.security_id for p in positions.positions}
    assert ids == {CE.security_id, PE.security_id}
    assert all(p.side is OrderSide.SELL for p in positions.positions)


# --------------------------------------------------- 2. short unrealised sign
def test_short_unrealised_pnl_sign_is_profit_when_premium_falls() -> None:
    positions = _manager()
    pos = positions.open(CE, OrderSide.SELL, 100.0, T0)
    pos.update_price(60.0)  # premium fell — a short seller profits
    assert pos.unrealised_pnl == (100.0 - 60.0) * QUANTITY
    assert pos.unrealised_pnl > 0


def test_short_unrealised_pnl_sign_is_loss_when_premium_rises() -> None:
    positions = _manager()
    pos = positions.open(CE, OrderSide.SELL, 100.0, T0)
    pos.update_price(150.0)  # premium rose — a short seller loses
    assert pos.unrealised_pnl == (100.0 - 150.0) * QUANTITY
    assert pos.unrealised_pnl < 0


# ------------------------------------------------- 3. short closing direction
def test_closing_a_short_position_buys_to_close() -> None:
    """InMemoryGateway.buy() is adverse-by-construction (fills above the
    reference price) — proving close() calls buy(), not sell(), for a SELL
    position is what proves the closing *direction* is right, not just that
    some fill happened."""
    positions = _manager(slippage=1.0)
    positions.open(CE, OrderSide.SELL, 100.0, T0)

    trade = positions.close(CE.security_id, 40.0, T0, ExitReason.TARGET_PROFIT)

    assert trade.side is OrderSide.SELL  # the position's own side, unchanged
    # buy() fills at ref_price + slippage: 40 + 1 = 41, not 40 - 1 = 39.
    assert trade.exit_price == 41.0


# ------------------------------------------------- 4. gross realised P&L
def test_short_gross_realised_pnl_is_entry_minus_exit_times_quantity() -> None:
    positions = _manager()
    positions.open(CE, OrderSide.SELL, 100.0, T0)
    trade = positions.close(CE.security_id, 30.0, T0, ExitReason.TARGET_PROFIT)

    assert trade.gross_pnl == (100.0 - 30.0) * QUANTITY
    assert trade.gross_pnl > 0


# --------------------------------------------- 5. close only one leg
def test_closing_one_leg_retains_the_other() -> None:
    positions = _manager()
    positions.open(CE, OrderSide.SELL, 100.0, T0)
    positions.open(PE, OrderSide.SELL, 95.0, T0)

    positions.close(CE.security_id, 200.0, T0, ExitReason.ADJUSTMENT)

    assert positions.get(CE.security_id) is None
    remaining = positions.get(PE.security_id)
    assert remaining is not None
    assert remaining.side is OrderSide.SELL
    assert len(positions.trades) == 1
    assert positions.trades[0].contract.security_id == CE.security_id


# --------------------------------------- 6. reopen the same security same day
def test_closing_and_reopening_the_same_security_is_a_new_leg_instance() -> None:
    """PositionManager itself has no notion of "leg instance" — that identity
    is the multi-leg engine's (basket_id/leg_id), layered on top. What
    PositionManager must get right underneath is exactly this: the same
    security_id key can be closed and then opened again the same day, and the
    earlier closed Trade is never overwritten."""
    positions = _manager()
    positions.open(CE, OrderSide.SELL, 100.0, T0)
    first_close = positions.close(CE.security_id, 205.0, T0, ExitReason.ADJUSTMENT)

    # A same-day "replacement" at the same strike/expiry resolves to the same
    # security_id — open() must accept it now that the prior one is closed.
    positions.open(CE, OrderSide.SELL, 50.0, T0)
    second_close = positions.close(CE.security_id, 10.0, T0, ExitReason.SQUARE_OFF)

    assert len(positions.trades) == 2
    assert positions.trades[0] is first_close
    assert positions.trades[1] is second_close
    assert first_close.entry_price == 100.0 and first_close.exit_price == 205.0
    assert second_close.entry_price == 50.0 and second_close.exit_price == 10.0


def test_opening_the_same_security_while_still_open_is_refused() -> None:
    """The other half of the same property: PositionManager must refuse a
    second open() for a security_id that is already open — this is exactly
    why a replacement leg must resolve to a *different* security_id (a moved
    ATM strike) while the adjusted-out leg is still being closed, never the
    same one reused mid-flight."""
    positions = _manager()
    positions.open(CE, OrderSide.SELL, 100.0, T0)
    try:
        positions.open(CE, OrderSide.SELL, 50.0, T0)
    except RuntimeError as exc:
        assert "already open" in str(exc)
    else:
        raise AssertionError("opening an already-open security_id must raise")


# ------------------------------------------------------- 7. close_all, 2 legs
def test_close_all_with_two_open_short_legs() -> None:
    positions = _manager()
    positions.open(CE, OrderSide.SELL, 100.0, T0)
    positions.open(PE, OrderSide.SELL, 95.0, T0)

    closed = positions.close_all(
        {CE.security_id: 20.0, PE.security_id: 15.0}, T0, ExitReason.SQUARE_OFF
    )

    assert len(closed) == 2
    assert not positions.has_position()
    assert all(t.exit_reason is ExitReason.SQUARE_OFF for t in closed)


# --------------------------------------------------- 8. independent charges
def test_entry_charges_are_tracked_independently_per_leg() -> None:
    from common.broker.costs import CostRates

    positions = PositionManager(InMemoryGateway(rates=CostRates()), lots=LOTS)
    positions.open(CE, OrderSide.SELL, 100.0, T0)
    positions.open(PE, OrderSide.SELL, 95.0, T0)

    ce_trade = positions.close(CE.security_id, 10.0, T0, ExitReason.SQUARE_OFF)
    pe_trade = positions.close(PE.security_id, 10.0, T0, ExitReason.SQUARE_OFF)

    # Different entry premiums -> different (percentage-based) charges; each
    # trade's charges must reflect only its own leg, never the other's.
    assert ce_trade.charges != pe_trade.charges
    assert ce_trade.charges > 0
    assert pe_trade.charges > 0


# --------------------------------------------- 9. replacement after adjustment
def test_replacement_leg_after_an_adjusted_out_leg_is_fully_independent() -> None:
    """The end-to-end shape a leg-doubling adjustment produces: the original
    CE closes (a different strike, since the ATM moved), a replacement CE
    opens at the new strike, and PE is never touched throughout."""
    positions = _manager()
    original_ce = _contract(24000.0, OptionType.CE)
    replacement_ce = _contract(24050.0, OptionType.CE)

    positions.open(original_ce, OrderSide.SELL, 100.0, T0)
    positions.open(PE, OrderSide.SELL, 95.0, T0)

    positions.close(original_ce.security_id, 205.0, T0, ExitReason.ADJUSTMENT)
    positions.open(replacement_ce, OrderSide.SELL, 90.0, T0)

    assert len(positions.positions) == 2
    open_ids = {p.contract.security_id for p in positions.positions}
    assert open_ids == {replacement_ce.security_id, PE.security_id}
    assert len(positions.trades) == 1
    assert positions.trades[0].exit_reason is ExitReason.ADJUSTMENT
    assert positions.trades[0].contract.security_id == original_ce.security_id
