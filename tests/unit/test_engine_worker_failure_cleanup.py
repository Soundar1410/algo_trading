"""A final reconciliation failure must not erase the primary engine failure."""

from __future__ import annotations

import queue
from pathlib import Path

from common.config.models import ExecutionMode
from common.execution import ExecutionRepository
from common.health import HeartbeatWriter
from common.notifications import RecordingNotifier, SafeNotifier
from common.persistence import Database, MigrationRunner
from runtimes.intraday_options import engine_worker
from runtimes.intraday_options.worker import EngineWorkerConfig, WorkerConfig, WorkerOutcome


def test_primary_and_live_cleanup_failures_are_both_persisted(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "runtime.db")
    MigrationRunner(database).run_pending()
    repository = ExecutionRepository(database)
    config = WorkerConfig(
        runtime_id="intraday_options",
        strategy_id="st01",
        security_id="13",
        instrument="NIFTY",
        database_path=database.path,
        lock_dir=tmp_path / "locks",
        pid_dir=tmp_path / "pids",
        log_dir=tmp_path / "logs",
        trading_date="2026-08-13",
        execution_mode=ExecutionMode.LIVE,
    )
    session = repository.open_session(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        process_role="worker",
        pid=123,
    )
    heartbeat = HeartbeatWriter(
        repository,
        session_id=session.id,
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
    )

    def close_live() -> None:
        raise RuntimeError("final reconciliation failed")

    monkeypatch.setattr(
        engine_worker,
        "_build",
        lambda *args, **kwargs: (
            object(),
            object(),
            object(),
            object(),
            object(),
            close_live,
        ),
    )

    def drive_failure(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("engine processing failed")

    monkeypatch.setattr(engine_worker, "_drive", drive_failure)
    outcome = engine_worker.run_engine(
        config,
        EngineWorkerConfig(strategy_ref="unused:Unused"),
        repository=repository,
        session_id=session.id,
        heartbeat=heartbeat,
        notifier=SafeNotifier(RecordingNotifier()),
        outcome=WorkerOutcome(),
        candle_queue=queue.Queue(),
        tick_queue=queue.Queue(),
        control_queue=queue.Queue(),
    )

    assert outcome.exit_code == 1
    assert "engine processing failed" in (outcome.error or "")
    assert "final reconciliation failed" in (outcome.error or "")
    errors = (
        database.connect().execute("SELECT component, message FROM errors ORDER BY id").fetchall()
    )
    assert [row["component"] for row in errors] == ["engine.live_cleanup", "engine"]
    assert "engine processing failed" in errors[1]["message"]
    assert "final reconciliation failed" in errors[1]["message"]
    heartbeat_row = (
        database.connect()
        .execute("SELECT health_state FROM runtime_heartbeats ORDER BY id DESC LIMIT 1")
        .fetchone()
    )
    assert heartbeat_row["health_state"] == "FAILED"
