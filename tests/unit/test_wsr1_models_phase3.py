"""Phase 3 value types: money, parameters, positions, orders, the book.

The rules' decisions are tested in ``test_wsr1_rules*.py``; this file pins the
types' own invariants, which every rule relies on.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    Book,
    BuyFill,
    ClosedTrade,
    OrderAction,
    PendingOrder,
    Position,
    PositionState,
    RulesParameters,
    SaleFill,
    Sizing,
    money,
)

D = Decimal
MON = date(2026, 1, 5)  # ISO 2026-W02


def _sizing(event_risk: bool = False) -> Sizing:
    return Sizing(
        D("0.10"), D("100000.00"), (D("40000.00"), D("30000.00"), D("30000.00")), event_risk
    )


def _position(**kw: object) -> Position:
    base: dict[str, object] = {
        "position_id": "X-2026W02",
        "symbol": "X",
        "sector": "Finance",
        "group": "X",
        "sizing": _sizing(),
        "p1": D("1000.00"),
        "l1": D("900.00"),
        "l2": D("800.00"),
        "stop": D("700.00"),
        "buys": (BuyFill(1, MON, D("1000.00"), 40),),
    }
    base.update(kw)
    return Position(**base)  # type: ignore[arg-type]


def test_money_goes_through_the_shortest_repr() -> None:
    assert money(1226.4) == D("1226.40")
    assert money(0.1 + 0.2) == D("0.30")
    assert money(92592.5925) == D("92592.59")
    with pytest.raises(ValueError):
        money(float("nan"))


def test_default_parameters_are_the_paper_book() -> None:
    params = RulesParameters()
    assert params.max_positions == 10
    assert params.committed_cap == D("1000000.00")
    assert params.buffer == D("0.00")
    with pytest.raises(ValueError, match="sum to 100"):
        RulesParameters(tranches_pct=(D("40"), D("30"), D("20")))


def test_order_action_tranches() -> None:
    assert OrderAction.buy(2) is OrderAction.BUY_T2
    assert OrderAction.BUY_T3.tranche == 3
    assert not OrderAction.SELL_ALL.is_buy
    with pytest.raises(ValueError):
        _ = OrderAction.SELL_HALF.tranche


def test_a_position_starts_with_t1_and_fills_tranches_in_order() -> None:
    with pytest.raises(ValueError, match="T1"):
        _position(buys=(BuyFill(2, MON, D("900"), 10),))
    with pytest.raises(ValueError, match="in order"):
        _position(buys=(BuyFill(1, MON, D("1000"), 40), BuyFill(3, MON, D("800"), 10)))


def test_closed_exactly_when_no_shares_are_held() -> None:
    with pytest.raises(ValueError, match="CLOSED"):
        _position(state=PositionState.CLOSED)
    sold = (SaleFill(OrderAction.SELL_ALL, MON, D("1100"), 40),)
    with pytest.raises(ValueError, match="CLOSED"):
        _position(sales=sold)
    assert _position(sales=sold, state=PositionState.CLOSED).shares_held == 0


def test_committed_is_a_until_the_partial_sale_then_cost_of_shares_held() -> None:
    assert _position().committed == D("100000.00")
    # EVENT_RISK: T1 + T2 only.
    assert _position(sizing=_sizing(event_risk=True)).committed == D("70000.00")
    # A PASS entry whose row turned EVENT_RISK (v1.2f): T3 disabled.
    assert _position(t3_disabled=True).committed == D("70000.00")
    half = _position(
        sales=(SaleFill(OrderAction.SELL_HALF, MON, D("1100"), 20),),
        state=PositionState.HALF_SOLD,
    )
    assert half.committed == D("20000.00")


def test_net_pnl_counts_every_fee() -> None:
    position = _position(
        buys=(BuyFill(1, MON, D("1000.00"), 40, fees=D("48.00")),),
        sales=(SaleFill(OrderAction.SELL_ALL, MON, D("1001.00"), 40, fees=D("59.04")),),
        state=PositionState.CLOSED,
    )
    # gross +40, fees 107.04 -> a net loss (v1.2f: P&L is net of costs)
    assert position.net_pnl == D("-67.04")
    trade = ClosedTrade("X", position.position_id, (2026, 2), position.net_pnl)
    assert trade.is_loss


def test_pending_orders_carry_what_their_fill_needs() -> None:
    with pytest.raises(ValueError, match="amount"):
        PendingOrder(OrderAction.BUY_T2, "X", (2026, 2), MON, "add", position_id="X")
    with pytest.raises(ValueError, match="quantity"):
        PendingOrder(OrderAction.SELL_ALL, "X", (2026, 2), MON, "stop", position_id="X")
    with pytest.raises(ValueError, match="sizing"):
        PendingOrder(OrderAction.BUY_T1, "X", (2026, 2), MON, "entry", amount=D("1"))
    with pytest.raises(ValueError, match="position_id"):
        PendingOrder(OrderAction.SELL_ALL, "X", (2026, 2), MON, "stop", quantity=1)


def test_the_book_holds_one_open_position_per_symbol() -> None:
    with pytest.raises(ValueError, match="one open position"):
        Book(cash=D("0"), positions=(_position(), _position()))
    closed = _position(
        sales=(SaleFill(OrderAction.SELL_ALL, MON, D("1100"), 40),),
        state=PositionState.CLOSED,
    )
    with pytest.raises(ValueError, match="CLOSED"):
        Book(cash=D("0"), positions=(closed,))


def test_last_closed_is_the_latest_exit() -> None:
    book = Book(
        cash=D("0"),
        closed=(
            ClosedTrade("X", "a", (2025, 10), D("5")),
            ClosedTrade("X", "b", (2026, 3), D("-5")),
            ClosedTrade("Y", "c", (2026, 9), D("1")),
        ),
    )
    last = book.last_closed("X")
    assert last is not None and last.position_id == "b"
    assert book.last_closed("Z") is None
