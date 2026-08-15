"""Basket/leg restart recovery, driven through two real ``run_worker`` calls.

Mirrors ``tests/integration/test_engine_worker_restart.py``'s structure and
its central property: the second run must **not** duplicate an entry. Proves
the multi-leg engine's own restart path — ``recover_basket`` /
``MultiLegEngine._adopt_recovered_basket`` — against a real SQLite database,
the real ``strategy_baskets``/``strategy_legs`` tables (migration ``0009``),
and the real ``straddle_920`` strategy.
"""

from __future__ import annotations

import queue as queue_module
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from common.config.models import ExecutionMode
from common.execution import ExecutionRepository
from common.models import Tick
from common.notifications import RecordingNotifier
from common.persistence import Database
from common.risk import SquareOffPolicy
from runtimes.intraday_options.worker import (
    MultiLegEngineWorkerConfig,
    WorkerConfig,
    run_worker,
)

IST = ZoneInfo("Asia/Kolkata")
RUNTIME_ID = "intraday_options"
STRATEGY_ID = "straddle_920"
TRADING_DATE = "2026-08-17"  # a Monday
NIFTY = "NIFTY_IDX"
CE1 = "SIM:NIFTY:WEEKLY:24000:CE"
PE1 = "SIM:NIFTY:WEEKLY:24000:PE"
STRATEGY_REF = "strategies.intraday_options.straddle_920.strategy:Straddle920Strategy"


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 17, hour, minute, second, tzinfo=IST)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id,
        instrument=security_id,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


#: Run one: both legs fill, then the tape ends (idle timeout) with the basket
#: still open — models a worker that died mid-session.
_FIRST_TAPE = [
    _tick(NIFTY, 24000.0, _ts(9, 16)),
    _tick(NIFTY, 24000.0, _ts(9, 21)),  # closes the entry candle -> ENTER_BASKET
    _tick(CE1, 100.0, _ts(9, 21, 5)),
    _tick(PE1, 95.0, _ts(9, 21, 10)),
]

#: Run two: a fresh candle sequence that must NOT re-enter (the day's one
#: attempt is already consumed), then a tick past 15:15 forces square-off.
_SECOND_TAPE = [
    _tick(NIFTY, 24000.0, _ts(9, 41)),
    _tick(NIFTY, 24000.0, _ts(9, 46)),
    _tick(NIFTY, 24000.0, _ts(15, 16)),
]


def _worker_config(runtime_dirs: dict[str, Path], database_path: Path) -> WorkerConfig:
    return WorkerConfig(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        security_id=NIFTY,
        instrument="NIFTY",
        database_path=database_path,
        lock_dir=runtime_dirs["lock_dir"],
        pid_dir=runtime_dirs["pid_dir"],
        log_dir=runtime_dirs["log_dir"],
        trading_date=TRADING_DATE,
        execution_mode=ExecutionMode.PAPER,
        idle_timeout_seconds=0.3,
        square_off_policy=SquareOffPolicy(),
        multi_leg_engine=MultiLegEngineWorkerConfig(
            strategy_ref=STRATEGY_REF,
            strategy_kwargs={
                "lots_per_leg": 10,
                "entry_evaluation_time": "09:20",
                "last_entry_time": "15:00",
                "vix_threshold": 20.0,
            },
            timeframe="5m",
            lot_size=75,
            strike_step=50,
            feed_poll_seconds=0.05,
        ),
    )


def _run(config: WorkerConfig, ticks: list[Tick]):
    q: queue_module.Queue = queue_module.Queue()
    for tick in ticks:
        q.put(tick)
    return run_worker(config, queue_module.Queue(), RecordingNotifier(), q, None)


def _repository(database_path: Path) -> ExecutionRepository:
    return ExecutionRepository(Database(database_path))


def _open_positions(database_path: Path):
    return _repository(database_path).open_positions(
        strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE
    )


def _basket_row(database_path: Path):
    return _repository(database_path).load_strategy_basket(
        strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE
    )


def _leg_rows(database_path: Path):
    return _repository(database_path).load_strategy_legs(
        strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE
    )


def _intents(database_path: Path, side: str) -> int:
    return (
        Database(database_path)
        .connect()
        .execute("SELECT COUNT(*) FROM order_intents WHERE side = ?", (side,))
        .fetchone()[0]
    )


# --------------------------------------------------------------- the gate
def test_a_restarted_worker_adopts_the_open_basket(runtime_dirs, database_path):
    config = _worker_config(runtime_dirs, database_path)

    first = _run(config, _FIRST_TAPE)
    assert first.exit_code == 0, first.error
    assert first.orders_placed == 2

    before = _open_positions(database_path)
    assert len(before) == 2

    basket = _basket_row(database_path)
    assert basket is not None
    assert bool(basket["entries_consumed"]) is True

    second = _run(config, _SECOND_TAPE)
    assert second.exit_code == 0, second.error
    assert second.trades_closed == 2, "the adopted basket's legs were never squared off"

    assert _open_positions(database_path) == []
    legs = _leg_rows(database_path)
    assert len(legs) == 2
    assert all(leg["state"] == "CLOSED" for leg in legs)
    assert all(leg["exit_reason"] == "SQUARE_OFF" for leg in legs)


def test_the_restarted_worker_does_not_duplicate_the_entry(runtime_dirs, database_path):
    """The failure this whole mechanism prevents: a second SELL of each leg,
    doubling exposure the restart exists to prevent."""
    config = _worker_config(runtime_dirs, database_path)
    _run(config, _FIRST_TAPE)
    _run(config, _SECOND_TAPE)

    # Two original SELLs (CE+PE) at entry, then two BUY-to-close at square-off.
    assert _intents(database_path, "SELL") == 2
    assert _intents(database_path, "BUY") == 2
    assert (
        Database(database_path).connect().execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        == 2
    )
