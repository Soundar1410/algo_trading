"""The walking-skeleton acceptance gate, end to end.

Everything here runs the real slice: recorded feed → hub → bounded queue →
worker → fixture signal → PaperBroker → SQLite → dashboard tile → notification.

The two gate tests that matter most —
:func:`test_restart_restores_the_open_paper_position` and
:func:`test_duplicate_worker_startup_is_refused` — use **real spawned
processes**, not in-process simulation, because both properties are about
process boundaries. A duplicate-worker guard that is only ever tested inside one
interpreter proves nothing about two.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import queue as queue_module
from dataclasses import replace
from datetime import time
from pathlib import Path

import pytest

from common.config.models import ExecutionMode
from common.execution import ExecutionRepository
from common.feed import SharedFeedHub
from common.feed.hub import build_channel
from common.market_data import RecordedFeedAdapter, load_tick_tape
from common.models import PositionStatus
from common.notifications import RecordingNotifier
from common.persistence import Database, MigrationRunner, connect_readonly
from common.risk import SquareOffPolicy
from dashboards.app import load_master
from dashboards.intraday_options import load_intraday_options
from runtimes.intraday_options.worker import EXIT_DUPLICATE, WorkerConfig, run_worker

TRADING_DATE = "2026-07-29"
RUNTIME_ID = "intraday_options"
STRATEGY_ID = "skelfix"
SECURITY_ID = "99926000"


@pytest.fixture
def worker_config(runtime_dirs: dict[str, Path], database_path: Path) -> WorkerConfig:
    return WorkerConfig(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        security_id=SECURITY_ID,
        instrument="NIFTY",
        database_path=database_path,
        lock_dir=runtime_dirs["lock_dir"],
        pid_dir=runtime_dirs["pid_dir"],
        log_dir=runtime_dirs["log_dir"],
        trading_date=TRADING_DATE,
        execution_mode=ExecutionMode.PAPER,
        quantity=50,
        entry_on_candle=1,
        exit_on_candle=3,
        # The tape stops at 09:21, well before any real square-off time, so
        # square-off is exercised by its own test rather than incidentally.
        square_off_policy=SquareOffPolicy(),
    )


def _candles(tick_tape_path: Path) -> list:
    """Run the real hub over the recorded tape and collect the fanned-out bars."""
    hub = SharedFeedHub(RecordedFeedAdapter(load_tick_tape(tick_tape_path)))
    channel = build_channel(STRATEGY_ID, [SECURITY_ID], in_process=True)
    hub.register(channel)
    hub.start()
    hub.stop()

    out = []
    while True:
        try:
            out.append(channel.queue.get(timeout=0.05))
        except queue_module.Empty:
            return out


def _feed_queue(candles: list) -> queue_module.Queue:
    q: queue_module.Queue = queue_module.Queue()
    for candle in candles:
        q.put(candle)
    q.put(None)  # shutdown sentinel
    return q


# ------------------------------------------------------------ the slice
def test_the_full_slice_runs_end_to_end(worker_config, tick_tape_path, database_path):
    candles = _candles(tick_tape_path)
    assert len(candles) == 6

    notifier = RecordingNotifier()
    outcome = run_worker(worker_config, _feed_queue(candles), notifier)

    assert outcome.exit_code == 0
    assert outcome.error is None
    assert outcome.candles_processed == 6
    # Entry on candle 1, exit on candle 3.
    assert outcome.orders_placed == 2

    database = Database(database_path)
    conn = database.connect()

    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM runtime_heartbeats").fetchone()[0] >= 1

    assert database.integrity_check() == []
    assert database.foreign_key_check() == []
    database.close()


def test_one_completed_candle_creates_one_deterministic_paper_order(
    worker_config, tick_tape_path, database_path
):
    """Gate: 'one completed candle creates one deterministic paper order'."""
    candles = _candles(tick_tape_path)
    config = worker_config
    config.exit_on_candle = 99  # entry only, so the count is unambiguous
    run_worker(config, _feed_queue(candles), RecordingNotifier())

    conn = Database(database_path).connect()
    rows = conn.execute("SELECT * FROM order_intents").fetchall()
    assert len(rows) == 1

    entry_candle = candles[0]
    signal = conn.execute("SELECT * FROM signals").fetchone()
    assert signal["candle_end_at"] == entry_candle.end_at.isoformat()
    assert signal["candle_close"] == entry_candle.close


def test_fill_and_position_are_persisted_with_execution_mode_paper(
    worker_config, tick_tape_path, database_path
):
    """Gate: 'fill and position appear in SQLite with execution_mode=paper'."""
    run_worker(worker_config, _feed_queue(_candles(tick_tape_path)), RecordingNotifier())

    conn = Database(database_path).connect()
    for table in ("signals", "order_intents", "orders", "fills", "positions"):
        modes = {r["execution_mode"] for r in conn.execute(f"SELECT execution_mode FROM {table}")}
        assert modes == {"paper"}, table

    correlation_ids = [
        r["correlation_id"] for r in conn.execute("SELECT correlation_id FROM orders")
    ]
    assert all(cid.startswith("p_") for cid in correlation_ids)


def test_a_telegram_event_is_produced(worker_config, tick_tape_path):
    """Gate: 'one Telegram event works'."""
    notifier = RecordingNotifier()
    run_worker(worker_config, _feed_queue(_candles(tick_tape_path)), notifier)

    event_types = [e.event_type for e in notifier.events]
    assert "worker_started" in event_types
    assert "order_filled" in event_types
    assert all(e.execution_mode is ExecutionMode.PAPER for e in notifier.events if e.execution_mode)


def test_a_failing_notifier_does_not_stop_trading(worker_config, tick_tape_path, database_path):
    class _Exploding:
        channel = "exploding"

        def send(self, event):
            raise RuntimeError("telegram is down")

    outcome = run_worker(worker_config, _feed_queue(_candles(tick_tape_path)), _Exploding())

    assert outcome.exit_code == 0
    assert outcome.orders_placed == 2
    conn = Database(database_path).connect()
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2

    # Phase 7 Part 2: SafeNotifier.on_failure now persists every failed send,
    # not just logs it — spec 2556's "persist notification failure" half,
    # closed alongside the "continue safe runtime operation" half this test
    # already proved. Every failure carries the same reason, since every
    # send() from this notifier fails identically.
    rows = conn.execute(
        "SELECT delivered, failure_reason, event_type FROM notifications ORDER BY id"
    ).fetchall()
    assert len(rows) >= 2, "worker_started and order_filled should both have failed to persist"
    assert all(r["delivered"] == 0 for r in rows)
    assert all(r["failure_reason"] == "telegram is down" for r in rows)
    assert {r["event_type"] for r in rows} >= {"worker_started", "order_filled"}


# ------------------------------------------------------------ dashboard
def test_the_dashboard_tile_renders_from_a_read_only_connection(
    worker_config, tick_tape_path, database_path
):
    """Gate: 'one dashboard tile works'.

    Phase 7 Part 3 retired the Phase 1 single tile (RuntimeTile/load_tile,
    four inline SELECTs) in favour of the multipage app reading through
    common.health.snapshot. The gate's spirit — a read-only dashboard read
    works end to end after a real run — now has two assertions: the Master
    page's card, and the Intraday Options page's per-strategy mode badge,
    which is where the "simulated" wording moved (dashboards/intraday_
    options.py's own docstring explains why: Master shows paper/live *counts*
    across strategies, not one strategy's own mode)."""
    run_worker(worker_config, _feed_queue(_candles(tick_tape_path)), RecordingNotifier())

    card = load_master(database_path, RUNTIME_ID, TRADING_DATE)
    assert card.runtime_id == RUNTIME_ID
    assert card.orders_today == 2
    assert card.group_health_state is None  # skelfix is a worker row, not a group one

    view = load_intraday_options(database_path, RUNTIME_ID, TRADING_DATE)
    assert len(view.strategies) == 1
    strategy = view.strategies[0]
    assert strategy.strategy_id == STRATEGY_ID
    assert strategy.execution_mode == "paper"
    assert "simulated" in strategy.mode_label
    assert strategy.health_state


def test_the_dashboard_connection_cannot_write(worker_config, tick_tape_path, database_path):
    import sqlite3

    run_worker(worker_config, _feed_queue(_candles(tick_tape_path)), RecordingNotifier())

    conn = connect_readonly(database_path)
    with pytest.raises(sqlite3.OperationalError, match="readonly database"):
        conn.execute("DELETE FROM positions")
    conn.close()


# ------------------------------------------------ GATE: restart recovery
def test_restart_restores_the_open_paper_position(worker_config, tick_tape_path, database_path):
    """Gate: 'restart restores the open paper position'.

    The first worker opens a position and is stopped while it is still open.
    A second worker, started fresh against the same database, must adopt it —
    and must not re-enter, which would double the exposure.
    """
    candles = _candles(tick_tape_path)

    first = worker_config
    first.exit_on_candle = 99  # leave the position open
    first_run = run_worker(first, _feed_queue(candles[:2]), RecordingNotifier())
    assert first_run.orders_placed == 1
    assert first_run.recovered_position is False

    repository = ExecutionRepository(Database(database_path))
    before = repository.open_positions(
        strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE
    )
    assert len(before) == 1
    assert before[0].status is PositionStatus.OPEN

    second_run = run_worker(worker_config, _feed_queue(candles[2:]), RecordingNotifier())

    assert second_run.recovered_position is True

    after = repository.open_positions(
        strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE
    )
    assert len(after) == 1
    assert after[0].quantity == before[0].quantity
    assert after[0].average_price == before[0].average_price
    assert after[0].entry_correlation_id == before[0].entry_correlation_id
    assert after[0].stop_price == before[0].stop_price
    assert after[0].target_price == before[0].target_price


def test_a_restarted_worker_does_not_reopen_a_position_it_already_holds(
    worker_config, tick_tape_path, database_path
):
    candles = _candles(tick_tape_path)
    first = worker_config
    first.exit_on_candle = 99
    run_worker(first, _feed_queue(candles[:2]), RecordingNotifier())

    run_worker(worker_config, _feed_queue(candles[2:]), RecordingNotifier())

    conn = Database(database_path).connect()
    buys = conn.execute("SELECT COUNT(*) FROM order_intents WHERE side = 'BUY'").fetchone()[0]
    assert buys == 1


def test_the_previous_incomplete_session_is_closed_on_recovery(
    worker_config, tick_tape_path, database_path
):
    candles = _candles(tick_tape_path)
    worker_config.exit_on_candle = 99
    run_worker(worker_config, _feed_queue(candles[:2]), RecordingNotifier())
    run_worker(worker_config, _feed_queue(candles[2:]), RecordingNotifier())

    conn = Database(database_path).connect()
    unclosed = conn.execute(
        "SELECT COUNT(*) FROM runtime_sessions WHERE ended_at IS NULL"
    ).fetchone()[0]
    assert unclosed == 0


# --------------------------------------------- GATE: duplicate refusal
def _spawn_worker(config: WorkerConfig, feed_queue, result_queue) -> None:
    """Module-level child entrypoint — must be importable for `spawn`."""
    outcome = run_worker(config, feed_queue)
    result_queue.put(outcome.exit_code)


def test_duplicate_worker_startup_is_refused(worker_config, database_path):
    """Gate: 'duplicate worker startup is refused'.

    Uses two genuinely separate processes. The first holds the lock while the
    second attempts to start; the second must exit with EXIT_DUPLICATE rather
    than trading alongside it.
    """
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    database.close()

    context = mp.get_context("spawn")
    holder_queue = context.Queue()
    holder_result = context.Queue()
    contender_queue = context.Queue()
    contender_result = context.Queue()

    holder = context.Process(
        target=_spawn_worker, args=(worker_config, holder_queue, holder_result)
    )
    holder.start()

    # Wait until the first worker actually owns the lock before contending.
    pid_file = worker_config.pid_dir / f"{RUNTIME_ID}.{STRATEGY_ID}.pid"
    deadline = 10.0
    waited = 0.0
    while not pid_file.exists() and waited < deadline:
        holder.join(timeout=0.1)
        waited += 0.1
    assert pid_file.exists(), "first worker never acquired its lock"

    # The PID file must name the holder child, not this test process — proof
    # that the lock is held across a real process boundary.
    owner_pid = json.loads(pid_file.read_text())["pid"]
    assert owner_pid == holder.pid
    assert owner_pid != os.getpid()

    contender = context.Process(
        target=_spawn_worker, args=(worker_config, contender_queue, contender_result)
    )
    contender.start()
    contender.join(timeout=30)

    assert contender.pid != holder.pid, "the two workers must be separate processes"
    assert not contender.is_alive()
    assert contender_result.get(timeout=5) == EXIT_DUPLICATE

    # The refused worker must not have written anything.
    conn = Database(database_path).connect()
    sessions = conn.execute(
        "SELECT COUNT(*) FROM runtime_sessions WHERE strategy_id = ?", (STRATEGY_ID,)
    ).fetchone()[0]
    assert sessions == 1, "the refused worker opened a session it should never have opened"

    # Release the first worker and confirm the refusal left it unharmed.
    holder_queue.put(None)
    holder.join(timeout=30)
    assert holder_result.get(timeout=5) == 0
    assert not pid_file.exists(), "a cleanly exited worker must remove its PID file"


def test_a_live_mode_contender_is_still_refused_as_a_duplicate(worker_config, database_path):
    """Regression guard for spec line 2520 ("prevent a second worker for the
    same strategy ID even when one configuration says paper and another says
    live"). ``worker_lock``'s identity has no room for mode at all (see
    ``common.process.locks.worker_lock``), so this is true by construction —
    proven here with real spawned processes, mirroring
    ``test_duplicate_worker_startup_is_refused`` above but with the contender's
    *mode* varied instead of using the identical config twice.
    """
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    database.close()

    context = mp.get_context("spawn")
    holder_queue = context.Queue()
    holder_result = context.Queue()
    contender_queue = context.Queue()
    contender_result = context.Queue()

    holder = context.Process(
        target=_spawn_worker, args=(worker_config, holder_queue, holder_result)
    )
    holder.start()

    pid_file = worker_config.pid_dir / f"{RUNTIME_ID}.{STRATEGY_ID}.pid"
    deadline = 10.0
    waited = 0.0
    while not pid_file.exists() and waited < deadline:
        holder.join(timeout=0.1)
        waited += 0.1
    assert pid_file.exists(), "first worker never acquired its lock"

    live_contender_config = replace(worker_config, execution_mode=ExecutionMode.LIVE)
    contender = context.Process(
        target=_spawn_worker,
        args=(live_contender_config, contender_queue, contender_result),
    )
    contender.start()
    contender.join(timeout=30)

    assert not contender.is_alive()
    assert contender_result.get(timeout=5) == EXIT_DUPLICATE

    # The live-mode contender never even reached the live gate in build_broker
    # — the lock, not mode, decided the outcome. No row of any kind for it.
    conn = Database(database_path).connect()
    sessions = conn.execute(
        "SELECT COUNT(*) FROM runtime_sessions WHERE strategy_id = ?", (STRATEGY_ID,)
    ).fetchone()[0]
    assert sessions == 1, "the refused live-mode contender opened a session it should never have"

    holder_queue.put(None)
    holder.join(timeout=30)
    assert holder_result.get(timeout=5) == 0


def test_the_same_strategy_can_restart_after_a_clean_exit(worker_config, tick_tape_path):
    """A released lock must not block the strategy's own restart."""
    candles = _candles(tick_tape_path)
    first = run_worker(worker_config, _feed_queue(candles[:2]), RecordingNotifier())
    assert first.exit_code == 0

    second = run_worker(worker_config, _feed_queue(candles[2:]), RecordingNotifier())
    assert second.exit_code == 0


# ---------------------------------------------------------- square off
#: The tape's six bars close at 09:16 … 09:21. Entry happens on the first, so
#: square-off is set to the fourth — late enough that there is a real position
#: to close. Setting it earlier would fire before any entry, because the worker
#: checks square-off *before* evaluating new entries (which is the correct
#: order: nothing may be opened at or after the square-off boundary).
_SQUARE_OFF_POLICY = SquareOffPolicy(
    entry_cutoff=time(9, 18),
    square_off_at=time(9, 19),
)


def test_the_worker_squares_off_at_the_configured_time(
    worker_config, tick_tape_path, database_path
):
    candles = _candles(tick_tape_path)
    config = worker_config
    config.exit_on_candle = 99  # keep the position open until square-off
    config.square_off_policy = _SQUARE_OFF_POLICY

    outcome = run_worker(config, _feed_queue(candles), RecordingNotifier())

    state = ExecutionRepository(Database(database_path)).load_strategy_state(
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )
    assert state is not None
    assert state["square_off_state"] == "COMPLETED"
    assert state["entries_blocked"] == 1
    assert outcome.square_off_completed is True

    # The position it held must actually be flat afterwards.
    open_positions = ExecutionRepository(Database(database_path)).open_positions(
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )
    assert open_positions == []


def test_no_new_entry_is_opened_after_the_cutoff(worker_config, tick_tape_path, database_path):
    config = worker_config
    config.entry_on_candle = 4  # would fire at 09:19, past the 09:18 cutoff
    config.exit_on_candle = 99
    config.square_off_policy = _SQUARE_OFF_POLICY

    run_worker(config, _feed_queue(_candles(tick_tape_path)), RecordingNotifier())

    conn = Database(database_path).connect()
    buys = conn.execute("SELECT COUNT(*) FROM order_intents WHERE side = 'BUY'").fetchone()[0]
    assert buys == 0


def test_a_completed_square_off_is_not_repeated_after_a_restart(
    worker_config, tick_tape_path, database_path
):
    candles = _candles(tick_tape_path)
    worker_config.exit_on_candle = 99
    worker_config.square_off_policy = _SQUARE_OFF_POLICY
    run_worker(worker_config, _feed_queue(candles), RecordingNotifier())

    conn = Database(database_path).connect()
    before = conn.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0]

    run_worker(worker_config, _feed_queue(candles), RecordingNotifier())
    after = conn.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0]

    assert after == before, "square-off must not run twice for the same day"
