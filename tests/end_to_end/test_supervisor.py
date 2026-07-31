"""The supervisor: real spawned worker processes over real IPC queues."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta
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


# ------------------------------------------------- tick channel (Part 2b-ii-A)
def test_a_worker_gets_no_tick_channel_unless_it_asks(supervisor_config, tick_tape_path):
    """Opt-in: the default run is exactly the run it was before this part."""
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    channel = supervisor.add_worker(_worker(supervisor_config, "skelfix"))

    assert channel.tick_queue is None
    assert supervisor.control_queue("skelfix") is None

    result = supervisor.run()

    assert result.worker_exit_codes["skelfix"] == 0
    assert supervisor.hub.ticks_published == 0


def test_an_opted_in_worker_gets_a_tick_queue_and_a_control_queue(
    supervisor_config, tick_tape_path
):
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    channel = supervisor.add_worker(_worker(supervisor_config, "skelfix"), tick_channel=True)

    assert channel.tick_queue is not None
    assert channel.tick_queue.max_depth == supervisor_config.tick_queue_depth
    assert supervisor.control_queue("skelfix") is not None

    result = supervisor.run()

    assert result.worker_exit_codes["skelfix"] == 0
    # Every tick on the tape reached the tick channel as well as the aggregator.
    assert supervisor.hub.ticks_published == 24


class _LiveishAdapter:
    """Replays a tape, then keeps the underlying ticking for a short while.

    The recorded tape is 24 ticks and finishes in microseconds, so a supervisor
    driven by it never completes a single heartbeat-loop iteration — there is no
    live feed left for a runtime subscription to be applied against. That is an
    artefact of an instant tape, not of the design: during market hours frames
    arrive continuously. This double supplies that continuity, bounded so a
    failure is an assertion rather than a hang.
    """

    def __init__(self, ticks, *, extra: int = 300, interval: float = 0.01) -> None:
        self._ticks = list(ticks)
        self._extra = extra
        self._interval = interval
        self._subscribed: set[str] = set()
        self._running = False
        self._stop = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def subscribed(self) -> frozenset[str]:
        return frozenset(self._subscribed)

    def subscribe(self, security_ids) -> None:
        self._subscribed.update(security_ids)

    def request_stop(self) -> None:
        self._stop.set()
        self._running = False

    def stop(self) -> None:
        self.request_stop()

    def start(self, on_tick) -> None:
        self._running = True
        for tick in self._ticks:
            if not self._running:
                break
            if tick.security_id in self._subscribed:
                on_tick(tick)

        last = self._ticks[-1]
        for i in range(1, self._extra + 1):
            if self._stop.wait(timeout=self._interval):
                break
            on_tick(
                replace(
                    last,
                    exchange_time=last.exchange_time + timedelta(microseconds=i),
                )
            )
        self._running = False


def test_a_subscription_request_on_the_control_queue_reaches_the_hub(
    supervisor_config, tick_tape_path
):
    """The upstream hop: child → control queue → supervisor → hub → feed thread."""
    adapter = _LiveishAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    channel = supervisor.add_worker(_worker(supervisor_config, "skelfix"), tick_channel=True)

    control = supervisor.control_queue("skelfix")
    assert control is not None
    control.put("45678")

    supervisor.run()

    assert "45678" in channel.dynamic_ids
    assert "45678" in adapter.subscribed
    assert supervisor.hub.subscriptions_applied == 1


def test_a_malformed_subscription_request_does_not_kill_the_group(
    supervisor_config, tick_tape_path
):
    """A bad child is the child's problem, not the whole runtime's."""
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    supervisor.add_worker(_worker(supervisor_config, "skelfix"), tick_channel=True)

    control = supervisor.control_queue("skelfix")
    assert control is not None
    control.put(None)
    control.put(12345)
    control.put("")

    result = supervisor.run()

    assert result.worker_exit_codes["skelfix"] == 0
    assert supervisor.hub.subscriptions_applied == 0


def test_undelivered_ticks_do_not_wedge_the_supervisors_exit(supervisor_config, tick_tape_path):
    """A defect found in Part 2b-ii-A, and the reason ``_release_queues`` exists.

    A ``multiprocessing.Queue`` joins its feeder thread at interpreter exit, so a
    producer holding undelivered events behind a full pipe never exits — measured
    at ~65 KB, which the tick channel reaches in a few hundred ticks. The candle
    channel never came close; the tick channel carries ~100x the volume, and in
    Part 2b-ii-A nothing consumes it yet.

    Pre-fix this run hung with no error and no exit code. There is no assertion
    that can express "did not hang" more directly than reaching the end.
    """
    adapter = _LiveishAdapter(load_tick_tape(tick_tape_path), extra=600, interval=0.001)
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    channel = supervisor.add_worker(_worker(supervisor_config, "skelfix"), tick_channel=True)

    result = supervisor.run()

    assert channel.tick_queue is not None
    # Well past the ~65 KB that wedged it, and none of it was consumed.
    assert channel.tick_queue.published > 400
    assert result.worker_exit_codes["skelfix"] == 0
