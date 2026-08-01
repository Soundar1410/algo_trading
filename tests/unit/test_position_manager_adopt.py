"""``PositionManager.adopt`` — taking over a position without placing an order.

Phase 3 Part 2b-ii-B-2. The restart case has one property that matters above all the
others: **adopting must not trade.** A previous process already opened this position
and the database already holds it; calling :meth:`PositionManager.open` here would go
to the gateway, place a second order, and double the exposure that recovery exists to
prevent. So the gateway used throughout this module raises on contact — a recording
double would let a regression pass while merely counting it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.engine.models import ExitReason, OptionContract, OptionType, OrderSide
from common.engine.positions import FillOutcome, InMemoryGateway, PositionManager

IST = ZoneInfo("Asia/Kolkata")
ENTRY_TIME = datetime(2026, 7, 16, 9, 21, tzinfo=IST)
LOT_SIZE = 65


class _ExplodingGateway:
    """Any call is a defect, so any call fails the test that made it."""

    def buy(self, contract, lots, *, ref_price, ts):
        raise AssertionError("adopt must not place an order; a position already exists")

    def sell(self, contract, lots, *, ref_price, ts):
        raise AssertionError("adopt must not place an order; a position already exists")


class _ExitOnlyGateway(_ExplodingGateway):
    """Refuses to open, allows exactly one closing leg at a fixed price."""

    def __init__(self, exit_price: float, charges: float = 0.0) -> None:
        self.exit_price = exit_price
        self.charges = charges
        self.sells = 0

    def sell(self, contract, lots, *, ref_price, ts):
        self.sells += 1
        return FillOutcome(fill_price=self.exit_price, charges=self.charges)


def _contract(security_id: str = "SIM:NIFTY:WEEKLY:24000:CE") -> OptionContract:
    return OptionContract(
        symbol="NIFTY 24000 CE",
        security_id=security_id,
        strike=24000.0,
        option_type=OptionType.CE,
        expiry="2026-07-23",
        lot_size=LOT_SIZE,
    )


# ------------------------------------------------------------- the core property
def test_adopting_places_no_order():
    manager = PositionManager(_ExplodingGateway(), lots=1)

    position = manager.adopt(_contract(), OrderSide.BUY, 1, 100.0, ENTRY_TIME)

    assert position.entry_price == 100.0
    assert position.quantity == LOT_SIZE
    assert manager.has_position(_contract().security_id)


def test_the_adopted_position_is_keyed_the_same_way_an_opened_one_is():
    """``get`` is how the engine routes an option tick, so the key has to match."""
    manager = PositionManager(_ExplodingGateway(), lots=1)
    contract = _contract()
    manager.adopt(contract, OrderSide.BUY, 1, 100.0, ENTRY_TIME)

    assert manager.get(contract.security_id) is not None
    assert [p.contract.security_id for p in manager.positions] == [contract.security_id]


def test_adopting_twice_is_refused():
    manager = PositionManager(_ExplodingGateway(), lots=1)
    manager.adopt(_contract(), OrderSide.BUY, 1, 100.0, ENTRY_TIME)

    with pytest.raises(RuntimeError, match="already open"):
        manager.adopt(_contract(), OrderSide.BUY, 1, 100.0, ENTRY_TIME)


def test_opening_on_top_of_an_adopted_position_is_still_refused():
    """The doubling this whole mechanism exists to prevent, asserted directly."""
    manager = PositionManager(InMemoryGateway(), lots=1)
    manager.adopt(_contract(), OrderSide.BUY, 1, 100.0, ENTRY_TIME)

    with pytest.raises(RuntimeError, match="already open"):
        manager.open(_contract(), OrderSide.BUY, 100.0, ENTRY_TIME)


# ----------------------------------------------------------------- the details
def test_the_adopted_position_carries_the_lots_it_was_given_not_the_managers():
    """A restarted worker may be configured with a different size than the run that
    opened the position. The position's own size is what must be closed."""
    manager = PositionManager(_ExplodingGateway(), lots=5)

    position = manager.adopt(_contract(), OrderSide.BUY, 2, 100.0, ENTRY_TIME)

    assert position.lots == 2
    assert position.quantity == 2 * LOT_SIZE


def test_excursion_restarts_at_zero_rather_than_being_invented():
    """MFE/MAE before the restart are recorded nowhere.

    Zero is visibly a floor; a fabricated figure would look like an observation.
    """
    manager = PositionManager(_ExplodingGateway(), lots=1)
    position = manager.adopt(_contract(), OrderSide.BUY, 1, 100.0, ENTRY_TIME)

    assert position.max_favorable_pnl == 0.0
    assert position.max_adverse_pnl == 0.0


def test_a_known_last_price_is_marked_immediately():
    manager = PositionManager(_ExplodingGateway(), lots=1)
    position = manager.adopt(_contract(), OrderSide.BUY, 1, 100.0, ENTRY_TIME, last_price=110.0)

    assert position.last_price == 110.0
    assert position.unrealised_pnl == pytest.approx(10.0 * LOT_SIZE)


def test_without_a_last_price_the_position_is_marked_at_entry():
    manager = PositionManager(_ExplodingGateway(), lots=1)
    position = manager.adopt(_contract(), OrderSide.BUY, 1, 100.0, ENTRY_TIME)

    assert position.last_price == 100.0
    assert position.unrealised_pnl == 0.0


def test_a_written_option_is_adopted_on_the_correct_side():
    manager = PositionManager(_ExplodingGateway(), lots=1)
    position = manager.adopt(_contract(), OrderSide.SELL, 1, 100.0, ENTRY_TIME, last_price=90.0)

    assert position.side is OrderSide.SELL
    # A short profits as the premium falls; getting this backwards would exit on the
    # wrong side of every risk check.
    assert position.unrealised_pnl == pytest.approx(10.0 * LOT_SIZE)


# ------------------------------------------------------- closing what was adopted
def test_the_entry_charges_of_the_previous_run_are_carried_into_the_closed_trade():
    """Otherwise the round trip under-reports its cost by exactly one leg.

    The entry charge was paid, and is on the persisted position row; a restart that
    dropped it would book a trade that looks more profitable than it was.
    """
    gateway = _ExitOnlyGateway(exit_price=120.0, charges=7.0)
    manager = PositionManager(gateway, lots=1)
    manager.adopt(_contract(), OrderSide.BUY, 1, 100.0, ENTRY_TIME, entry_charges=11.0)

    trade = manager.close(
        _contract().security_id, 120.0, ENTRY_TIME + timedelta(minutes=5), ExitReason.SQUARE_OFF
    )

    assert gateway.sells == 1
    assert trade.gross_pnl == pytest.approx(20.0 * LOT_SIZE)
    assert trade.charges == pytest.approx(18.0)
    assert trade.net_pnl == pytest.approx(20.0 * LOT_SIZE - 18.0)


def test_an_adopted_position_closes_into_an_ordinary_trade():
    gateway = _ExitOnlyGateway(exit_price=80.0)
    manager = PositionManager(gateway, lots=1)
    manager.adopt(_contract(), OrderSide.BUY, 1, 100.0, ENTRY_TIME)

    trade = manager.close(
        _contract().security_id, 80.0, ENTRY_TIME + timedelta(minutes=5), ExitReason.SQUARE_OFF
    )

    assert trade.exit_reason is ExitReason.SQUARE_OFF
    assert trade.entry_price == 100.0
    assert trade.exit_price == 80.0
    assert trade.entry_time == ENTRY_TIME
    assert manager.trades == [trade]
    assert not manager.has_position()


def test_close_all_reaches_an_adopted_position():
    """Square-off must flatten a position this process did not open."""
    gateway = _ExitOnlyGateway(exit_price=95.0)
    manager = PositionManager(gateway, lots=1)
    manager.adopt(_contract(), OrderSide.BUY, 1, 100.0, ENTRY_TIME)

    closed = manager.close_all({}, ENTRY_TIME + timedelta(minutes=5), ExitReason.SQUARE_OFF)

    assert len(closed) == 1
    assert not manager.has_position()
