"""Child process for the engine worker's signal test. Not a test module itself.

Run as ``python tests/end_to_end/engine_worker_signal_child.py <work-dir>``. It runs
a real :func:`~runtimes.intraday_options.worker.run_worker` driving the **ported
engine**, opens a real position through the real paper broker into real SQLite, and
then waits to be signalled.

This is the Part 2b-ii-B-2 acceptance gate that could not be written before: every
earlier square-off test either ran in-process, where a "signal" is a method call, or
signalled the *supervisor*, which reaches a worker through a queue sentinel rather
than through a handler. Neither exercises what a process manager actually does at
15:20, which is deliver ``SIGTERM`` to the worker itself.

It reports through stdout, because the point is what a *separate process* does:

``READY``
    a position is genuinely open in the database, so it is worth signalling. Waited
    for rather than slept on — signalling before the engine has anything to close
    would let a broken square-off pass.
``RESULT <json>``
    what the shutdown did, including whether anything was left open.

The tick stream is produced by a thread here rather than by a hub. The hub → queue →
child hop has its own coverage (``tests/end_to_end/test_supervisor.py``); what is
under test here is the signal path, and a live-shaped feed that keeps delivering is
what that needs — a silent one would prove only the idle timeout.
"""

from __future__ import annotations

import argparse
import json
import queue as queue_module
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from common.config.models import ExecutionMode
from common.models import Tick
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

#: Real seconds between ticks. Fast enough that the shutdown is prompt, slow enough
#: that the loop waits rather than spins.
TICK_INTERVAL = 0.005
#: Exchange-time step per tick. Advances so premium candles actually close; the price
#: stays flat so no exit policy fires and the position is still open when the signal
#: lands.
EXCHANGE_STEP = timedelta(seconds=1)

SESSION_START = datetime(2026, 7, 16, 9, 16, tzinfo=IST)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id,
        instrument=security_id,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


def _produce(tick_queue: queue_module.Queue, stop: threading.Event) -> None:
    """A live-shaped stream: opens a position, then keeps the feed alive flat."""
    # Two underlying ticks close the first five-minute bar, which is what makes the
    # fixture strategy signal an entry and the engine pick its contract.
    tick_queue.put(_tick(UNDERLYING, 24000.0, SESSION_START))
    tick_queue.put(_tick(UNDERLYING, 24010.0, SESSION_START + timedelta(minutes=5)))
    # Fills the pending entry at the first fresh premium tick.
    tick_queue.put(_tick(CE_CONTRACT, 100.0, SESSION_START + timedelta(minutes=5, seconds=30)))

    moment = SESSION_START + timedelta(minutes=5, seconds=31)
    while not stop.is_set():
        # Flat premium: every completed bar closes at the same price, so
        # MOMENTUM_CLOSE never fires and only the square-off can end this position.
        tick_queue.put(_tick(CE_CONTRACT, 100.0, moment))
        moment += EXCHANGE_STEP
        time.sleep(TICK_INTERVAL)


def _announce_when_open(database_path: Path, stop: threading.Event) -> None:
    """Print READY once the engine genuinely holds a position.

    Its own connection: SQLite objects belong to the thread that made them, and the
    worker's belongs to the main thread.
    """
    deadline = time.monotonic() + 60.0
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            connection = sqlite3.connect(str(database_path), timeout=1.0)
            try:
                row = connection.execute(
                    "SELECT COUNT(*) FROM positions WHERE status = 'OPEN' AND quantity != 0"
                ).fetchone()
            finally:
                connection.close()
            if row and row[0]:
                print("READY", flush=True)
                return
        except sqlite3.Error:
            pass  # the file or the schema may not exist yet
        time.sleep(0.02)


def main(work_dir: Path) -> int:
    lock_dir = work_dir / "locks"
    pid_dir = work_dir / "pid"
    log_dir = work_dir / "logs"
    operational = work_dir / "operational"
    for directory in (lock_dir, pid_dir, log_dir, operational):
        directory.mkdir(parents=True, exist_ok=True)
    database_path = operational / "intraday_options.db"

    config = WorkerConfig(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        security_id=UNDERLYING,
        instrument="NIFTY",
        database_path=database_path,
        lock_dir=lock_dir,
        pid_dir=pid_dir,
        log_dir=log_dir,
        trading_date=TRADING_DATE,
        execution_mode=ExecutionMode.PAPER,
        # Generous: this run must end because it was signalled, not because it got
        # bored. A run that idled out would report a clean shutdown having never
        # tested the handler at all.
        idle_timeout_seconds=30.0,
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

    tick_queue: queue_module.Queue = queue_module.Queue()
    stop = threading.Event()
    producer = threading.Thread(target=_produce, args=(tick_queue, stop), daemon=True)
    announcer = threading.Thread(
        target=_announce_when_open, args=(database_path, stop), daemon=True
    )
    producer.start()
    announcer.start()

    started = time.monotonic()
    outcome = run_worker(config, queue_module.Queue(), None, tick_queue, None)
    stop.set()

    # Read the final state back on a fresh connection: the worker has closed its own.
    connection = sqlite3.connect(str(database_path), timeout=5.0)
    try:
        still_open = connection.execute(
            "SELECT COUNT(*) FROM positions WHERE status = 'OPEN' AND quantity != 0"
        ).fetchone()[0]
        square_off_state = connection.execute(
            "SELECT square_off_state FROM strategy_state WHERE strategy_id = ?",
            (STRATEGY_ID,),
        ).fetchone()
        last_health = connection.execute(
            """
            SELECT health_state FROM runtime_heartbeats
            WHERE strategy_id = ? ORDER BY id DESC LIMIT 1
            """,
            (STRATEGY_ID,),
        ).fetchone()
    finally:
        connection.close()

    print(
        "RESULT "
        + json.dumps(
            {
                "exit_code": outcome.exit_code,
                "error": outcome.error,
                "stopped_by_request": outcome.stopped_by_request,
                "square_off_completed": outcome.square_off_completed,
                "clean_engine_shutdown": outcome.clean_engine_shutdown,
                "trades_closed": outcome.trades_closed,
                "orders_placed": outcome.orders_placed,
                "ticks_processed": outcome.ticks_processed,
                "positions_still_open": still_open,
                "square_off_state": square_off_state[0] if square_off_state else None,
                "last_health_state": last_health[0] if last_health else None,
                "shutdown_seconds": round(time.monotonic() - started, 3),
            }
        ),
        flush=True,
    )
    return outcome.exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("work_dir", type=Path)
    parsed = parser.parse_args()
    sys.exit(main(parsed.work_dir))
