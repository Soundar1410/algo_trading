"""A feed that dies before the square-off bar still squares off (Part 3, limitation 7).

The acceptance gate's central claim, on **real threads with a real database**.
`tests/unit/test_wall_clock_square_off.py` covers the decision in isolation; this
covers the consequence.

Getting the scenario right took two attempts, and both failures are worth stating
because they are the same trap:

1. With a fixed ``09:16``-style tape and a square-off time already in the wall
   clock's past, that time is also in the *tape's* past — so the **candle** clock
   fires on the first tick and the net proves nothing.
2. With the square-off time a minute ago, the net fires on the very first poll,
   **before the tape is read at all**. No position ever opens, so "no position is
   left open" passes vacuously.

So the timeline has to be: ticks in the recent past open a position, and the
square-off time falls a few seconds into the **future** — beyond every tick, so
the candle clock can never reach it, but close enough that the wall clock crosses
it while the run idles on an exhausted queue. That is exactly the situation
limitation 7 describes, and nothing simpler reproduces it.
"""

from __future__ import annotations

import queue as queue_module
import sqlite3
import time as time_module
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from common.candles.aggregator import floor_to_interval
from common.config.models import ExecutionMode
from common.models import Tick
from common.notifications import RecordingNotifier
from common.risk import SquareOffPolicy
from common.utils.timeutils import now_ist
from runtimes.intraday_options.worker import EngineWorkerConfig, WorkerConfig, run_worker

ENGINE_STRATEGY = "strategies.intraday_options.engine_fixture_strategy:EngineFixtureStrategy"
UNDERLYING = "INDEX"
LOT_SIZE = 65
CE_CONTRACT = "SIM:NIFTY:WEEKLY:24000:CE"

#: How long after the run starts the wall clock crosses the square-off time. The
#: queue is pre-loaded, so the position opens in milliseconds; this only has to be
#: long enough to be unambiguous on a loaded machine.
GRACE_SECONDS = 4
#: Far above ``GRACE_SECONDS`` so an idle timeout can never be what ended a run.
#: If the net fails to fire these tests wait and then fail, rather than passing.
IDLE_TIMEOUT = 30.0
#: For the negative controls, where the idle timeout *is* the intended ending and
#: a long one only makes the suite slow.
SHORT_IDLE = 2.0


def _last_weekday(moment: datetime) -> datetime:
    """``moment`` itself on a weekday, else the same clock time on the last one."""
    while moment.weekday() >= 5:
        moment -= timedelta(days=1)
    return moment


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id,
        instrument=security_id,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


def _tape(anchor: datetime) -> list[Tick]:
    """Opens a position, then stops. Every tick is in the recent past.

    Offsets are measured from the anchor **floored to a bucket boundary**, not
    from the anchor itself. Two reasons, both found by this test failing:

    * two ticks a fixed number of minutes apart can land in the *same* 5-minute
      bucket depending on where the anchor happens to fall, in which case no bar
      ever closes and no position opens; and
    * if they straddle an empty bucket, Part 3's own gap detection marks the bar
      ``spans_gap`` and the engine correctly declines to signal on it.

    Flooring first makes the two ticks exactly one interval apart every time: one
    rollover, no empty bucket.

    The tape is also moved onto the most recent **weekday**, while the worker's
    ``trading_date`` stays *today*. Those two are independent here and both are
    needed: `MarketSession.is_trading_day` rejects a weekend tick, so a Saturday
    tape opens no position at all — and the net's own guard requires the wall
    clock to be on the configured trading date, so the date cannot be moved
    instead. Found by this test failing on a Saturday, having passed on a Friday,
    which is the kind of green that is worth nothing.
    """
    base = _last_weekday(floor_to_interval(anchor, 300))
    return [
        _tick(UNDERLYING, 24000.0, base - timedelta(minutes=20)),
        _tick(UNDERLYING, 24010.0, base - timedelta(minutes=15)),
        _tick(CE_CONTRACT, 100.0, base - timedelta(minutes=14)),
    ]


def _config(
    runtime_dirs: dict[str, Path],
    database_path: Path,
    *,
    anchor: datetime,
    square_off_in: timedelta,
    trading_date: str | None = None,
    idle_timeout: float = IDLE_TIMEOUT,
) -> WorkerConfig:
    square_off_at = (anchor + square_off_in).time().replace(microsecond=0)
    # After every tick, so the candle clock can never reach it, and at or before
    # the square-off, which MarketSession requires.
    entry_cutoff = (anchor - timedelta(minutes=1)).time().replace(microsecond=0)
    return WorkerConfig(
        runtime_id="intraday_options",
        strategy_id="engine01",
        security_id=UNDERLYING,
        instrument="NIFTY",
        database_path=database_path,
        lock_dir=runtime_dirs["lock_dir"],
        pid_dir=runtime_dirs["pid_dir"],
        log_dir=runtime_dirs["log_dir"],
        trading_date=trading_date or anchor.date().isoformat(),
        execution_mode=ExecutionMode.PAPER,
        idle_timeout_seconds=idle_timeout,
        square_off_policy=SquareOffPolicy(
            entry_cutoff=entry_cutoff,
            square_off_at=square_off_at,
            timezone="Asia/Kolkata",
        ),
        engine=EngineWorkerConfig(
            strategy_ref=ENGINE_STRATEGY,
            strategy_kwargs={"enter_on_candle": 1, "premium_exit": True},
            timeframe="5m",
            lot_size=LOT_SIZE,
            strike_step=50,
            session_start_time="00:00",
            feed_poll_seconds=0.05,
        ),
    )


def _queue(items) -> queue_module.Queue:
    q: queue_module.Queue = queue_module.Queue()
    for item in items:
        q.put(item)
    return q


def _rows(database_path: Path, sql: str):
    connection = sqlite3.connect(str(database_path), timeout=5.0)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _open_positions(database_path: Path) -> int:
    return _rows(
        database_path, "SELECT COUNT(*) FROM positions WHERE status = 'OPEN' AND quantity != 0"
    )[0][0]


def _run(config: WorkerConfig, ticks):
    return run_worker(config, queue_module.Queue(), RecordingNotifier(), _queue(ticks), None)


@pytest.fixture
def anchor() -> datetime:
    """One clock reading per test, so the tape and the policy cannot disagree."""
    now = now_ist().replace(microsecond=0)
    if now.hour == 23 and now.minute >= 55:
        pytest.skip("within minutes of IST midnight; the relative timeline would wrap")
    return now


# ------------------------------------------------------------------- the gate
def test_a_feed_that_dies_before_the_square_off_bar_still_squares_off(
    runtime_dirs, database_path, anchor
):
    """Limitation 7. No tick ever reaches the square-off time; the clock does."""
    config = _config(
        runtime_dirs, database_path, anchor=anchor, square_off_in=timedelta(seconds=GRACE_SECONDS)
    )
    outcome = _run(config, _tape(anchor))

    assert outcome.exit_code == 0, outcome.error
    # Not vacuous: a position really did open before the net fired.
    assert outcome.orders_placed == 2, "the tape did not open and close a position"
    assert outcome.square_off_completed is True, "the wall-clock net did not fire"
    assert _open_positions(database_path) == 0, "a position was carried past square-off"


def test_the_closing_leg_is_persisted_through_the_audited_path(runtime_dirs, database_path, anchor):
    """Not merely 'the book looks flat': the close is a real SELL through
    signals -> intents -> orders -> fills, like any other."""
    config = _config(
        runtime_dirs, database_path, anchor=anchor, square_off_in=timedelta(seconds=GRACE_SECONDS)
    )
    _run(config, _tape(anchor))

    sides = [row[0] for row in _rows(database_path, "SELECT side FROM order_intents ORDER BY id")]
    assert sides == ["BUY", "SELL"]
    assert _rows(database_path, "SELECT COUNT(*) FROM fills")[0][0] == 2


def test_the_run_ends_on_the_net_rather_than_on_the_idle_timeout(
    runtime_dirs, database_path, anchor
):
    """If the run merely waited out an exhausted tape, the net proved nothing."""
    config = _config(
        runtime_dirs, database_path, anchor=anchor, square_off_in=timedelta(seconds=GRACE_SECONDS)
    )
    started = time_module.monotonic()
    _run(config, _tape(anchor))
    elapsed = time_module.monotonic() - started

    assert elapsed < IDLE_TIMEOUT / 2, (
        f"took {elapsed:.1f}s against a {IDLE_TIMEOUT}s idle timeout — that looks "
        "like the tape running dry, not the net firing"
    )


# ------------------------------------------------------------ negative controls
def test_a_square_off_still_in_the_future_leaves_the_position_open(
    runtime_dirs, database_path, anchor
):
    """Without this, every test above would also pass for a net that fired
    unconditionally — which is the bug the trading-date guard was added for."""
    config = _config(
        runtime_dirs,
        database_path,
        anchor=anchor,
        square_off_in=timedelta(hours=2),
        idle_timeout=SHORT_IDLE,
    )
    outcome = _run(config, _tape(anchor))

    assert outcome.square_off_completed is False
    assert _open_positions(database_path) == 1, "the net fired before its time"


def test_the_net_is_silent_when_the_trading_date_is_not_today(runtime_dirs, database_path, anchor):
    """A replay of a historical tape must not square off on today's wall clock.
    This is the guard 25 failing tests found on first implementation."""
    config = _config(
        runtime_dirs,
        database_path,
        anchor=anchor,
        square_off_in=timedelta(seconds=GRACE_SECONDS),
        trading_date="2026-07-16",
        idle_timeout=SHORT_IDLE,
    )
    outcome = _run(config, _tape(anchor))

    assert outcome.square_off_completed is False
    assert _open_positions(database_path) == 1


def test_a_completed_day_is_not_re_closed_after_a_restart(runtime_dirs, database_path, anchor):
    """Two sequential real runs on one database. The authority's persisted
    COMPLETED state stops the second — the net inherits restart-safety rather
    than carrying its own."""
    config = _config(
        runtime_dirs, database_path, anchor=anchor, square_off_in=timedelta(seconds=GRACE_SECONDS)
    )
    first = _run(config, _tape(anchor))
    assert first.square_off_completed is True

    before = _rows(database_path, "SELECT COUNT(*) FROM order_intents")[0][0]
    second = _run(config, [])
    assert second.exit_code == 0, second.error

    after = _rows(database_path, "SELECT COUNT(*) FROM order_intents")[0][0]
    assert after == before, "the restart placed another order"
