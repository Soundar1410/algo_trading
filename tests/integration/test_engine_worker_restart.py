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
        .execute(
            "SELECT severity, component, message FROM errors "
            "WHERE component = 'engine.recovery' ORDER BY id DESC LIMIT 1"
        )
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

    assert second.exit_code == 1
    assert second.recovered_position is False
    assert second.orders_placed == 0
    error = (
        Database(database_path)
        .connect()
        .execute(
            "SELECT severity, component, message FROM errors "
            "WHERE component = 'engine.recovery' ORDER BY id DESC LIMIT 1"
        )
        .fetchone()
    )
    assert error["severity"] == "CRITICAL"
    assert error["component"] == "engine.recovery"
    assert "carry-forward" in error["message"]


def test_multiple_open_positions_are_refused_before_engine_handoff(worker_config, database_path):
    _run(worker_config, _FIRST_TAPE)
    with Database(database_path).transaction() as conn:
        original = conn.execute(
            "SELECT * FROM positions WHERE strategy_id = ?", (STRATEGY_ID,)
        ).fetchone()
        conn.execute(
            "INSERT INTO positions "
            "(runtime_id, strategy_id, execution_mode, trading_date, instrument, "
            "security_id, quantity, average_price, entry_correlation_id, realised_pnl, "
            "charges, status, opened_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 'OPEN', ?, ?)",
            (
                original["runtime_id"],
                original["strategy_id"],
                original["execution_mode"],
                original["trading_date"],
                "SECOND OPTION",
                "SIM:NIFTY:WEEKLY:24500:PE",
                original["quantity"],
                original["average_price"],
                "unexpected-second-entry",
                original["opened_at"],
                original["updated_at"],
            ),
        )

    second = _run(worker_config, _SECOND_TAPE)

    assert second.exit_code == 1
    assert second.recovered_position is False
    assert second.orders_placed == 0
    error = (
        Database(database_path)
        .connect()
        .execute(
            "SELECT severity, component, message FROM errors "
            "WHERE component = 'engine.recovery' ORDER BY id DESC LIMIT 1"
        )
        .fetchone()
    )
    assert error["severity"] == "CRITICAL"
    assert "exactly one" in error["message"]


# ------------------------------------- Phase 6 Part 1: daily risk state across a restart
#
# ``worker_config`` above has no ``max_daily_loss_percent`` set, so its engine builds
# no ``DailyRiskGuard`` at all (see ``TradingEngine._build_daily_guard``) — these tests
# need one, so they get their own fixture and their own strategy_id (a fresh database
# either way, since ``database_path`` is function-scoped, but a distinct id keeps the
# intent obviously self-contained on inspection).

DAILY_LOSS_STRATEGY_ID = "engine_daily_loss"

#: Opens and closes within one run, at a real loss — same premium walk as
#: ``_SECOND_TAPE`` (same relative gaps, so the same 5-minute candle buckets close the
#: same way), shifted 25 minutes earlier so entry and exit both land in one run instead
#: of straddling a restart the way ``_FIRST_TAPE``/``_SECOND_TAPE`` deliberately do.
_LOSS_TAPE = [
    _tick(UNDERLYING, 24000.0, _ts(9, 16)),
    _tick(UNDERLYING, 24010.0, _ts(9, 21)),  # closes candle 1 -> ENTER
    _tick(CE_CONTRACT, 100.0, _ts(9, 21, 30)),  # fills the pending entry
    _tick(CE_CONTRACT, 105.0, _ts(9, 23)),
    _tick(CE_CONTRACT, 110.0, _ts(9, 26)),
    _tick(CE_CONTRACT, 108.0, _ts(9, 28)),
    _tick(CE_CONTRACT, 90.0, _ts(9, 31)),
    _tick(CE_CONTRACT, 85.0, _ts(9, 33)),
    _tick(CE_CONTRACT, 80.0, _ts(9, 36)),  # closes the 108 bucket at 85 -> MOMENTUM_CLOSE exits
]

#: A second run's attempt at a fresh entry, complete with the fill tick that would
#: complete it if nothing blocks it. **The fill tick matters**: ``_enter()`` only
#: queues ``self._pending`` — the gateway is not called, and no order intent exists,
#: until a matching tick actually arrives (``TradingEngine.on_tick``'s pending-entry
#: branch). A tape that stopped at the candle-close tick would pass these tests
#: whether or not the block worked, because neither case reaches the gateway either
#: way. ``orders_placed`` (``gateway.executions``, one count per run) is what actually
#: distinguishes them: 0 if blocked, 1 if this entry went through.
_ENTRY_ATTEMPT_TAPE = [
    _tick(UNDERLYING, 24000.0, _ts(9, 41)),
    _tick(UNDERLYING, 24010.0, _ts(9, 46)),  # closes candle 1 -> ENTER
    _tick(CE_CONTRACT, 100.0, _ts(9, 46, 30)),  # would fill the pending entry, if not blocked
]


@pytest.fixture
def daily_loss_worker_config(runtime_dirs: dict[str, Path], database_path: Path) -> WorkerConfig:
    return WorkerConfig(
        runtime_id=RUNTIME_ID,
        strategy_id=DAILY_LOSS_STRATEGY_ID,
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
            starting_capital=100_000.0,
            # A cap far smaller than any plausible round-trip loss on this tape (which
            # runs to roughly -1000 once costs are included) -- these tests care that a
            # real loss trips it, not the exact rupee figure PaperBroker produces.
            max_daily_loss_percent=0.01,
        ),
    )


def _daily_realised_pnl(database_path: Path, strategy_id: str) -> float:
    row = (
        Database(database_path)
        .connect()
        .execute(
            "SELECT daily_realised_pnl FROM strategy_state WHERE strategy_id = ?",
            (strategy_id,),
        )
        .fetchone()
    )
    return float(row["daily_realised_pnl"])


def test_a_restart_after_a_loss_already_past_the_cap_takes_no_new_entry(
    daily_loss_worker_config, database_path
):
    first = _run(daily_loss_worker_config, _LOSS_TAPE)
    assert first.exit_code == 0, first.error
    assert first.trades_closed == 1, "the loss tape must actually close, or this proves nothing"

    booked = _daily_realised_pnl(database_path, DAILY_LOSS_STRATEGY_ID)
    assert booked < -10.0, "the tape must produce a real loss past the configured cap"

    second = _run(daily_loss_worker_config, _ENTRY_ATTEMPT_TAPE)

    assert second.exit_code == 0
    assert second.orders_placed == 0, "a restart after an already-capped loss must not enter"
    assert (
        _repository(database_path).open_positions(
            strategy_id=DAILY_LOSS_STRATEGY_ID,
            execution_mode=ExecutionMode.PAPER,
            trading_date=TRADING_DATE,
        )
        == []
    )


def test_a_fresh_trading_date_does_not_carry_the_daily_loss_halt_over(
    daily_loss_worker_config, database_path
):
    """The no-leak rule (spec section 12) applies to daily risk state too."""
    first = _run(daily_loss_worker_config, _LOSS_TAPE)
    assert first.trades_closed == 1

    daily_loss_worker_config.trading_date = "2026-07-17"
    second = _run(daily_loss_worker_config, _ENTRY_ATTEMPT_TAPE)

    assert second.exit_code == 0
    assert second.orders_placed == 1, "a new trading date must start the daily loss cap at zero"


def test_daily_risk_recovery_failure_blocks_entries_without_crashing(
    daily_loss_worker_config, database_path
):
    """A ``strategy_state.daily_realised_pnl`` that will not convert must fail closed.

    Mirrors ``test_an_open_position_with_no_contract_record_blocks_entries_rather_than_trading``:
    a value written by something other than this build (or corrupted) must never be
    guessed at, because a wrong guess could be too low and let a strategy trade past a
    limit it had already hit. Deliberately uses ``_LOSS_TAPE`` (opens and closes,
    ending flat) rather than the open-position tapes above: this test isolates the
    daily-risk failure path from position recovery's own — a leftover open position
    would block the second run's fill through *its* has-position guard regardless of
    whether the daily-risk block worked, proving nothing about this one.
    """
    first = _run(daily_loss_worker_config, _LOSS_TAPE)
    assert first.exit_code == 0, first.error
    assert first.trades_closed == 1

    repository = _repository(database_path)
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE strategy_state SET daily_realised_pnl = 'not-a-number' WHERE strategy_id = ?",
            (DAILY_LOSS_STRATEGY_ID,),
        )

    second = _run(daily_loss_worker_config, _ENTRY_ATTEMPT_TAPE)

    assert second.exit_code == 0, "a recoverable inconsistency must not crash the worker"
    assert second.orders_placed == 0, "it entered again despite unrestorable daily risk state"

    error = (
        Database(database_path)
        .connect()
        .execute("SELECT severity, component, message FROM errors ORDER BY id DESC LIMIT 1")
        .fetchone()
    )
    assert error["severity"] == "CRITICAL"
    assert error["component"] == "engine.recovery"
    assert "daily risk" in error["message"]


# ------------------------------------------- Phase 6 Part 2: exit-policy state
#
# A trailing stop's peak, restored through the real worker path — the property
# the plan named directly: "a trailing stop that had locked a peak before the
# restart still exits on the same retracement after it."

TRAILING_STRATEGY_ID = "engine_trailing"


@pytest.fixture
def trailing_worker_config(runtime_dirs: dict[str, Path], database_path: Path) -> WorkerConfig:
    return WorkerConfig(
        runtime_id=RUNTIME_ID,
        strategy_id=TRAILING_STRATEGY_ID,
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
            strategy_kwargs={
                "enter_on_candle": 1,
                "premium_exit": True,
                "exit_engine_name": "trailing",
                "exit_engine_params": {"trail_points": 15},
            },
            timeframe="5m",
            lot_size=LOT_SIZE,
            strike_step=50,
            feed_poll_seconds=0.05,
        ),
    )


#: Opens, then walks the premium up to a peak of 40 (100 -> 140) and stops —
#: not enough retracement yet to fire (trail_points=15). The peak this leaves
#: behind is exactly what the restart must not lose.
_TRAILING_RUN1_TAPE = [
    _tick(UNDERLYING, 24000.0, _ts(9, 16)),
    _tick(UNDERLYING, 24010.0, _ts(9, 21)),  # closes candle 1 -> ENTER
    _tick(CE_CONTRACT, 100.0, _ts(9, 21, 30)),  # fills the pending entry
    _tick(CE_CONTRACT, 140.0, _ts(9, 23)),  # within the 09:20-09:25 bucket
    _tick(CE_CONTRACT, 145.0, _ts(9, 26)),  # closes 09:20-09:25 at 140 -> peak=40
]

#: A restart, then straight to a retracement that only fires if the 40 peak
#: survived: 140 -> 120 gives back 20 >= 15. A fresh (unrestored) trailing
#: engine would instead read 120 as a brand-new peak and never fire.
_TRAILING_RUN2_TAPE = [
    _tick(CE_CONTRACT, 120.0, _ts(9, 41)),  # 09:40-09:45 bucket opens
    _tick(CE_CONTRACT, 125.0, _ts(9, 46)),  # closes it at 120 -> retrace 40-20=20 >= 15
]


def test_a_trailing_stops_peak_survives_a_restart_and_still_exits(
    trailing_worker_config, database_path
):
    first = _run(trailing_worker_config, _TRAILING_RUN1_TAPE)
    assert first.exit_code == 0, first.error
    assert first.trades_closed == 0, "the tape must stop short of firing, or this proves nothing"
    assert _open_positions_for(database_path, TRAILING_STRATEGY_ID)[0].status is PositionStatus.OPEN

    second = _run(trailing_worker_config, _TRAILING_RUN2_TAPE)

    assert second.exit_code == 0, second.error
    assert second.recovered_position is True
    assert second.trades_closed == 1, "the restored peak must be what this retracement exits on"


def test_a_fresh_leg_after_a_gap_reads_a_new_peak_not_the_restored_one(
    trailing_worker_config, database_path
):
    """Control for the test above: prove the tape genuinely depends on restore()
    rather than firing on its own merits regardless of the prior peak.

    Same run-two tape, but the exit-state key is cleared before it runs — the
    same premium walk (120 -> close 120, no second data point yet) must NOT
    exit, because with no restored peak there is nothing to retrace *from* on
    the very first candle it sees.
    """
    _run(trailing_worker_config, _TRAILING_RUN1_TAPE)
    repository = _repository(database_path)
    payload = _payload_for(database_path, TRAILING_STRATEGY_ID)
    del payload["exit_state"]
    with repository.database.transaction() as conn:
        import json

        conn.execute(
            "UPDATE strategy_state SET payload = ? WHERE strategy_id = ?",
            (json.dumps(payload), TRAILING_STRATEGY_ID),
        )

    second = _run(trailing_worker_config, _TRAILING_RUN2_TAPE)

    assert second.exit_code == 0, second.error
    assert second.recovered_position is True
    assert second.trades_closed == 0, "with no restored peak, this retracement must not fire"


def test_a_stale_exit_state_snapshot_for_another_contract_is_ignored(
    trailing_worker_config, database_path
):
    """Mirrors ``test_a_stale_contract_record_for_another_instrument_is_refused``
    for the exit-state key: unlike position/daily-risk recovery, the refusal
    must not block anything — only degrade this run's exit timing back to a
    fresh peak."""
    _run(trailing_worker_config, _TRAILING_RUN1_TAPE)
    repository = _repository(database_path)
    payload = _payload_for(database_path, TRAILING_STRATEGY_ID)
    payload["exit_state"]["security_id"] = "SIM:NIFTY:WEEKLY:24500:PE"
    with repository.database.transaction() as conn:
        import json

        conn.execute(
            "UPDATE strategy_state SET payload = ? WHERE strategy_id = ?",
            (json.dumps(payload), TRAILING_STRATEGY_ID),
        )

    second = _run(trailing_worker_config, _TRAILING_RUN2_TAPE)

    assert second.exit_code == 0, second.error
    assert second.recovered_position is True, "the position itself must still adopt cleanly"
    assert second.trades_closed == 0, "a snapshot naming a different contract must be ignored"
    # No error row for this: a stale exit-state snapshot degrades timing, not
    # safety, so it must never be reported the way position/daily-risk failures
    # are — that would be a false CRITICAL for something that isn't one.
    error = (
        Database(database_path)
        .connect()
        .execute(
            "SELECT COUNT(*) FROM errors WHERE strategy_id = ? AND component = 'engine.recovery'",
            (TRAILING_STRATEGY_ID,),
        )
        .fetchone()[0]
    )
    assert error == 0


def _open_positions_for(database_path: Path, strategy_id: str):
    return _repository(database_path).open_positions(
        strategy_id=strategy_id,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )


def _payload_for(database_path: Path, strategy_id: str) -> dict:
    return read_payload(
        _repository(database_path),
        strategy_id=strategy_id,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )


# --------------------------------------------------------- Phase 6 Part 3: MFE/MAE
#: Opens and walks the premium up in two candles without ever triggering
#: momentum_close (each closes higher than the one before), leaving a peak of
#: 3250 (150 - 100 = 50 * LOT_SIZE) — same structure as _LOSS_TAPE, direction
#: reversed, and stopping short of any exit.
_MFE_RUN1_TAPE = [
    _tick(UNDERLYING, 24000.0, _ts(9, 16)),
    _tick(UNDERLYING, 24010.0, _ts(9, 21)),  # closes candle 1 -> ENTER
    _tick(CE_CONTRACT, 100.0, _ts(9, 21, 30)),  # fills the pending entry
    _tick(CE_CONTRACT, 130.0, _ts(9, 23)),
    _tick(CE_CONTRACT, 140.0, _ts(9, 26)),  # closes 09:20-09:25 @130 -- no previous, no exit
    _tick(CE_CONTRACT, 150.0, _ts(9, 28)),
    _tick(CE_CONTRACT, 155.0, _ts(9, 31)),  # closes 09:25-09:30 @150 -- 150>130, no exit; peak=3250
]

#: A restart, then a fresh premium candle (no previous, no exit) followed by
#: one that closes lower, firing momentum_close -- at a price (90) far below
#: the restored peak, so the peak survives only if it was actually restored.
_MFE_RUN2_TAPE = [
    _tick(CE_CONTRACT, 120.0, _ts(9, 41)),
    _tick(CE_CONTRACT, 125.0, _ts(9, 46)),  # closes @120 -- first post-restart candle, no exit
    _tick(CE_CONTRACT, 90.0, _ts(9, 48)),
    _tick(CE_CONTRACT, 85.0, _ts(9, 51)),  # closes @90 -- 90<120 -> EXIT
]


def test_mfe_survives_a_restart_rather_than_resetting_to_the_first_post_restart_tick(
    worker_config, database_path
):
    """Fails without Part 3's seeding: an unrestored position's peak starts at
    0.0, so the first post-restart profitable tick would read as a *new* peak
    instead of falling short of the real one.

    Compares before/after rather than a hand-computed literal: ``update_price``
    runs on every tick (not just candle closes), so the exact peak depends on
    PaperBroker's entry slippage too. The property under test — a restart must
    not lose it — doesn't need the exact number, only that run 2's ticks (all
    below run 1's peak) leave it unchanged.
    """
    first = _run(worker_config, _MFE_RUN1_TAPE)
    assert first.exit_code == 0, first.error
    assert first.trades_closed == 0, "the tape must stop short of firing, or this proves nothing"
    restored_peak = _open_positions(database_path)[0].highest_favourable
    assert restored_peak is not None and restored_peak > 0, "the tape must build a real peak"

    second = _run(worker_config, _MFE_RUN2_TAPE)

    assert second.exit_code == 0, second.error
    assert second.recovered_position is True
    assert second.trades_closed == 1

    closed = Database(database_path).connect().execute("SELECT * FROM positions").fetchone()
    # Every run 2 tick (120, 125, 90, 85) is below the peak reached in run 1
    # (which topped out at 155), so a correctly-restored peak must be
    # unchanged -- not reset to 0 and rebuilt from run 2's own smaller ticks.
    assert closed["highest_favourable"] == pytest.approx(restored_peak)


# --------------------------------------------- Phase 6 Part 3: state version
def test_an_unrecognised_state_version_blocks_position_recovery(worker_config, database_path):
    """Mirrors ``test_a_stale_contract_record_for_another_instrument_is_refused``:
    an unreadable/unrecognised payload must fail closed for position recovery,
    with the same CRITICAL-row treatment every other recovery failure gets."""
    first = _run(worker_config, _FIRST_TAPE)
    assert first.exit_code == 0, first.error

    repository = _repository(database_path)
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE strategy_state SET state_version = 99 WHERE strategy_id = ?",
            (STRATEGY_ID,),
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
    assert "state_version" in error["message"]


def test_a_bad_state_version_blocks_position_recovery_before_exit_state_is_ever_read(
    trailing_worker_config, database_path
):
    """A finding from building this test, corrected here rather than left
    asserting what the plan originally claimed.

    The plan's draft expected exit-state recovery's fail-open path (D60) to be
    independently reachable through the *same* state_version corruption that
    fails position recovery closed. It is not, and cannot be, in this
    architecture: ``state_version`` gates the whole ``strategy_state`` row, not
    a key within it, and ``recover_exit_state`` is only ever called from
    *inside* ``_adopt_recovered_position`` — after ``recover_position`` has
    already succeeded. A version bad enough to block one blocks both, and
    position recovery, which runs first, always intercepts it — exit-state
    recovery's own fail-open wrapper never gets a chance to see this particular
    failure. (It remains real and reachable for the failures Part 2's own test
    already covers — a foreign ``security_id``, or no snapshot at all — which
    do not depend on ``state_version``.)
    """
    _run(trailing_worker_config, _TRAILING_RUN1_TAPE)
    repository = _repository(database_path)
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE strategy_state SET state_version = 99 WHERE strategy_id = ?",
            (TRAILING_STRATEGY_ID,),
        )

    second = _run(trailing_worker_config, _TRAILING_RUN2_TAPE)

    assert second.exit_code == 0, second.error
    assert second.recovered_position is False, "position recovery fails closed first"
    assert second.trades_closed == 0
    error = (
        Database(database_path)
        .connect()
        .execute(
            "SELECT severity, component FROM errors WHERE strategy_id = ? ORDER BY id DESC LIMIT 1",
            (TRAILING_STRATEGY_ID,),
        )
        .fetchone()
    )
    assert error["severity"] == "CRITICAL"
    assert error["component"] == "engine.recovery"


# --------------------------------------------- Phase 6 Part 3: candle idempotency
#: Two premium candles, so the guard's target (the first) and its control (the
#: second, which must still process) are distinguishable. Reuses the trailing
#: fixture's peak from _TRAILING_RUN1_TAPE.
_WATERMARK_RUN2_TAPE = [
    _tick(CE_CONTRACT, 120.0, _ts(9, 41)),
    _tick(CE_CONTRACT, 125.0, _ts(9, 46)),  # closes @120, ts 09:46 -- this one gets skipped
    _tick(CE_CONTRACT, 90.0, _ts(9, 48)),
    _tick(CE_CONTRACT, 85.0, _ts(9, 51)),  # closes @90, ts 09:51 -- past the watermark, must fire
]


def test_a_restored_watermark_blocks_reprocessing_the_same_candle(
    trailing_worker_config, database_path
):
    """The property the plan's own bullet names: a replayed candle already
    reflected in last_candle_end_at must not double-count. Simulated by
    hand-setting the watermark to the first of run 2's two candle boundaries
    before running it, so only that one is skipped."""
    _run(trailing_worker_config, _TRAILING_RUN1_TAPE)
    repository = _repository(database_path)
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE strategy_state SET last_candle_end_at = ? WHERE strategy_id = ?",
            (_ts(9, 46).isoformat(), TRAILING_STRATEGY_ID),
        )

    second = _run(trailing_worker_config, _WATERMARK_RUN2_TAPE)

    assert second.exit_code == 0, second.error
    assert second.recovered_position is True
    # The skipped candle (@120, ts 09:46) never reaches the exit engine -- but
    # the *next* candle (@90, ts 09:51, past the watermark) still processes
    # normally and fires against the restored trailing peak from run 1.
    assert second.trades_closed == 1


def test_without_a_restored_watermark_both_candles_process_normally(
    trailing_worker_config, database_path
):
    """Control for the test above: the same two-candle tape, no watermark set,
    must behave exactly as Part 2 already proved -- proving the skip above is
    the watermark's doing, not something already true of this tape."""
    _run(trailing_worker_config, _TRAILING_RUN1_TAPE)

    second = _run(trailing_worker_config, _WATERMARK_RUN2_TAPE)

    assert second.exit_code == 0, second.error
    assert second.trades_closed == 1


def test_a_flat_restart_does_not_carry_the_candle_guard_over(worker_config, database_path):
    """Control proving the guard is position-gated: a strategy-day with nothing
    adopted (the position already closed in run 1) must not have any watermark
    applied in run 2, even if strategy_state.last_candle_end_at holds a stale
    value from run 1's own bookkeeping."""
    first = _run(worker_config, _FIRST_TAPE)
    assert first.exit_code == 0

    second = _run(worker_config, _SECOND_TAPE)  # adopts and closes within run 2
    assert second.trades_closed == 1

    # Nothing was adopted this time (already flat) -- a fresh entry attempt
    # must not be silently skipped by a leftover watermark.
    third = _run(worker_config, _ENTRY_ATTEMPT_TAPE)
    assert third.exit_code == 0
    assert third.orders_placed == 1, "a flat restart must not inherit a stale candle watermark"


def test_stop_and_target_stay_null_under_todays_risk_manager(worker_config, database_path):
    """Phase 6 Part 2's negative control on the widened stop/target plumbing.

    ``FixtureRiskManager`` — the only risk manager this repository can run
    today — reports neither, so the columns the engine now threads all the way
    through ``PositionManager.open()`` -> ``LifecycleGateway`` -> ``apply_fill``
    must still land NULL. The day Phase 9 adds a manager that overrides
    ``stop_price``/``target_price``, this test starts seeing a real number and
    fails — forcing that change to be confronted here rather than inherited
    silently.
    """
    first = _run(worker_config, _FIRST_TAPE)
    assert first.exit_code == 0, first.error

    position = _open_positions(database_path)[0]
    assert position.stop_price is None
    assert position.target_price is None
