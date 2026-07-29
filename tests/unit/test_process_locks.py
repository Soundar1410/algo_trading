"""Process locks: the mechanism behind duplicate-worker refusal."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from common.process import DuplicateProcessError, ProcessLock, supervisor_lock, worker_lock


@pytest.fixture
def lock_dirs(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "locks", tmp_path / "pid"


def _lock(lock_dirs: tuple[Path, Path], identity: str = "intraday_options.st01") -> ProcessLock:
    return ProcessLock(identity, lock_dir=lock_dirs[0], pid_dir=lock_dirs[1])


def test_acquiring_writes_a_pid_file_naming_this_process(lock_dirs: tuple[Path, Path]):
    with _lock(lock_dirs) as lock:
        assert lock.pid_path.is_file()
        record = json.loads(lock.pid_path.read_text())
        assert record["pid"] == os.getpid()
        assert record["identity"] == "intraday_options.st01"


def test_a_second_lock_on_the_same_identity_is_refused(lock_dirs: tuple[Path, Path]):
    with _lock(lock_dirs), pytest.raises(DuplicateProcessError, match="already holds"):
        _lock(lock_dirs).acquire()


def test_the_refusal_explains_why_duplicates_are_dangerous(lock_dirs: tuple[Path, Path]):
    with _lock(lock_dirs), pytest.raises(DuplicateProcessError, match="duplicate orders"):
        _lock(lock_dirs).acquire()


def test_different_strategies_do_not_block_each_other(lock_dirs: tuple[Path, Path]):
    with _lock(lock_dirs, "intraday_options.st01"), _lock(lock_dirs, "intraday_options.st02"):
        pass  # both held simultaneously


def test_releasing_allows_a_later_acquisition(lock_dirs: tuple[Path, Path]):
    first = _lock(lock_dirs).acquire()
    first.release()
    second = _lock(lock_dirs).acquire()
    second.release()


def test_releasing_removes_our_own_pid_file(lock_dirs: tuple[Path, Path]):
    lock = _lock(lock_dirs).acquire()
    assert lock.pid_path.is_file()
    lock.release()
    assert not lock.pid_path.exists()


def test_release_is_safe_to_call_twice(lock_dirs: tuple[Path, Path]):
    lock = _lock(lock_dirs).acquire()
    lock.release()
    lock.release()


# ------------------------------------------------------------ staleness
def test_a_pid_file_naming_a_dead_process_is_not_an_owner(lock_dirs: tuple[Path, Path]):
    """PIDs are reused, so a stale file must not be believed."""
    lock = _lock(lock_dirs)
    lock.pid_path.parent.mkdir(parents=True, exist_ok=True)
    lock.pid_path.write_text(
        json.dumps(
            {
                "pid": 999_999,  # far above any live PID on macOS
                "identity": lock.identity,
                "command": "python worker.py",
                "acquired_at": "2026-07-29T09:00:00+00:00",
            }
        )
    )
    assert lock.current_owner() is None


def test_a_stale_pid_file_does_not_prevent_starting(lock_dirs: tuple[Path, Path]):
    """A crashed worker must not lock itself out of its own restart."""
    lock = _lock(lock_dirs)
    lock.pid_path.parent.mkdir(parents=True, exist_ok=True)
    lock.pid_path.write_text(
        json.dumps(
            {
                "pid": 999_999,
                "identity": lock.identity,
                "command": "x",
                "acquired_at": "2026-07-29T09:00:00+00:00",
            }
        )
    )
    lock.acquire()
    lock.release()


def test_an_unparseable_pid_file_is_treated_as_no_owner(lock_dirs: tuple[Path, Path]):
    lock = _lock(lock_dirs)
    lock.pid_path.parent.mkdir(parents=True, exist_ok=True)
    lock.pid_path.write_text("{not json")
    assert lock.current_owner() is None


def test_a_live_holder_is_reported_as_the_owner(lock_dirs: tuple[Path, Path]):
    with _lock(lock_dirs) as lock:
        owner = lock.current_owner()
        assert owner is not None
        assert owner.pid == os.getpid()


def test_release_does_not_delete_another_processs_pid_file(lock_dirs: tuple[Path, Path]):
    lock = _lock(lock_dirs).acquire()
    # Simulate the file having been replaced by a different live process.
    lock.pid_path.write_text(
        json.dumps(
            {
                "pid": 1,
                "identity": lock.identity,
                "command": "launchd",
                "acquired_at": "2026-07-29T09:00:00+00:00",
            }
        )
    )
    lock.release()
    assert lock.pid_path.is_file()


# ------------------------------------------------------------- helpers
def test_worker_and_supervisor_identities_differ(lock_dirs: tuple[Path, Path]):
    worker = worker_lock(
        runtime_id="intraday_options",
        strategy_id="st01",
        lock_dir=lock_dirs[0],
        pid_dir=lock_dirs[1],
    )
    supervisor = supervisor_lock(
        runtime_id="intraday_options", lock_dir=lock_dirs[0], pid_dir=lock_dirs[1]
    )
    assert worker.identity == "intraday_options.st01"
    assert supervisor.identity == "intraday_options.supervisor"
    assert worker.lock_path != supervisor.lock_path


def test_a_worker_lock_does_not_block_its_supervisor(lock_dirs: tuple[Path, Path]):
    with (
        worker_lock(
            runtime_id="intraday_options",
            strategy_id="st01",
            lock_dir=lock_dirs[0],
            pid_dir=lock_dirs[1],
        ),
        supervisor_lock(runtime_id="intraday_options", lock_dir=lock_dirs[0], pid_dir=lock_dirs[1]),
    ):
        pass


def test_lock_directories_are_created_on_demand(tmp_path: Path):
    lock = ProcessLock(
        "intraday_options.st01",
        lock_dir=tmp_path / "deep" / "locks",
        pid_dir=tmp_path / "deep" / "pid",
    )
    lock.acquire()
    assert lock.lock_path.parent.is_dir()
    lock.release()
