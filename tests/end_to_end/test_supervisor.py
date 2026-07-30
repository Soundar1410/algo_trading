"""The supervisor: real spawned worker processes over real IPC queues."""

from __future__ import annotations

from pathlib import Path

import pytest

from common.config.models import ExecutionMode
from common.market_data import RecordedFeedAdapter, load_tick_tape
from common.persistence import Database
from runtimes.intraday_options.supervisor import (
    IntradayOptionsSupervisor,
    SupervisorConfig,
)
from runtimes.intraday_options.worker import WorkerConfig

RUNTIME_ID = "intraday_options"
SECURITY_ID = "99926000"
TRADING_DATE = "2026-07-29"


@pytest.fixture
def supervisor_config(runtime_dirs: dict[str, Path], database_path: Path) -> SupervisorConfig:
    return SupervisorConfig(
        runtime_id=RUNTIME_ID,
        database_path=database_path,
        lock_dir=runtime_dirs["lock_dir"],
        pid_dir=runtime_dirs["pid_dir"],
        log_dir=runtime_dirs["log_dir"],
    )


def _worker(config: SupervisorConfig, strategy_id: str, **overrides) -> WorkerConfig:
    return WorkerConfig(
        runtime_id=RUNTIME_ID,
        strategy_id=strategy_id,
        security_id=SECURITY_ID,
        instrument="NIFTY",
        database_path=config.database_path,
        lock_dir=config.lock_dir,
        pid_dir=config.pid_dir,
        log_dir=config.log_dir,
        trading_date=TRADING_DATE,
        **overrides,
    )


def test_the_supervisor_spawns_a_worker_that_trades(
    supervisor_config, tick_tape_path, database_path
):
    """One live-shaped run: feed → hub → IPC queue → child process → SQLite."""
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    supervisor.add_worker(_worker(supervisor_config, "skelfix"))

    result = supervisor.run()

    assert result.workers_started == 1
    assert result.ticks_received == 24
    assert result.candles_published == 6
    assert result.worker_exit_codes["skelfix"] == 0

    conn = Database(database_path).connect()
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1


def test_two_workers_receive_identical_bars(supervisor_config, tick_tape_path, database_path):
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    supervisor.add_worker(_worker(supervisor_config, "skelone"))
    supervisor.add_worker(_worker(supervisor_config, "skeltwo"))

    result = supervisor.run()

    assert result.workers_started == 2
    assert set(result.worker_exit_codes.values()) == {0}

    conn = Database(database_path).connect()
    rows = conn.execute(
        """
        SELECT strategy_id, candle_end_at, candle_close FROM signals
        WHERE side = 'BUY' ORDER BY strategy_id
        """
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["candle_end_at"] == rows[1]["candle_end_at"]
    assert rows[0]["candle_close"] == rows[1]["candle_close"]


def test_no_events_are_dropped_at_normal_volume(supervisor_config, tick_tape_path):
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    supervisor.add_worker(_worker(supervisor_config, "skelfix"))

    result = supervisor.run()
    assert result.dropped_events["skelfix"] == 0


def test_a_run_that_ends_on_its_own_is_not_reported_as_signalled(supervisor_config, tick_tape_path):
    """An exhausted tape is not a shutdown signal, and must not be logged as one.

    Worth its own test because the tempting implementation — "the feed thread is
    still alive, so we must have been signalled" — is wrong: a thread that has
    just finished its work is briefly still alive while it unwinds, so an ordinary
    end-of-tape run would report itself as signalled about as often as the timing
    happened to fall that way.
    """
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    supervisor.add_worker(_worker(supervisor_config, "skelfix"))

    result = supervisor.run()

    assert result.stopped_by_signal is False
    assert result.clean_feed_shutdown is True


def test_the_supervisor_refuses_a_live_mode_worker(supervisor_config, tick_tape_path):
    """Phase 1 is paper-only; a live worker is never even spawned."""
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)

    with pytest.raises(ValueError, match="paper-only"):
        supervisor.add_worker(
            _worker(supervisor_config, "livestrat", execution_mode=ExecutionMode.LIVE)
        )


def test_the_database_is_consistent_after_a_supervised_run(
    supervisor_config, tick_tape_path, database_path
):
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    supervisor.add_worker(_worker(supervisor_config, "skelfix"))
    supervisor.run()

    database = Database(database_path)
    assert database.integrity_check() == []
    assert database.foreign_key_check() == []
    assert database.journal_mode() == "wal"
