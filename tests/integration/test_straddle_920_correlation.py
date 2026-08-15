"""P0-3: end-to-end basket/leg correlation, proved against a real database.

Runs the real worker (``run_worker``) through entry, an adjustment
(close + replacement), a restart, and a hard square-off — then queries the
actual ``order_intents``/``strategy_legs``/``positions`` rows to prove
``basket_id``/``leg_id`` were genuinely populated on every order this
strategy places, and that ``strategy_legs.entry_correlation_id``/
``.exit_correlation_id`` match the authoritative ``order_intents.
correlation_id`` for the exact order that produced them — never approximated
by ``(security_id, time)``.

Mirrors ``tests/integration/test_straddle_920_restart.py``'s fixture style.
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
CE2 = "SIM:NIFTY:WEEKLY:24050:CE"  # the replacement, ATM shifts with spot
STRATEGY_REF = "strategies.intraday_options.straddle_920.strategy:Straddle920Strategy"
BASKET_ID = f"{STRATEGY_ID}:{TRADING_DATE}"
CE1_LEG_ID = f"{BASKET_ID}:CE:1"
PE1_LEG_ID = f"{BASKET_ID}:PE:1"
CE2_LEG_ID = f"{BASKET_ID}:CE:2"


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


#: Run one: entry, then CE doubles (adjustment), then the replacement fills
#: — leaves CE2 + PE1 open when the tape ends (idle timeout).
_FIRST_TAPE = [
    _tick(NIFTY, 24000.0, _ts(9, 16)),
    _tick(NIFTY, 24000.0, _ts(9, 21)),  # ENTER_BASKET
    _tick(CE1, 100.0, _ts(9, 21, 5)),
    _tick(PE1, 95.0, _ts(9, 21, 10)),
    _tick(CE1, 205.0, _ts(9, 30)),  # CE doubles -> ADJUSTMENT close
    _tick(NIFTY, 24050.0, _ts(9, 31)),
    _tick(NIFTY, 24050.0, _ts(9, 36)),  # replacement queued
    _tick(CE2, 90.0, _ts(9, 36, 5)),  # replacement fills
]

#: Run two: adopts CE2 + PE1, then a tick past 15:15 forces square-off.
_SECOND_TAPE = [
    _tick(NIFTY, 24050.0, _ts(9, 41)),
    _tick(NIFTY, 24050.0, _ts(9, 46)),
    _tick(NIFTY, 24050.0, _ts(15, 16)),
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
                "leg_adjustment_multiplier": 2.0,
                "max_adjustments_per_day": 1,
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


def _leg_row(database_path: Path, leg_id: str):
    rows = _repository(database_path).load_strategy_legs(
        strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE
    )
    matches = [r for r in rows if r["leg_id"] == leg_id]
    assert len(matches) == 1, f"expected exactly one strategy_legs row for {leg_id}"
    return matches[0]


def _order_intent(database_path: Path, *, leg_id: str, side: str):
    rows = (
        Database(database_path)
        .connect()
        .execute(
            "SELECT * FROM order_intents WHERE leg_id = ? AND side = ?",
            (leg_id, side),
        )
        .fetchall()
    )
    assert len(rows) == 1, f"expected exactly one {side} order_intents row for leg_id={leg_id}"
    return rows[0]


def _position(database_path: Path, security_id: str):
    row = (
        Database(database_path)
        .connect()
        .execute(
            "SELECT * FROM positions WHERE security_id = ? AND strategy_id = ?",
            (security_id, STRATEGY_ID),
        )
        .fetchone()
    )
    assert row is not None
    return row


# ---------------------------------------------------- pre-restart scenarios
def test_entry_adjustment_and_replacement_carry_real_basket_leg_correlation(
    runtime_dirs, database_path
):
    """Scenarios 1-4: original CE entry, original PE entry, the adjusted
    leg's exit, and the replacement's entry."""
    config = _worker_config(runtime_dirs, database_path)
    result = _run(config, _FIRST_TAPE)
    assert result.exit_code == 0, result.error

    # 1. Original CE entry: order_intents carries the real basket_id/leg_id,
    # and the persisted leg's own entry_correlation_id is that exact order's
    # correlation_id — not a guess.
    ce1_entry_intent = _order_intent(database_path, leg_id=CE1_LEG_ID, side="SELL")
    assert ce1_entry_intent["basket_id"] == BASKET_ID
    ce1_leg = _leg_row(database_path, CE1_LEG_ID)
    assert ce1_leg["entry_correlation_id"] == ce1_entry_intent["correlation_id"]

    # 2. Original PE entry: same proof, independently.
    pe1_entry_intent = _order_intent(database_path, leg_id=PE1_LEG_ID, side="SELL")
    assert pe1_entry_intent["basket_id"] == BASKET_ID
    pe1_leg = _leg_row(database_path, PE1_LEG_ID)
    assert pe1_leg["entry_correlation_id"] == pe1_entry_intent["correlation_id"]

    # 3. Adjusted-leg exit: CE1's close is a distinct order_intents row
    # (side=BUY, same leg_id) whose correlation_id lands on
    # strategy_legs.exit_correlation_id for CE1 specifically.
    ce1_exit_intent = _order_intent(database_path, leg_id=CE1_LEG_ID, side="BUY")
    assert ce1_exit_intent["basket_id"] == BASKET_ID
    assert ce1_leg["state"] == "CLOSED"
    assert ce1_leg["exit_correlation_id"] == ce1_exit_intent["correlation_id"]
    assert ce1_leg["exit_correlation_id"] != ce1_leg["entry_correlation_id"]

    # 4. Replacement entry: a genuinely new leg_id (CE2), its own entry
    # order, its own correlation_id — never CE1's.
    ce2_entry_intent = _order_intent(database_path, leg_id=CE2_LEG_ID, side="SELL")
    assert ce2_entry_intent["basket_id"] == BASKET_ID
    ce2_leg = _leg_row(database_path, CE2_LEG_ID)
    assert ce2_leg["state"] == "OPEN"
    assert ce2_leg["entry_correlation_id"] == ce2_entry_intent["correlation_id"]
    assert ce2_leg["entry_correlation_id"] != ce1_leg["entry_correlation_id"]

    # positions.entry_correlation_id for CE2/PE1 (the still-open legs) must
    # also agree — this is what restart adoption below relies on.
    ce2_position = _position(database_path, CE2)
    assert ce2_position["entry_correlation_id"] == ce2_entry_intent["correlation_id"]
    pe1_position = _position(database_path, PE1)
    assert pe1_position["entry_correlation_id"] == pe1_entry_intent["correlation_id"]


# --------------------------------------------------- restart + square-off
def test_restart_adoption_and_square_off_carry_correlation_forward(runtime_dirs, database_path):
    """Scenarios 5-7: replacement exit, hard square-off, and restart
    adoption — proved together, since the square-off in run two is what
    exercises the adopted (not freshly-opened) leg's exit correlation."""
    config = _worker_config(runtime_dirs, database_path)
    first = _run(config, _FIRST_TAPE)
    assert first.exit_code == 0, first.error

    ce2_leg_before = _leg_row(database_path, CE2_LEG_ID)
    pe1_leg_before = _leg_row(database_path, PE1_LEG_ID)
    assert ce2_leg_before["state"] == "OPEN"
    assert pe1_leg_before["state"] == "OPEN"

    second = _run(config, _SECOND_TAPE)
    assert second.exit_code == 0, second.error
    assert second.trades_closed == 2, "both adopted legs must be squared off"

    # 7. Restart adoption: the adopted position's entry_correlation_id
    # (established in run one) survives into run two's OpenPosition/Trade —
    # proved by the closing Trade's entry_correlation_id still matching the
    # leg's own persisted value from before the restart, not a blank/guessed
    # one the second process invented.
    ce2_exit_intent = _order_intent(database_path, leg_id=CE2_LEG_ID, side="BUY")
    ce2_leg_after = _leg_row(database_path, CE2_LEG_ID)
    assert ce2_leg_after["entry_correlation_id"] == ce2_leg_before["entry_correlation_id"], (
        "the adopted leg's entry correlation must survive the restart unchanged"
    )

    # 6. Hard square-off + 5. "replacement exit": CE2's close in run two
    # carries the real leg_id, and its own new exit correlation_id.
    assert ce2_exit_intent["basket_id"] == BASKET_ID
    assert ce2_leg_after["state"] == "CLOSED"
    assert ce2_leg_after["exit_correlation_id"] == ce2_exit_intent["correlation_id"]
    assert ce2_leg_after["exit_reason"] == "SQUARE_OFF"

    pe1_exit_intent = _order_intent(database_path, leg_id=PE1_LEG_ID, side="BUY")
    pe1_leg_after = _leg_row(database_path, PE1_LEG_ID)
    assert pe1_exit_intent["basket_id"] == BASKET_ID
    assert pe1_leg_after["state"] == "CLOSED"
    assert pe1_leg_after["exit_correlation_id"] == pe1_exit_intent["correlation_id"]
    assert pe1_leg_after["entry_correlation_id"] == pe1_leg_before["entry_correlation_id"], (
        "PE1's entry correlation (from run one) must also survive the restart"
    )
