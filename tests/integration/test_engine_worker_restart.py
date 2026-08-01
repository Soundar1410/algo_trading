"""**The Part 2b-ii-B-2 restart gate**: recovery with the real engine wired.

Phase 1's ``test_restart_restores_the_open_paper_position`` proved this for the
fixture strategy, whose whole recovered state is "a position is open and here is its
entry price". The engine needs more and could not get it: the ``positions`` row
carries ``instrument``, ``security_id``, ``quantity`` and ``average_price`` but not
the option type, strike, expiry or lot size, so an ``OptionContract`` could not be
rebuilt from it — and without a contract the engine cannot manage, mark, or close the
position it already holds.

Part 2b-ii-B-2 closes that with the contract record ``LifecycleGateway`` now writes
into ``strategy_state.payload`` and ``PositionManager.adopt``, which seeds the book
**without calling the gateway**. These tests drive both halves through two real,
sequential ``run_worker`` calls against one database.

The property that matters most is negative: the second run must **not** re-enter. Its
strategy fires an ENTER on the first completed candle exactly as the first run's did,
so the tape below deliberately produces that signal — a test whose second run never
signalled would prove nothing about the case that doubles exposure.
"""

from __future__ import annotations

import queue as queue_module
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from common.config.models import ExecutionMode
from common.engine.state_payload import OPEN_POSITION_KEY, read_payload
from common.execution import ExecutionRepository
from common.models import PositionStatus, Tick
from common.notifications import RecordingNotifier
from common.persistence import Database
from common.risk import SquareOffPolicy
from runtimes.intraday_options.worker import EngineWorkerConfig, WorkerConfig, run_worker

IST = ZoneInfo("Asia/Kolkata")
RUNTIME_ID = "intraday_options"
STRATEGY_ID = "engine01"
TRADING_DATE = "2026-07-16"
UNDERLYING = "INDEX"
LOT_SIZE = 65
CE_CONTRACT = "SIM:NIFTY:WEEKLY:24000:CE"
ENGINE_STRATEGY = "strategies.intraday_options.engine_fixture_strategy:EngineFixtureStrategy"


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 7, 16, hour, minute, second, tzinfo=IST)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id,
        instrument=security_id,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


#: Run one: opens a position and stops with it still open.
_FIRST_TAPE = [
    _tick(UNDERLYING, 24000.0, _ts(9, 16)),
    _tick(UNDERLYING, 24010.0, _ts(9, 21)),  # closes candle 1 -> ENTER
    _tick(CE_CONTRACT, 100.0, _ts(9, 21, 30)),  # fills the pending entry
]

#: Run two: signals an ENTER again (which must be a no-op against the adopted leg),
#: then walks the premium down so the real Part 2a policy closes it.
#: Premium buckets: 09:45-09:50 closes at 105, 09:50-09:55 at 108, 09:55-10:00 at 85
#: — and 85 < 108 is what MOMENTUM_CLOSE exits on.
_SECOND_TAPE = [
    _tick(UNDERLYING, 24000.0, _ts(9, 41)),
    _tick(UNDERLYING, 24010.0, _ts(9, 46)),  # closes a candle -> ENTER, same leg
    _tick(CE_CONTRACT, 100.0, _ts(9, 46, 30)),
    _tick(CE_CONTRACT, 105.0, _ts(9, 48)),
    _tick(CE_CONTRACT, 110.0, _ts(9, 51)),
    _tick(CE_CONTRACT, 108.0, _ts(9, 53)),
    _tick(CE_CONTRACT, 90.0, _ts(9, 56)),
    _tick(CE_CONTRACT, 85.0, _ts(9, 58)),
    _tick(CE_CONTRACT, 80.0, _ts(10, 1)),
]


@pytest.fixture
def worker_config(runtime_dirs: dict[str, Path], database_path: Path) -> WorkerConfig:
    return WorkerConfig(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        security_id=UNDERLYING,
        instrument="NIFTY",
        database_path=database_path,
        lock_dir=runtime_dirs["lock_dir"],
        pid_dir=runtime_dirs["pid_dir"],
        log_dir=runtime_dirs["log_dir"],
        trading_date=TRADING_DATE,
        execution_mode=ExecutionMode.PAPER,
        idle_timeout_seconds=0.5,
        square_off_policy=SquareOffPolicy(),
        engine=EngineWorkerConfig(
            strategy_ref=ENGINE_STRATEGY,
            strategy_kwargs={"enter_on_candle": 1, "premium_exit": True},
            timeframe="5m",
            lot_size=LOT_SIZE,
            strike_step=50,
            feed_poll_seconds=0.05,
        ),
    )


def _run(config: WorkerConfig, ticks):
    q: queue_module.Queue = queue_module.Queue()
    for tick in ticks:
        q.put(tick)
    return run_worker(config, queue_module.Queue(), RecordingNotifier(), q, None)


def _repository(database_path: Path) -> ExecutionRepository:
    return ExecutionRepository(Database(database_path))


def _open_positions(database_path: Path):
    return _repository(database_path).open_positions(
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )


def _payload(database_path: Path) -> dict:
    return read_payload(
        _repository(database_path),
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )


def _intents(database_path: Path, side: str) -> int:
    return (
        Database(database_path)
        .connect()
        .execute("SELECT COUNT(*) FROM order_intents WHERE side = ?", (side,))
        .fetchone()[0]
    )


# --------------------------------------------------------------- the gate
def test_a_restarted_engine_adopts_the_position_it_already_holds(worker_config, database_path):
    first = _run(worker_config, _FIRST_TAPE)
    assert first.exit_code == 0, first.error
    assert first.recovered_position is False
    assert first.orders_placed == 1

    before = _open_positions(database_path)
    assert len(before) == 1
    assert before[0].status is PositionStatus.OPEN

    second = _run(worker_config, _SECOND_TAPE)

    assert second.exit_code == 0, second.error
    assert second.recovered_position is True, "the second run did not adopt the open position"
    assert second.trades_closed == 1, "the adopted position was never closed"

    assert _open_positions(database_path) == []
    closed = Database(database_path).connect().execute("SELECT * FROM positions").fetchone()
    assert closed["status"] == PositionStatus.CLOSED.value
    assert closed["quantity"] == 0


def test_the_restarted_engine_does_not_re_enter(worker_config, database_path):
    """The failure this whole mechanism prevents: two entries, double the exposure."""
    _run(worker_config, _FIRST_TAPE)
    _run(worker_config, _SECOND_TAPE)

    assert _intents(database_path, "BUY") == 1, "the restarted engine opened a second position"
    assert _intents(database_path, "SELL") == 1
    assert (
        Database(database_path).connect().execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        == 1
    )


def test_the_second_run_really_did_signal_an_entry(worker_config, database_path):
    """Otherwise the test above passes for the wrong reason.

    A second run that never produced an ENTER would show one BUY intent whether or
    not the adoption worked. The strategy enters on the first completed candle, so
    the tape's underlying ticks must actually close one.
    """
    _run(worker_config, _FIRST_TAPE)
    second = _run(worker_config, _SECOND_TAPE)

    # The exit proves the engine processed premium candles for the adopted contract,
    # which only happens once _start_day has run and the strategy has been consulted.
    assert second.ticks_processed == len(_SECOND_TAPE)
    assert second.trades_closed == 1


def test_the_adopted_position_matches_what_was_persisted(worker_config, database_path):
    """Field by field, against the row the previous run left behind."""
    _run(worker_config, _FIRST_TAPE)
    before = _open_positions(database_path)[0]
    record = _payload(database_path)[OPEN_POSITION_KEY]

    _run(worker_config, _SECOND_TAPE)
    closed = Database(database_path).connect().execute("SELECT * FROM positions").fetchone()

    # The contract identity came from the payload...
    assert record["security_id"] == before.security_id == CE_CONTRACT
    assert record["lot_size"] == LOT_SIZE
    assert record["lots"] == abs(before.quantity) // LOT_SIZE
    # ...and the price and size from the authoritative positions row, which the close
    # netted to zero against.
    assert closed["average_price"] == pytest.approx(before.average_price)
    assert closed["entry_correlation_id"] == before.entry_correlation_id


def test_the_entry_charges_survive_the_restart(worker_config, database_path):
    """The round trip must not look cheaper for having been interrupted."""
    _run(worker_config, _FIRST_TAPE)
    entry_charges = _open_positions(database_path)[0].charges
    assert entry_charges > 0, "the fixture must actually charge something to mean anything"

    _run(worker_config, _SECOND_TAPE)

    closed = Database(database_path).connect().execute("SELECT * FROM positions").fetchone()
    # The position row accumulates both legs; the point is that the entry leg's
    # charge is still counted after the process that paid it died.
    assert closed["charges"] > entry_charges


def test_the_contract_record_is_cleared_once_the_adopted_position_closes(
    worker_config, database_path
):
    _run(worker_config, _FIRST_TAPE)
    assert OPEN_POSITION_KEY in _payload(database_path)

    _run(worker_config, _SECOND_TAPE)

    assert OPEN_POSITION_KEY not in _payload(database_path)


def test_the_previous_incomplete_session_is_closed_on_recovery(worker_config, database_path):
    _run(worker_config, _FIRST_TAPE)
    _run(worker_config, _SECOND_TAPE)

    unclosed = (
        Database(database_path)
        .connect()
        .execute("SELECT COUNT(*) FROM runtime_sessions WHERE ended_at IS NULL")
        .fetchone()[0]
    )
    assert unclosed == 0


# ------------------------------------------------------ failing closed
def test_an_open_position_with_no_contract_record_blocks_entries_rather_than_trading(
    worker_config, database_path
):
    """The inconsistency that must never become a second position.

    An open position whose contract cannot be rebuilt is worse than none at all: the
    engine would believe it was flat. Recovery raises, the engine latches entries off
    for the day, and the run still ends cleanly — so the outcome is "manage nothing
    new", never "trade alongside something unknown".
    """
    _run(worker_config, _FIRST_TAPE)
    repository = _repository(database_path)
    # Exactly what a state file written by an older build would look like.
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE strategy_state SET payload = '{}' WHERE strategy_id = ?", (STRATEGY_ID,)
        )

    second = _run(worker_config, _SECOND_TAPE)

    assert second.exit_code == 0, "a recoverable inconsistency must not crash the worker"
    assert second.recovered_position is False
    assert _intents(database_path, "BUY") == 1, "it entered again despite the open position"

    error = (
        Database(database_path)
        .connect()
        .execute("SELECT severity, component, message FROM errors ORDER BY id DESC LIMIT 1")
        .fetchone()
    )
    assert error["severity"] == "CRITICAL"
    assert error["component"] == "engine.recovery"
    assert "OPEN" in error["message"]


def test_a_stale_contract_record_for_another_instrument_is_refused(worker_config, database_path):
    _run(worker_config, _FIRST_TAPE)
    repository = _repository(database_path)
    payload = _payload(database_path)
    payload[OPEN_POSITION_KEY]["security_id"] = "SIM:NIFTY:WEEKLY:24500:PE"
    with repository.database.transaction() as conn:
        import json

        conn.execute(
            "UPDATE strategy_state SET payload = ? WHERE strategy_id = ?",
            (json.dumps(payload), STRATEGY_ID),
        )

    second = _run(worker_config, _SECOND_TAPE)

    assert second.recovered_position is False
    assert _intents(database_path, "BUY") == 1


def test_recovery_does_not_reach_across_trading_dates(worker_config, database_path):
    """A restart on Tuesday must not adopt Monday's position (spec section 12)."""
    _run(worker_config, _FIRST_TAPE)

    worker_config.trading_date = "2026-07-17"
    second = _run(worker_config, _SECOND_TAPE)

    assert second.recovered_position is False
