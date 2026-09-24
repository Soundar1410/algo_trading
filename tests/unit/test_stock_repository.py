"""Phase 4a: the ``stock_`` repository round-trips the rules' own types exactly."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from _wsr1_rules_fixtures import PARAMS, fill, friday, open_position, universe_row, week

from runtimes.positional_stocks.database import open_stock_database
from runtimes.positional_stocks.repository import StockRepository, order_id, week_key, week_text
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    OnExit,
    OrderAction,
    PendingOrder,
    Position,
    PositionState,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import sizing

D = Decimal
STRATEGY = "wsr1_weekly_stochrsi"


@pytest.fixture
def repo(tmp_path: Path) -> StockRepository:
    return StockRepository(
        open_stock_database(tmp_path / "positional_stocks.db"), STRATEGY, D("1000000")
    )


def _entry_order(symbol: str = "X") -> PendingOrder:
    size = sizing(0.072, True, PARAMS)
    return PendingOrder(
        OrderAction.BUY_T1,
        symbol,
        week(209),
        friday(209),
        "entry: trigger",
        amount=size.tranche_amounts[0],
        sizing=size,
        sector="Power",
        group="GRP",
    )


def _store(repo: StockRepository, position: Position, order: PendingOrder, cash_delta: str) -> None:
    with repo.database.transaction() as conn:
        repo.save_position(conn, position, week(210))
        repo.save_order(conn, order)
        repo.resolve_order(conn, order, state="FILLED", week=week(210), resolution="filled")
        repo.save_fill(
            conn,
            order,
            position,
            cash_delta=D(cash_delta),
            not_traded_on_execution_session=False,
            week=week(210),
        )


def test_week_text_round_trips_including_week_53() -> None:
    assert week_text((2026, 53)) == "2026-W53"
    assert week_key("2026-W53") == (2026, 53)
    assert week_key(week_text((2027, 1))) == (2027, 1)


def test_a_pending_order_keeps_its_sizing_sector_and_group(repo: StockRepository) -> None:
    order = _entry_order()
    with repo.database.transaction() as conn:
        repo.save_order(conn, order)
    (loaded,) = repo.pending_orders()
    assert loaded == order
    assert order_id(STRATEGY, order) == f"{STRATEGY}:{week_text(week(209))}:X:BUY_T1"


def test_a_position_round_trips_with_its_bookkeeping(repo: StockRepository) -> None:
    position = open_position("X", atr_pct=0.072, p1=1000.0, fill_week=210, at_week_open=False)
    position = replace(position, touch_week=week(211), t3_disabled=True)
    _store(repo, position, replace(_entry_order(), sizing=position.sizing), "-37000.00")
    (loaded,) = repo.positions().values()
    assert loaded == position
    assert loaded.buys[0].at_week_open is False
    assert repo.cash() == D("963000.00")


def test_a_zero_share_half_sold_position_round_trips(repo: StockRepository) -> None:
    one = open_position("ONE", atr_pct=0.06, p1=30000.0, fill_week=210)
    one = replace(one, state=PositionState.HALF_SOLD, half_sold_week=week(215))
    size_order = replace(_entry_order("ONE"), sizing=one.sizing)
    _store(repo, one, size_order, "-30000.00")
    (loaded,) = repo.positions().values()
    assert loaded == one and loaded.partial_week == week(215)


def test_the_book_splits_held_closed_and_pending(repo: StockRepository) -> None:
    held = open_position("H", atr_pct=0.06, p1=1000.0, fill_week=200)
    _store(repo, held, replace(_entry_order("H"), sizing=held.sizing), "-40000.00")
    loser = open_position("L", atr_pct=0.06, p1=1000.0, fill_week=200)
    _store(repo, loser, replace(_entry_order("L"), sizing=loser.sizing), "-40000.00")
    closed = fill(loser, OrderAction.SELL_ALL, price=900.0, week_index=205, quantity=40)
    exit_order = PendingOrder(
        OrderAction.SELL_ALL,
        "L",
        week(204),
        friday(204),
        "stop",
        position_id=loser.position_id,
        quantity=40,
    )
    with repo.database.transaction() as conn:
        repo.save_position(conn, closed, week(205))
        repo.save_order(conn, exit_order)
        repo.resolve_order(conn, exit_order, state="FILLED", week=week(205), resolution="filled")
        repo.save_fill(
            conn,
            exit_order,
            closed,
            cash_delta=D("36000.00"),
            not_traded_on_execution_session=False,
            week=week(205),
        )
    waiting = _entry_order("W")
    with repo.database.transaction() as conn:
        repo.save_order(conn, waiting)
    book = repo.book()
    assert [p.symbol for p in book.positions] == ["H"]
    (trade,) = book.closed
    assert (trade.symbol, trade.exit_week, trade.net_pnl) == ("L", week(205), D("-4000.00"))
    assert book.pending == (waiting,)
    # 10L - 40,000 (H) - 40,000 (L) + 36,000 (L's exit)
    assert book.cash == D("956000.00")


def test_resolving_an_order_twice_is_refused(repo: StockRepository) -> None:
    order = _entry_order()
    with repo.database.transaction() as conn:
        repo.save_order(conn, order)
        repo.resolve_order(conn, order, state="SKIPPED", week=week(210), resolution="0 shares")
    with pytest.raises(RuntimeError, match="not PENDING"), repo.database.transaction() as conn:
        repo.resolve_order(conn, order, state="FILLED", week=week(210), resolution="again")
    assert repo.pending_orders() == []


def test_last_seen_universe_rows_are_upserted(repo: StockRepository) -> None:
    first = universe_row("X", "Power", on_exit=OnExit.EXIT)
    with repo.database.transaction() as conn:
        repo.save_universe_seen(conn, [first], week(209))
    renamed = replace(first, company="X Ltd", on_exit=OnExit.HOLD)
    with repo.database.transaction() as conn:
        repo.save_universe_seen(conn, [renamed], week(210))
    assert repo.last_seen_rows() == {"X": renamed}


def test_brakes_default_before_any_week(repo: StockRepository) -> None:
    brakes = repo.brakes()
    assert brakes.peak is None and brakes.brake1_can_fire and not brakes.brake2_active
    assert repo.latest_run() is None
    repo.mark_started(date(2026, 9, 18), (2026, 38), "fp")
    assert repo.run_status(date(2026, 9, 18)) == "STARTED"
