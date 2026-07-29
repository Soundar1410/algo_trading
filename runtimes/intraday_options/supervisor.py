"""The intraday options strategy-group supervisor.

Owns the shared feed and the worker registry. One supervisor per strategy group,
one shared feed inside it, one child process per enabled strategy — never one
feed per strategy.

The supervisor deliberately does **not** touch the operational database's
trading tables. Workers own their own state, and a supervisor that also wrote
positions would reintroduce the shared-mutable-state problem that
process-per-strategy exists to avoid. It runs migrations once at startup (so
workers race nothing) and otherwise stays out of the way.

The feed is driven on the supervisor's own thread after the workers are spawned,
because the recorded adapter replays synchronously and the live adapter blocks
in ``run_forever``. Either way the parent is the only process holding the socket.
"""

from __future__ import annotations

import multiprocessing as mp
from dataclasses import dataclass, field
from pathlib import Path

from common.config.models import ExecutionMode
from common.feed import SharedFeedHub
from common.feed.hub import WorkerChannel, build_channel
from common.logging import get_logger
from common.market_data.adapter import MarketFeedAdapter
from common.persistence import Database, MigrationRunner
from common.process import DuplicateProcessError, supervisor_lock

from .worker import WorkerConfig, run_worker

_log = get_logger(__name__)

#: Queue depth per worker. Roughly an hour of one-minute candles: deep enough
#: that a briefly busy worker loses nothing, shallow enough that a wedged one
#: is detected rather than accumulating a day of stale bars.
DEFAULT_QUEUE_DEPTH = 64


@dataclass
class SupervisorConfig:
    """Group-level configuration."""

    runtime_id: str
    database_path: Path
    lock_dir: Path
    pid_dir: Path
    log_dir: Path
    candle_interval_seconds: int = 60
    queue_depth: int = DEFAULT_QUEUE_DEPTH


@dataclass
class SupervisorResult:
    """What one supervised run produced."""

    workers_started: int = 0
    candles_published: int = 0
    ticks_received: int = 0
    worker_exit_codes: dict[str, int] = field(default_factory=dict)
    dropped_events: dict[str, int] = field(default_factory=dict)


class IntradayOptionsSupervisor:
    """Runs one shared feed and a set of paper workers to completion."""

    def __init__(self, config: SupervisorConfig, adapter: MarketFeedAdapter) -> None:
        self._config = config
        self._hub = SharedFeedHub(adapter, interval_seconds=config.candle_interval_seconds)
        self._workers: list[tuple[WorkerConfig, WorkerChannel]] = []
        self._processes: dict[str, mp.process.BaseProcess] = {}

    @property
    def hub(self) -> SharedFeedHub:
        return self._hub

    def add_worker(self, worker_config: WorkerConfig) -> WorkerChannel:
        """Register a strategy. Its queue is created here, before any spawn."""
        if worker_config.execution_mode is not ExecutionMode.PAPER:
            # Phase 1 has no live path at all. The broker factory would refuse
            # anyway; refusing here too means the supervisor never even spawns
            # a process that is guaranteed to fail.
            raise ValueError(
                f"Strategy {worker_config.strategy_id!r} requests "
                f"{worker_config.execution_mode.value} mode; Phase 1 is paper-only "
                "and live execution is not implemented."
            )
        channel = build_channel(
            worker_config.strategy_id,
            [worker_config.security_id],
            max_depth=self._config.queue_depth,
        )
        self._hub.register(channel)
        self._workers.append((worker_config, channel))
        return channel

    def run(self, *, join_timeout: float = 30.0) -> SupervisorResult:
        """Migrate, spawn workers, drive the feed, then shut down cleanly."""
        result = SupervisorResult()
        lock = supervisor_lock(
            runtime_id=self._config.runtime_id,
            lock_dir=self._config.lock_dir,
            pid_dir=self._config.pid_dir,
        )
        try:
            lock.acquire()
        except DuplicateProcessError:
            _log.error("another supervisor already owns %s", self._config.runtime_id)
            raise

        try:
            # Migrate once, in the parent, before any worker opens the file.
            database = Database(self._config.database_path)
            MigrationRunner(database).run_pending()
            database.close()

            context = mp.get_context("spawn")
            for worker_config, channel in self._workers:
                process = context.Process(
                    target=run_worker,
                    args=(worker_config, channel.queue.raw),
                    name=f"{self._config.runtime_id}:{worker_config.strategy_id}",
                    daemon=False,
                )
                process.start()
                self._processes[worker_config.strategy_id] = process
                result.workers_started += 1
                _log.info(
                    "spawned worker strategy_id=%s pid=%s",
                    worker_config.strategy_id,
                    process.pid,
                )

            self._hub.start()
            self._hub.stop()

            result.ticks_received = self._hub.tick_count
            result.candles_published = self._hub.candle_count

            # Sentinel per worker: tells a blocked consumer to stop waiting
            # rather than relying on its idle timeout.
            for _, channel in self._workers:
                channel.queue.publish(None)

            for strategy_id, worker_process in self._processes.items():
                worker_process.join(timeout=join_timeout)
                if worker_process.is_alive():
                    _log.warning("worker %s did not exit; terminating", strategy_id)
                    worker_process.terminate()
                    worker_process.join(timeout=5.0)
                result.worker_exit_codes[strategy_id] = worker_process.exitcode or 0

            for _, channel in self._workers:
                result.dropped_events[channel.strategy_id] = channel.queue.dropped

            return result
        finally:
            lock.release()
