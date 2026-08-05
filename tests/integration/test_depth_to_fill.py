"""Depth, end to end: a bid/ask on the tape becomes the price on the fill.

**The clearest single statement of what Phase 4 Part 5 changed.** Run the same
engine over the same tape before this part and every fill came back
``ltp_fallback``, because depth was dropped at four consecutive places between the
socket and the simulator — the adapter never read it, the gateway's verbs could
not carry it, ``Signal`` had no field for it, and ``OrderLifecycle`` built its
``Quote`` from the signal's reference price alone.

Nothing is faked below the strategy: real SQLite behind real migrations, a real
``ExecutionRepository``, ``OrderLifecycle``, ``LifecycleGateway``, ``PaperBroker``
and ``TradingEngine``. The tape carries bid/ask exactly as
``DhanMarketFeedAdapter`` now produces it from a Full-mode frame, and the
``QuoteBook`` is wired the way ``engine_worker._build`` wires it — as a feed
observer, ahead of the engine's own handler.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from common.broker import PaperBroker, PaperFillConfig, QuoteBook, SlippageConfig
from common.config.models import ExecutionMode
from common.engine.config import EngineConfig, SessionConfig
from common.engine.engine import TradingEngine
from common.engine.feed import SimulatedFeed
from common.engine.gateway import LifecycleGateway
from common.engine.positions import PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.execution import ExecutionRepository, OrderLifecycle
from common.models import Tick
from common.persistence import Database, MigrationRunner
from strategies.intraday_options.engine_fixture_strategy import EngineFixtureStrategy

IST = ZoneInfo("Asia/Kolkata")
UNDERLYING = "INDEX"
CE_CONTRACT = "SIM:NIFTY:WEEKLY:24000:CE"
LOT_SIZE = 65
RUNTIME_ID = "intraday_options"
STRATEGY_ID = "engine01"
TRADING_DATE = "2026-07-16"

#: A half-rupee spread on the option, held constant across the walk so the
#: assertions are about *which side* was taken rather than about the level.
SPREAD = 0.50


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 7, 16, hour, minute, second, tzinfo=IST)


def _underlying_tick(price: float, ts: datetime) -> Tick:
    """An index tick. Carries no book — an index has none in any feed mode."""
    return Tick(
        security_id=UNDERLYING,
        instrument=UNDERLYING,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


def _option_tick(price: float, ts: datetime, *, book: bool = True) -> Tick:
    return Tick(
        security_id=CE_CONTRACT,
        instrument=CE_CONTRACT,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
        bid_price=round(price - SPREAD / 2, 2) if book else None,
        ask_price=round(price + SPREAD / 2, 2) if book else None,
    )


def _tape(*, book: bool) -> list[Tick]:
    """Enter on the second underlying tick, walk the premium down, exit on policy."""
    return [
        _underlying_tick(24000.0, _ts(9, 16)),
        _underlying_tick(24010.0, _ts(9, 21)),
        _option_tick(100.0, _ts(9, 21, 30), book=book),
        _option_tick(105.0, _ts(9, 23), book=book),
        _option_tick(110.0, _ts(9, 26), book=book),
        _option_tick(108.0, _ts(9, 28), book=book),
        _option_tick(90.0, _ts(9, 31), book=book),
        _option_tick(85.0, _ts(9, 33), book=book),
        _option_tick(80.0, _ts(9, 36), book=book),
    ]


@pytest.fixture
def repository(database_path):
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    yield ExecutionRepository(database)
    database.close()


def _run(repository: ExecutionRepository, ticks: Sequence[Tick]) -> PositionManager:
    """Drive the full stack, wired as ``engine_worker._build`` wires it."""
    session = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=4242,
    )
    quotes = QuoteBook()
    broker = PaperBroker(
        # No slippage of any kind, so the only thing that can move a fill off the
        # mid is which side of the book it took. That is the property under test.
        config=PaperFillConfig(
            slippage=SlippageConfig(market_order_ticks=0), ltp_fallback_extra_ticks=0
        ),
        quotes=quotes,
    )
    gateway = LifecycleGateway(
        OrderLifecycle(
            repository=repository,
            broker=broker,
            runtime_id=RUNTIME_ID,
            strategy_id=STRATEGY_ID,
            execution_mode=ExecutionMode.PAPER,
            session_id=session.id,
            quotes=quotes,
        ),
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )
    positions = PositionManager(gateway, lots=1)
    feed = SimulatedFeed(list(ticks))
    # Ahead of the engine's handler: the engine's reaction to a tick may be to
    # place an order priced off exactly that tick's quote.
    feed.add_tick_observer(quotes.record)
    TradingEngine(
        EngineConfig(
            timeframe="5m",
            session=SessionConfig(
                timezone="Asia/Kolkata",
                start_time="09:15",
                end_time="15:15",
                square_off_time="15:20",
            ),
        ),
        feed=feed,
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=LOT_SIZE), strike_step=50
        ),
        strategy=EngineFixtureStrategy(enter_on_candle=1, premium_exit=True),
        position_manager=positions,
        underlying_security_id=UNDERLYING,
    ).run()
    return positions


def _fills(repository: ExecutionRepository) -> list[sqlite3.Row]:
    return list(repository.database.connect().execute("SELECT * FROM fills ORDER BY id").fetchall())


# ------------------------------------------------------------------ the gate
def test_a_depth_carrying_tape_produces_bid_ask_fills_end_to_end(repository):
    """Before Part 5 this same run produced ``ltp_fallback`` for every fill."""
    _run(repository, _tape(book=True))

    fills = _fills(repository)
    assert len(fills) == 2, "one entry and one exit"
    assert {row["fill_method"] for row in fills} == {"bid_ask"}


def test_the_entry_takes_the_ask_and_the_exit_takes_the_bid(repository):
    """A long is opened by *buying*, so it pays the ask; the exit *sells* into the
    bid. Getting these the wrong way round would make a round trip earn the spread
    rather than pay it, which is the failure mode a simulator must not have."""
    positions = _run(repository, _tape(book=True))

    trade = positions.trades[0]
    assert trade.entry_price == pytest.approx(100.0 + SPREAD / 2)
    assert trade.exit_price == pytest.approx(80.0 - SPREAD / 2)


def test_the_round_trip_pays_the_spread_rather_than_earning_it(repository):
    """Limitation 5 in one number. With no depth the same tape reported a cost of
    exactly zero, so paper P&L systematically flattered every strategy by the full
    width of the book, twice per trade."""
    with_book = _run(repository, _tape(book=True)).trades[0]

    gross_at_mid = (80.0 - 100.0) * LOT_SIZE
    gross_with_spread = (with_book.exit_price - with_book.entry_price) * LOT_SIZE

    assert gross_with_spread < gross_at_mid
    assert gross_at_mid - gross_with_spread == pytest.approx(SPREAD * LOT_SIZE)


def test_a_tape_without_depth_still_trades_and_says_it_fell_back(repository):
    """The recorded tapes every other test uses carry no book, and must keep
    working — reporting ``ltp_fallback`` rather than failing or pretending."""
    positions = _run(repository, _tape(book=False))

    assert len(positions.trades) == 1
    assert {row["fill_method"] for row in _fills(repository)} == {"ltp_fallback"}


def test_the_book_behind_each_fill_is_persisted(repository):
    """Spec section 6 asks for the submission-time quote, not only the price
    derived from it — so a paper P&L can be audited back to the book that
    produced it rather than merely recomputed."""
    _run(repository, _tape(book=True))

    rows = list(
        repository.database.connect()
        .execute("SELECT quote_bid, quote_ask, fill_method FROM paper_fill_quotes ORDER BY id")
        .fetchall()
    )
    assert len(rows) == 2
    entry = rows[0]
    assert entry["quote_ask"] == pytest.approx(100.0 + SPREAD / 2)
    assert entry["quote_bid"] == pytest.approx(100.0 - SPREAD / 2)
    assert entry["fill_method"] == "bid_ask"


def test_the_quote_book_saw_both_instruments(repository):
    """The observer is registered on the feed, not on the option subscription, so
    the underlying's ticks land in it too. Harmless — nothing prices against an
    index — and worth pinning, because an observer that silently saw only one
    instrument would still pass every assertion above."""
    quotes = QuoteBook()
    feed = SimulatedFeed(_tape(book=True))
    feed.add_tick_observer(quotes.record)
    feed.subscribe(UNDERLYING)
    feed.subscribe(CE_CONTRACT)
    feed.run()

    assert quotes.instruments() == {UNDERLYING, CE_CONTRACT}
    assert quotes.latest(CE_CONTRACT) is not None
    assert quotes.latest(UNDERLYING).bid is None  # type: ignore[union-attr]
