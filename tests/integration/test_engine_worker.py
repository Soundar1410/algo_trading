"""The engine worker: everything Phase 3 built, assembled in one process.

Phase 3 Part 2b-ii-B-2. Until this part the pieces existed and nothing joined them —
``PersistedSquareOffAuthority`` had no caller outside its own tests, ``LifecycleGateway``
had no worker, and ``HubTickFeed``'s sentinel path was unreachable as deployed. These
tests drive :func:`~runtimes.intraday_options.worker.run_worker` with an
``EngineWorkerConfig``, which is the real entry point a spawned child uses.

Real throughout: real SQLite behind real migrations, a real ``ExecutionRepository``,
a real ``PaperBroker``, a real ``OrderLifecycle``, a real ``TradingEngine`` over a
real ``HubTickFeed`` reading a real queue, and the exit decided by the **real** Part
2a ``MOMENTUM_CLOSE`` policy. The only double is ``EngineFixtureStrategy``, which
supplies entry timing — real strategies are Phase 9.

The tick queue is pre-loaded rather than fed by a live hub. The hub half is already
proven end to end on the deployed two-thread topology by
``tests/integration/test_engine_over_hub.py``; what is new here is the *worker*, so
that is what these drive.
"""

from __future__ import annotations

import queue as queue_module
import sqlite3
import threading
import time as time_module
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from common.config.models import ExecutionMode
from common.engine.state_payload import DAY_SUMMARY_KEY, OPEN_POSITION_KEY, read_payload
from common.execution import ExecutionRepository
from common.feed.queues import TickDropNotice
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
#: What ``SimulatedOptionChainResolver`` builds for spot 24010 at a 50-point step.
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


#: Two underlying ticks; the second closes the 09:15 five-minute bar, which is what
#: makes the fixture strategy emit its ENTER and the engine choose a contract.
_ENTRY_TAPE = [
    _tick(UNDERLYING, 24000.0, _ts(9, 16)),
    _tick(UNDERLYING, 24010.0, _ts(9, 21)),
]

#: Fills the pending entry at 100, walks the premium up and then down, so exactly one
#: adverse *closed* premium bar exists for the real policy to fire on. Same series as
#: the Part 2b-i and 2b-ii-A suites, so a divergence here is about the worker.
_PREMIUM_WALK = [
    (_ts(9, 21, 30), 100.0),
    (_ts(9, 23), 105.0),
    (_ts(9, 26), 110.0),
    (_ts(9, 28), 108.0),
    (_ts(9, 31), 90.0),
    (_ts(9, 33), 85.0),
    (_ts(9, 36), 80.0),
]

_FULL_TAPE = [*_ENTRY_TAPE, *[_tick(CE_CONTRACT, price, ts) for ts, price in _PREMIUM_WALK]]

#: Enough to open a position and no more, so it is still open when the run ends.
_ENTRY_ONLY_TAPE = [*_ENTRY_TAPE, _tick(CE_CONTRACT, 100.0, _ts(9, 21, 30))]


def _queue(items) -> queue_module.Queue:
    q: queue_module.Queue = queue_module.Queue()
    for item in items:
        q.put(item)
    return q


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
        # Short, because every test here ends by running the tape out.
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


def _run(config: WorkerConfig, ticks, notifier=None, control_queue=None):
    return run_worker(
        config,
        queue_module.Queue(),
        notifier if notifier is not None else RecordingNotifier(),
        _queue(ticks),
        control_queue,
    )


def _repository(database_path: Path) -> ExecutionRepository:
    return ExecutionRepository(Database(database_path))


def _count(database_path: Path, table: str) -> int:
    return Database(database_path).connect().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _wait_for_open_position(database_path: Path, timeout: float = 10.0) -> bool:
    """Block until the worker has a position open, on its own connection.

    Polls the database rather than sleeping a guessed interval: the state a test
    wants to act on is the one the worker actually reached, and a sleep long enough
    to be safe on a loaded machine is long enough to make the suite slow on every
    other run. Its own connection because SQLite objects belong to their creating
    thread, and this runs on a helper.
    """
    deadline = time_module.monotonic() + timeout
    while time_module.monotonic() < deadline:
        try:
            connection = sqlite3.connect(str(database_path), timeout=1.0)
            try:
                row = connection.execute(
                    "SELECT COUNT(*) FROM positions WHERE status = 'OPEN' AND quantity != 0"
                ).fetchone()
            finally:
                connection.close()
            if row and row[0]:
                return True
        except sqlite3.Error:
            pass  # the file or the table may not exist yet
        time_module.sleep(0.02)
    return False


# ------------------------------------------------------------------ the slice
def test_the_engine_worker_trades_end_to_end_through_the_persisted_path(
    worker_config, database_path
):
    outcome = _run(worker_config, _FULL_TAPE)

    assert outcome.exit_code == 0, outcome.error
    assert outcome.error is None
    assert outcome.trades_closed == 1
    # Two legs, not one trade: the count has to reflect orders actually placed.
    assert outcome.orders_placed == 2
    assert outcome.ticks_processed == len(_FULL_TAPE)
    assert outcome.ticks_dropped_upstream == 0

    # Nothing bypassed record_signal -> reserve_intent -> submit -> apply_fill.
    for table in ("signals", "order_intents", "orders", "fills"):
        assert _count(database_path, table) == 2, table
    assert _count(database_path, "positions") == 1

    database = Database(database_path)
    assert database.integrity_check() == []
    assert database.foreign_key_check() == []


def test_the_position_is_closed_and_paper_namespaced(worker_config, database_path):
    _run(worker_config, _FULL_TAPE)

    conn = Database(database_path).connect()
    position = conn.execute("SELECT * FROM positions").fetchone()
    assert position["status"] == PositionStatus.CLOSED.value
    assert position["quantity"] == 0
    assert position["security_id"] == CE_CONTRACT

    for table in ("signals", "order_intents", "orders", "fills", "positions"):
        modes = {r["execution_mode"] for r in conn.execute(f"SELECT execution_mode FROM {table}")}
        assert modes == {"paper"}, table
    correlation_ids = [
        r["correlation_id"] for r in conn.execute("SELECT correlation_id FROM orders")
    ]
    assert all(cid.startswith("p_") for cid in correlation_ids)


def test_the_exit_was_decided_by_the_real_part_2a_policy(worker_config, database_path):
    """The closing leg exists because ``MOMENTUM_CLOSE`` fired on a premium bar."""
    _run(worker_config, _FULL_TAPE)

    conn = Database(database_path).connect()
    sides = [r["side"] for r in conn.execute("SELECT side FROM order_intents ORDER BY id")]
    assert sides == ["BUY", "SELL"]

    summary = read_payload(
        _repository(database_path),
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )[DAY_SUMMARY_KEY]
    assert summary["trades"][0]["exit_reason"] == "MOMENTUM_LOW"


def test_the_contract_was_chosen_at_runtime_and_asked_for_upstream(worker_config):
    """The engine picks its strike mid-session, so the subscription has to travel.

    Entries are ``(security_id, segment)`` as of Phase 4 Part 1. A simulated
    contract has no exchange segment, so ``None`` here is the correct value and
    not an omission — the assertion below pins that rather than ignoring it.
    """
    control_queue: queue_module.Queue = queue_module.Queue()
    _run(worker_config, _FULL_TAPE, control_queue=control_queue)

    requested = []
    while not control_queue.empty():
        requested.append(control_queue.get_nowait())
    assert (CE_CONTRACT, None) in requested, "the runtime subscription never reached the supervisor"


# ------------------------------------------------------------- D20 reporting
def test_the_day_summary_reaches_the_strategy_state_payload(worker_config, database_path):
    """What the execution tables cannot hold — MFE/MAE, regime, the engine's reason."""
    _run(worker_config, _FULL_TAPE)

    summary = read_payload(
        _repository(database_path),
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )[DAY_SUMMARY_KEY]

    assert summary["trade_count"] == 1
    assert summary["net_pnl"] == pytest.approx(summary["gross_pnl"] - summary["total_charges"])
    trade = summary["trades"][0]
    assert trade["security_id"] == CE_CONTRACT
    # The premium walked to 110 before falling to 80, so the excursion is real data
    # rather than a zero that would also pass a weaker assertion.
    assert trade["mfe"] > 0
    assert trade["mae"] < 0


def test_running_and_terminal_heartbeats_are_published(worker_config, database_path):
    _run(worker_config, _FULL_TAPE)

    states = [
        row["health_state"]
        for row in Database(database_path)
        .connect()
        .execute(
            "SELECT health_state FROM runtime_heartbeats WHERE strategy_id = ? ORDER BY id",
            (STRATEGY_ID,),
        )
        .fetchall()
    ]
    assert states[0] == "STARTING"
    assert "RUNNING_PAPER" in states
    assert states[-1] == "STOPPED", states
    assert "DEGRADED" not in states


def test_a_clean_run_closes_its_session(worker_config, database_path):
    _run(worker_config, _FULL_TAPE)

    unclosed = (
        Database(database_path)
        .connect()
        .execute("SELECT COUNT(*) FROM runtime_sessions WHERE ended_at IS NULL")
        .fetchone()[0]
    )
    assert unclosed == 0


# -------------------------------------------------- the open-position record
def test_an_open_position_leaves_a_contract_record_behind(worker_config, database_path):
    """What restart recovery reads. The ``positions`` row alone cannot express it."""
    outcome = _run(worker_config, _ENTRY_ONLY_TAPE)
    assert outcome.exit_code == 0, outcome.error
    assert outcome.trades_closed == 0

    record = read_payload(
        _repository(database_path),
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )[OPEN_POSITION_KEY]

    assert record["security_id"] == CE_CONTRACT
    assert record["option_type"] == "CE"
    assert record["strike"] == 24000.0
    assert record["lot_size"] == LOT_SIZE
    assert record["side"] == "BUY"
    assert record["lots"] == 1


def test_closing_the_position_clears_the_contract_record(worker_config, database_path):
    _run(worker_config, _FULL_TAPE)

    payload = read_payload(
        _repository(database_path),
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )
    assert OPEN_POSITION_KEY not in payload
    # And the other writer's key survived the clear.
    assert DAY_SUMMARY_KEY in payload


# --------------------------------------------------------------- the refusals
def test_an_engine_worker_without_a_tick_queue_refuses_to_start(worker_config, database_path):
    """Falling back to the fixture strategy would trade something never reviewed."""
    outcome = run_worker(worker_config, queue_module.Queue(), RecordingNotifier(), None, None)

    assert outcome.exit_code == 1
    assert "tick queue" in (outcome.error or "")
    assert _count(database_path, "order_intents") == 0

    error = (
        Database(database_path)
        .connect()
        .execute("SELECT severity, component, message FROM errors ORDER BY id DESC LIMIT 1")
        .fetchone()
    )
    assert error["severity"] == "CRITICAL"
    assert error["component"] == "engine"


def test_live_mode_is_still_refused_on_the_engine_path(worker_config, database_path):
    """Deviation D5: the engine's arrival must not reintroduce a live broker.

    The gate stays in ``build_broker``, consulted exactly as the fixture path
    consults it — the engine gets no opinion of its own about live.
    """
    worker_config.execution_mode = ExecutionMode.LIVE

    outcome = _run(worker_config, _FULL_TAPE)

    assert outcome.exit_code == 1
    assert _count(database_path, "order_intents") == 0
    assert _count(database_path, "fills") == 0


def test_an_unknown_strategy_reference_fails_before_any_order(worker_config, database_path):
    worker_config.engine.strategy_ref = "strategies.intraday_options.fixture_strategy:Strategy"

    outcome = _run(worker_config, _FULL_TAPE)

    assert outcome.exit_code == 1
    assert _count(database_path, "order_intents") == 0


# ------------------------------------------------------- limitation 14's block
def test_a_dropped_tick_upstream_blocks_this_workers_entries(worker_config, database_path):
    """Runbook limitation 14 / D23, end to end through the worker.

    The engine builds its own candles from these ticks, so a drop upstream means its
    OHLC may be quietly wrong — and trading on bars that might be wrong is worse than
    not trading. The same tape without the notice is the control below.
    """
    outcome = _run(worker_config, [TickDropNotice(dropped=3), *_FULL_TAPE])

    assert outcome.exit_code == 0, outcome.error
    assert outcome.ticks_dropped_upstream == 3
    assert _count(database_path, "order_intents") == 0, "an entry was taken after a drop"


def test_the_same_tape_enters_normally_without_a_drop(worker_config, database_path):
    outcome = _run(worker_config, _FULL_TAPE)

    assert outcome.ticks_dropped_upstream == 0
    assert _count(database_path, "order_intents") == 2


def test_a_drop_after_entry_still_lets_the_open_position_exit(worker_config, database_path):
    """Entry side only — a block must never trap a position it cannot close."""
    tape = [*_ENTRY_TAPE, _tick(CE_CONTRACT, 100.0, _ts(9, 21, 30)), TickDropNotice(dropped=1)]
    tape += [_tick(CE_CONTRACT, price, ts) for ts, price in _PREMIUM_WALK[1:]]

    outcome = _run(worker_config, tape)

    assert outcome.ticks_dropped_upstream == 1
    assert outcome.trades_closed == 1
    conn = Database(database_path).connect()
    assert conn.execute("SELECT status FROM positions").fetchone()["status"] == "CLOSED"


# ------------------------------------------------------------- the sentinel
def test_the_shutdown_sentinel_squares_off_the_open_position(worker_config, database_path):
    """The path Part 2b-ii-A built and Part 2b-ii-B-2 finally made reachable.

    The supervisor publishes ``None`` per worker at shutdown; ``HubTickFeed`` turns
    it into ``request_square_off``, and the engine closes on its own thread.
    """
    outcome = _run(worker_config, [*_ENTRY_ONLY_TAPE, None])

    assert outcome.exit_code == 0, outcome.error
    assert outcome.stopped_by_request is True
    assert outcome.square_off_completed is True
    assert outcome.trades_closed == 1
    assert outcome.clean_engine_shutdown is True

    conn = Database(database_path).connect()
    assert conn.execute("SELECT status FROM positions").fetchone()["status"] == "CLOSED"

    state = _repository(database_path).load_strategy_state(
        strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE
    )
    assert state["square_off_state"] == "COMPLETED"


def test_the_candle_sentinel_also_reaches_the_engine(worker_config, database_path):
    """The redundant path: the supervisor sentinels both channels.

    An engine worker never consumes candles (D23), but if the tick channel is starved
    or backed up behind a full buffer the shutdown still has to arrive — so the drain
    thread watches for the sentinel there too.

    The sentinel is published only once a position is genuinely open, waiting on the
    database rather than on a sleep. Publishing it up front would prove far less: the
    engine would square off an empty book, which any implementation passes.
    """
    worker_config.idle_timeout_seconds = 5.0  # the sentinel must end this run, not idling
    candle_queue: queue_module.Queue = queue_module.Queue()

    def _sentinel_once_open() -> None:
        if _wait_for_open_position(database_path):
            candle_queue.put(None)

    publisher = threading.Thread(target=_sentinel_once_open, daemon=True)
    publisher.start()
    outcome = run_worker(
        worker_config,
        candle_queue,
        RecordingNotifier(),
        _queue(_ENTRY_ONLY_TAPE),
        None,
    )
    publisher.join(timeout=5.0)

    assert outcome.exit_code == 0, outcome.error
    assert outcome.stopped_by_request is True
    assert outcome.trades_closed == 1, "the sentinel arrived but nothing was squared off"
    conn = Database(database_path).connect()
    assert conn.execute("SELECT status FROM positions").fetchone()["status"] == "CLOSED"


def test_a_run_that_ends_on_its_own_is_not_reported_as_requested(worker_config):
    """An exhausted tape is not a shutdown, and a position left open by one is not
    an alarm — it is the ordinary case restart recovery exists to adopt."""
    outcome = _run(worker_config, _ENTRY_ONLY_TAPE)

    assert outcome.stopped_by_request is False
    assert outcome.clean_engine_shutdown is True
    assert outcome.square_off_completed is False


# ------------------------------------------------------- the square-off clock
def test_the_square_off_time_is_derived_from_the_policy_not_configured_twice(
    worker_config, database_path
):
    """One configured pair, two derived session strings.

    Moving the policy has to move the engine's session with it; the fifteen-minute
    drift between the two sets of defaults is what Part 2b-ii-B-1 removed, and a
    worker that re-configured them independently would put it straight back.
    """
    worker_config.square_off_policy = SquareOffPolicy(
        entry_cutoff=time(9, 25), square_off_at=time(9, 30)
    )
    late = [*_ENTRY_ONLY_TAPE, _tick(CE_CONTRACT, 99.0, _ts(9, 31))]

    outcome = _run(worker_config, late)

    assert outcome.square_off_completed is True
    assert outcome.trades_closed == 1
    conn = Database(database_path).connect()
    assert conn.execute("SELECT status FROM positions").fetchone()["status"] == "CLOSED"


def test_a_completed_square_off_is_not_repeated_after_a_restart(worker_config, database_path):
    """``PersistedSquareOffAuthority``'s reason for existing, through a real worker."""
    worker_config.square_off_policy = SquareOffPolicy(
        entry_cutoff=time(9, 25), square_off_at=time(9, 30)
    )
    _run(worker_config, [*_ENTRY_ONLY_TAPE, _tick(CE_CONTRACT, 99.0, _ts(9, 31))])
    before = _count(database_path, "order_intents")

    _run(worker_config, [_tick(CE_CONTRACT, 99.0, _ts(9, 32))])

    assert _count(database_path, "order_intents") == before
