"""The ported engine, executing through the audited persistence path.

**The Phase 3 Part 2b-ii-B-1 gate.** Part 2b-i shipped the ``ExecutionGateway``
seam with ``InMemoryGateway`` behind it, which is why every engine test until now
proved things about an in-memory :class:`~common.engine.models.OpenPosition` and
nothing about a persisted :class:`~common.models.Position`. Structurally the
engine could not reach the database at all.

:class:`~common.engine.gateway.LifecycleGateway` closes that. Every open and every
close goes through the **existing**
:meth:`~common.execution.lifecycle.OrderLifecycle.handle_signal`, so nothing
bypasses ``record_signal`` → ``reserve_intent`` → ``broker.submit`` →
``record_submission`` → ``apply_fill``, and every row carries a correlation ID and
an ``execution_mode``.

Nothing here is faked. Real SQLite behind real migrations, a real
``ExecutionRepository``, a real ``PaperBroker``, a real ``OrderLifecycle``, a real
``TradingEngine`` over a real ``SimulatedFeed``, and the exit decision made by the
**real** Part 2a ``MOMENTUM_CLOSE`` policy in premium mode. The only double is
``EngineFixtureStrategy``, which supplies entry timing — real strategies are
Phase 9.

Two hazards were identified during planning rather than discovered here, and each
has its own section below: the ``signals`` UNIQUE constraint against a
tick-driven engine, and the rule that a call which did not trade must **raise**
rather than fabricate a :class:`~common.engine.positions.FillOutcome`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.broker import PaperBroker, PaperFillConfig
from common.config.models import ExecutionMode
from common.engine.config import EngineConfig, SessionConfig
from common.engine.engine import TradingEngine
from common.engine.feed import SimulatedFeed
from common.engine.gateway import GatewayExecutionError, LifecycleGateway
from common.engine.models import ExitReason, OptionContract, OptionType, OrderSide
from common.engine.positions import PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.execution import ExecutionRepository, OrderLifecycle
from common.models import PositionStatus, Tick
from common.persistence import Database, MigrationRunner
from strategies.intraday_options.engine_fixture_strategy import EngineFixtureStrategy

IST = ZoneInfo("Asia/Kolkata")
UNDERLYING = "INDEX"
LOT_SIZE = 65
CE_CONTRACT = "SIM:NIFTY:WEEKLY:24000:CE"
RUNTIME_ID = "intraday_options"
STRATEGY_ID = "engine01"
TRADING_DATE = "2026-07-16"


def _ts(hour: int, minute: int, second: int = 0, micro: int = 0) -> datetime:
    return datetime(2026, 7, 16, hour, minute, second, micro, tzinfo=IST)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id,
        instrument=security_id,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


def _contract(security_id: str = CE_CONTRACT, symbol: str = "NIFTY 24000 CE") -> OptionContract:
    return OptionContract(
        symbol=symbol,
        security_id=security_id,
        strike=24000.0,
        option_type=OptionType.CE,
        expiry="2026-07-23",
        lot_size=LOT_SIZE,
    )


# Two underlying ticks: the second closes candle #1 and triggers the ENTER.
_ENTRY_TAPE = [
    _tick(UNDERLYING, 24000.0, _ts(9, 16)),
    _tick(UNDERLYING, 24010.0, _ts(9, 21)),
]

# Fills the entry at 100, walks the premium up (105, 108) and then down (85), so
# exactly one adverse *closed* bar exists and the real policy fires on it.
_PREMIUM_WALK = [
    (_ts(9, 21, 30), 100.0),
    (_ts(9, 23), 105.0),
    (_ts(9, 26), 110.0),
    (_ts(9, 28), 108.0),
    (_ts(9, 31), 90.0),
    (_ts(9, 33), 85.0),
    (_ts(9, 36), 80.0),
]


@pytest.fixture
def repository(database_path):
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    yield ExecutionRepository(database)
    database.close()


@pytest.fixture
def lifecycle(repository: ExecutionRepository) -> OrderLifecycle:
    session = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=4242,
    )
    return OrderLifecycle(
        repository=repository,
        broker=PaperBroker(config=PaperFillConfig(slippage_points=0.0)),
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        session_id=session.id,
    )


@pytest.fixture
def gateway(lifecycle: OrderLifecycle) -> LifecycleGateway:
    return LifecycleGateway(
        lifecycle,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )


def _build_engine(
    gateway: LifecycleGateway, ticks: Sequence[Tick]
) -> tuple[TradingEngine, PositionManager]:
    positions = PositionManager(gateway, lots=1)
    engine = TradingEngine(
        EngineConfig(
            timeframe="5m",
            session=SessionConfig(
                timezone="Asia/Kolkata",
                start_time="09:15",
                end_time="15:15",
                square_off_time="15:20",
            ),
        ),
        feed=SimulatedFeed(list(ticks)),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=LOT_SIZE), strike_step=50
        ),
        strategy=EngineFixtureStrategy(enter_on_candle=1, premium_exit=True),
        position_manager=positions,
        underlying_security_id=UNDERLYING,
    )
    return engine, positions


@pytest.fixture
def round_trip(gateway: LifecycleGateway, repository: ExecutionRepository):
    """One full open → premium walk → policy exit, driven by the real engine."""
    ticks = [*_ENTRY_TAPE, *(_tick(CE_CONTRACT, p, ts) for ts, p in _PREMIUM_WALK)]
    engine, positions = _build_engine(gateway, ticks)
    engine.run()
    return engine, positions, repository


def _rows(repository: ExecutionRepository, sql: str, *args) -> list[sqlite3.Row]:
    return list(repository.database.connect().execute(sql, args).fetchall())


# ----------------------------------------------------------------- the gate


def test_the_engine_opens_and_closes_through_the_persisted_lifecycle(round_trip) -> None:
    """One round trip, and the exit decided by the real Part 2a policy."""
    _engine, positions, _repository = round_trip
    assert len(positions.trades) == 1
    trade = positions.trades[0]
    assert trade.contract.security_id == CE_CONTRACT
    assert trade.side is OrderSide.BUY
    assert trade.exit_reason is ExitReason.MOMENTUM_LOW
    assert trade.entry_price == 100.0
    assert trade.exit_price == 80.0


def test_the_position_reconciles_between_the_engine_and_the_database(round_trip) -> None:
    """**The item Part 2b-i and 2b-ii-A both had to defer.**

    The engine's in-memory view and the persisted row are two accounts of the same
    trade, and this is the assertion that they agree.
    """
    _engine, positions, repository = round_trip
    trade = positions.trades[0]

    rows = _rows(repository, "SELECT * FROM positions")
    assert len(rows) == 1, "one contract traded once must be one position row"
    row = rows[0]
    assert row["status"] == PositionStatus.CLOSED.value
    assert row["quantity"] == 0
    assert row["security_id"] == CE_CONTRACT
    assert row["instrument"] == trade.contract.symbol
    assert row["average_price"] == pytest.approx(trade.entry_price)
    assert row["realised_pnl"] == pytest.approx(trade.gross_pnl)
    assert row["entry_correlation_id"] is not None


def test_both_legs_are_persisted_end_to_end(round_trip) -> None:
    _engine, _positions, repository = round_trip
    for table in ("signals", "order_intents", "orders", "fills"):
        rows = _rows(repository, f"SELECT * FROM {table}")
        assert len(rows) == 2, f"{table} must hold the entry and the exit"


def test_the_engines_prices_and_charges_are_the_persisted_ones(round_trip) -> None:
    """No second opinion on the numbers.

    ``FillOutcome`` is built *from* the persisted fill rows, so a divergence
    between what the engine reports and what the database holds is impossible by
    construction rather than by convention.
    """
    _engine, positions, repository = round_trip
    trade = positions.trades[0]
    fills = _rows(repository, "SELECT * FROM fills ORDER BY id")
    assert [f["price"] for f in fills] == [trade.entry_price, trade.exit_price]
    assert sum(f["charges"] for f in fills) == pytest.approx(trade.charges)
    assert all(f["quantity"] == LOT_SIZE for f in fills)


def test_every_persisted_row_is_paper_namespaced(round_trip) -> None:
    _engine, _positions, repository = round_trip
    for table in ("signals", "order_intents", "orders", "fills", "positions"):
        rows = _rows(repository, f"SELECT * FROM {table}")
        assert {r["execution_mode"] for r in rows} == {ExecutionMode.PAPER.value}
    correlation_ids = [r["correlation_id"] for r in _rows(repository, "SELECT * FROM orders")]
    assert all(cid.startswith("p_") for cid in correlation_ids)
    assert len(set(correlation_ids)) == 2, "each leg gets its own correlation ID"


def test_the_database_stays_consistent(round_trip) -> None:
    _engine, _positions, repository = round_trip
    assert repository.database.integrity_check() == []
    assert repository.database.foreign_key_check() == []


def test_the_signal_rows_record_the_side_the_engine_took(round_trip) -> None:
    _engine, _positions, repository = round_trip
    sides = [r["side"] for r in _rows(repository, "SELECT * FROM signals ORDER BY id")]
    assert sides == ["BUY", "SELL"], "a long is opened with a buy and closed with a sell"


# ----------------------------------- hazard (a): the signals UNIQUE constraint


def test_two_legs_on_one_contract_in_one_second_both_persist(
    gateway: LifecycleGateway, repository: ExecutionRepository
) -> None:
    """The hazard, at full strength.

    ``signals`` carries ``UNIQUE (strategy_id, execution_mode, instrument,
    candle_end_at)``, the engine is not candle-driven, and Dhan's exchange
    timestamp is second-resolution. An exit and a re-entry on one contract inside
    one second would collide — and ``record_signal`` turns a collision into
    ``None``, which ``handle_signal`` turns into a silent skip.

    Silent is the problem. A suppressed *close* leaves a position open while the
    engine believes it closed.
    """
    contract = _contract()
    at = _ts(9, 30)
    gateway.buy(contract, 1, ref_price=100.0, ts=at)
    gateway.sell(contract, 1, ref_price=101.0, ts=at)
    gateway.buy(contract, 1, ref_price=101.5, ts=at)

    signals = _rows(repository, "SELECT * FROM signals ORDER BY id")
    assert len(signals) == 3, "three executions must be three signal rows, not one"
    ends = [s["candle_end_at"] for s in signals]
    assert len(set(ends)) == 3
    assert ends == sorted(ends), "the disambiguator must be monotonic, not random"


def test_the_recorded_window_stays_truthful_to_the_microsecond(
    gateway: LifecycleGateway, repository: ExecutionRepository
) -> None:
    """The bump is 1µs, not a fabricated bar.

    The engine never evaluated a candle here, so the persisted window is a
    one-second slice ending at the execution decision. Inventing a 5-minute bar it
    never saw would be worse than admitting that.
    """
    contract = _contract()
    at = _ts(9, 30)
    gateway.buy(contract, 1, ref_price=100.0, ts=at)
    gateway.sell(contract, 1, ref_price=101.0, ts=at)

    signals = _rows(repository, "SELECT * FROM signals ORDER BY id")
    ends = [datetime.fromisoformat(s["candle_end_at"]) for s in signals]
    starts = [datetime.fromisoformat(s["candle_start_at"]) for s in signals]
    assert ends[0] == at
    assert ends[1] == at + timedelta(microseconds=1)
    assert [e - s for e, s in zip(ends, starts, strict=True)] == [timedelta(seconds=1)] * 2


def test_a_later_timestamp_is_used_as_is(
    gateway: LifecycleGateway, repository: ExecutionRepository
) -> None:
    """The bump only ever applies to a collision — it must not drift the clock."""
    contract = _contract()
    gateway.buy(contract, 1, ref_price=100.0, ts=_ts(9, 30))
    gateway.sell(contract, 1, ref_price=101.0, ts=_ts(9, 45))
    ends = [
        datetime.fromisoformat(s["candle_end_at"])
        for s in _rows(repository, "SELECT * FROM signals ORDER BY id")
    ]
    assert ends == [_ts(9, 30), _ts(9, 45)]


def test_the_disambiguator_is_per_instrument(
    gateway: LifecycleGateway, repository: ExecutionRepository
) -> None:
    """The constraint keys on ``instrument``, so two contracts at the same instant
    do not collide and must not be pushed apart."""
    at = _ts(9, 30)
    gateway.buy(_contract(CE_CONTRACT, "NIFTY 24000 CE"), 1, ref_price=100.0, ts=at)
    gateway.buy(_contract("SIM:NIFTY:WEEKLY:24000:PE", "NIFTY 24000 PE"), 1, ref_price=90.0, ts=at)
    ends = [s["candle_end_at"] for s in _rows(repository, "SELECT * FROM signals ORDER BY id")]
    assert ends == [at.isoformat(), at.isoformat()]


def test_an_out_of_order_timestamp_still_produces_a_distinct_row(
    gateway: LifecycleGateway, repository: ExecutionRepository
) -> None:
    """A tick arriving with an earlier exchange time must not silently vanish."""
    contract = _contract()
    gateway.buy(contract, 1, ref_price=100.0, ts=_ts(9, 30))
    gateway.sell(contract, 1, ref_price=101.0, ts=_ts(9, 29))
    assert len(_rows(repository, "SELECT * FROM signals")) == 2


# ---------------------------------- hazard (b): a call that did not trade raises


class _SuppressingRepository:
    """Delegates everything, but refuses to record a signal.

    Stands in for the *class* of failure, not just the duplicate: ``record_signal``
    catches ``sqlite3.IntegrityError`` broadly, so a foreign-key failure on
    ``session_id`` or a CHECK violation also returns ``None`` and is reported
    upstream as "duplicate signal for this candle".
    """

    def __init__(self, inner: ExecutionRepository) -> None:
        self._inner = inner

    def record_signal(self, **_kwargs) -> None:
        return None

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def test_a_suppressed_signal_raises_instead_of_fabricating_a_fill(
    repository: ExecutionRepository,
) -> None:
    """The rule that makes the hazard survivable.

    A loud failure leaves the position open in the database, where restart
    recovery adopts it — recoverable. A fabricated ``FillOutcome`` reports a close
    that never happened, and nothing downstream can tell.
    """
    session = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=4242,
    )
    lifecycle = OrderLifecycle(
        repository=_SuppressingRepository(repository),  # type: ignore[arg-type]
        broker=PaperBroker(config=PaperFillConfig(slippage_points=0.0)),
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        session_id=session.id,
    )
    gateway = LifecycleGateway(
        lifecycle,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )
    with pytest.raises(GatewayExecutionError, match="did not trade"):
        gateway.buy(_contract(), 1, ref_price=100.0, ts=_ts(9, 30))


def test_a_suppressed_close_leaves_the_position_open_rather_than_phantom_closed(
    repository: ExecutionRepository, gateway: LifecycleGateway
) -> None:
    """The consequence, asserted rather than argued.

    The open goes through normally; the close is then suppressed. The database
    must still show an OPEN position — that is what makes it recoverable.
    """
    contract = _contract()
    gateway.buy(contract, 1, ref_price=100.0, ts=_ts(9, 30))
    gateway._lifecycle._repo = _SuppressingRepository(repository)  # type: ignore[attr-defined]

    with pytest.raises(GatewayExecutionError):
        gateway.sell(contract, 1, ref_price=110.0, ts=_ts(9, 31))

    open_positions = repository.open_positions(
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )
    assert len(open_positions) == 1
    assert open_positions[0].status is PositionStatus.OPEN
    assert open_positions[0].quantity == LOT_SIZE


def test_a_broker_rejection_raises_rather_than_returning_a_fill(
    repository: ExecutionRepository,
) -> None:
    """``ExecutionResult.traded`` is ``True`` for a *rejected* order — the order
    object exists, it just holds no fill. Checking ``traded`` would have been the
    obvious mistake, so the gateway checks the fills."""
    session = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=4242,
    )
    broker = PaperBroker(config=PaperFillConfig(slippage_points=0.0))
    lifecycle = OrderLifecycle(
        repository=repository,
        broker=broker,
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        session_id=session.id,
    )
    gateway = LifecycleGateway(
        lifecycle,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )

    class _Rejecting:
        name = "rejecting"

        def submit(self, *_args, **_kwargs):
            from common.broker import BrokerError

            raise BrokerError("exchange said no")

        def is_healthy(self) -> bool:
            return True

    lifecycle._broker = _Rejecting()  # type: ignore[assignment]
    with pytest.raises(GatewayExecutionError, match="did not trade"):
        gateway.buy(_contract(), 1, ref_price=100.0, ts=_ts(9, 30))

    assert _rows(repository, "SELECT * FROM orders")[0]["status"] == "REJECTED", (
        "the rejection is still audited, it just is not reported as a fill"
    )


def test_the_failure_reaches_the_engine_rather_than_being_swallowed(
    repository: ExecutionRepository, gateway: LifecycleGateway
) -> None:
    """``PositionManager`` does not catch, and the engine squares off and re-raises
    on an unhandled exception — the safe direction for an execution failure."""
    positions = PositionManager(gateway, lots=1)
    gateway._lifecycle._repo = _SuppressingRepository(repository)  # type: ignore[attr-defined]
    with pytest.raises(GatewayExecutionError):
        positions.open(_contract(), OrderSide.BUY, 100.0, _ts(9, 30))
    assert positions.positions == [], "a failed open must not leave a phantom position"


# ------------------------------------------------------------- shape and reuse


def test_the_gateway_satisfies_the_execution_gateway_protocol(
    gateway: LifecycleGateway,
) -> None:
    from common.engine.positions import ExecutionGateway

    assert isinstance(gateway, ExecutionGateway)


def test_quantity_is_lots_times_lot_size(
    gateway: LifecycleGateway, repository: ExecutionRepository
) -> None:
    gateway.buy(_contract(), 3, ref_price=100.0, ts=_ts(9, 30))
    assert _rows(repository, "SELECT * FROM order_intents")[0]["quantity"] == 3 * LOT_SIZE


def test_a_short_is_opened_with_a_sell_and_closed_with_a_buy(
    gateway: LifecycleGateway, repository: ExecutionRepository
) -> None:
    """The gateway's verbs are directional, not open/close — the repository's own
    netting decides which is which, so the gateway needs no notion of it."""
    positions = PositionManager(gateway, lots=1)
    contract = _contract()
    positions.open(contract, OrderSide.SELL, 100.0, _ts(9, 30))
    positions.close(contract.security_id, 90.0, _ts(9, 40), ExitReason.STRATEGY_EXIT)

    sides = [r["side"] for r in _rows(repository, "SELECT * FROM signals ORDER BY id")]
    assert sides == ["SELL", "BUY"]
    row = _rows(repository, "SELECT * FROM positions")[0]
    assert row["status"] == PositionStatus.CLOSED.value
    assert row["quantity"] == 0


def test_the_reason_recorded_names_the_engine_as_the_source(
    gateway: LifecycleGateway, repository: ExecutionRepository
) -> None:
    """An auditor reading ``signals`` must be able to tell an engine-originated
    row from a candle-driven one, since the candle columns mean something
    different for each."""
    gateway.buy(_contract(), 1, ref_price=100.0, ts=_ts(9, 30))
    assert "engine" in _rows(repository, "SELECT * FROM signals")[0]["reason"].lower()


def test_the_session_square_off_time_is_not_consulted_by_the_gateway(
    gateway: LifecycleGateway, repository: ExecutionRepository
) -> None:
    """A square-off close at 15:20 must persist like any other close — the gateway
    holds no clock of its own."""
    contract = _contract()
    gateway.buy(contract, 1, ref_price=100.0, ts=_ts(9, 30))
    outcome = gateway.sell(contract, 1, ref_price=95.0, ts=_ts(15, 20))
    assert outcome.fill_price == 95.0
    assert len(_rows(repository, "SELECT * FROM fills")) == 2
