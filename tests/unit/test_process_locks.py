"""Process locks: the mechanism behind duplicate-worker refusal."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psutil
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
                "create_time": 1753776000.0,
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
                "create_time": 1753776000.0,
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


# ------------------------------------------------------ verified stale cleanup
def test_clear_stale_pid_file_removes_an_orphan_naming_a_dead_pid(lock_dirs: tuple[Path, Path]):
    """Spec step 6's second clause: a SIGKILL skips release() entirely and
    orphans the PID file forever, even though the OS already released the
    flock. This is the cleanup that step has never had until now."""
    lock = _lock(lock_dirs)
    lock.pid_path.parent.mkdir(parents=True, exist_ok=True)
    lock.pid_path.write_text(
        json.dumps(
            {
                "pid": 999_999,
                "identity": lock.identity,
                "command": "python worker.py",
                "acquired_at": "2026-07-29T09:00:00+00:00",
                "create_time": 1753776000.0,
            }
        )
    )
    assert lock.clear_stale_pid_file() is True
    assert not lock.pid_path.exists()


def test_clear_stale_pid_file_removes_an_orphan_from_a_reused_pid(lock_dirs: tuple[Path, Path]):
    """The other half of "verified": alive but not ours is just as stale as
    dead — a PID recycled onto an unrelated live process must be cleared,
    not mistaken for a live owner because *something* answers to that PID."""
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lock = _lock(lock_dirs)
        lock.pid_path.parent.mkdir(parents=True, exist_ok=True)
        lock.pid_path.write_text(
            json.dumps(
                {
                    "pid": sleeper.pid,
                    "identity": lock.identity,
                    "command": "python worker.py",
                    "acquired_at": "2026-07-29T09:00:00+00:00",
                    "create_time": 1753776000.0,  # not sleeper's real create_time
                }
            )
        )
        assert lock.clear_stale_pid_file() is True
        assert not lock.pid_path.exists()
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)


def test_clear_stale_pid_file_never_touches_a_live_verified_owner(lock_dirs: tuple[Path, Path]):
    """The property that makes it safe to call unconditionally: a file
    naming a live, verified owner is left alone."""
    with _lock(lock_dirs) as lock:
        assert lock.clear_stale_pid_file() is False
        assert lock.pid_path.is_file()


def test_clear_stale_pid_file_is_a_no_op_when_no_file_exists(lock_dirs: tuple[Path, Path]):
    lock = _lock(lock_dirs)
    assert lock.clear_stale_pid_file() is False


def test_clear_stale_pid_file_removes_an_unparseable_file(lock_dirs: tuple[Path, Path]):
    lock = _lock(lock_dirs)
    lock.pid_path.parent.mkdir(parents=True, exist_ok=True)
    lock.pid_path.write_text("{not json")
    assert lock.clear_stale_pid_file() is True
    assert not lock.pid_path.exists()


def test_acquire_sweeps_its_own_orphaned_pid_file_before_taking_the_lock(
    lock_dirs: tuple[Path, Path],
):
    """The integration of the two: a crashed worker's orphan must not
    survive the very next restart of the same identity, even though nothing
    else in this test ever calls clear_stale_pid_file directly."""
    lock = _lock(lock_dirs)
    lock.pid_path.parent.mkdir(parents=True, exist_ok=True)
    lock.pid_path.write_text(
        json.dumps(
            {
                "pid": 999_999,
                "identity": lock.identity,
                "command": "python worker.py",
                "acquired_at": "2026-07-29T09:00:00+00:00",
                "create_time": 1753776000.0,
            }
        )
    )
    lock.acquire()
    try:
        record = json.loads(lock.pid_path.read_text())
        assert record["pid"] == os.getpid()
    finally:
        lock.release()


def test_a_live_holder_is_reported_as_the_owner(lock_dirs: tuple[Path, Path]):
    with _lock(lock_dirs) as lock:
        owner = lock.current_owner()
        assert owner is not None
        assert owner.pid == os.getpid()


def test_a_live_process_that_is_not_ours_is_not_an_owner(lock_dirs: tuple[Path, Path]):
    """PID reuse: the recorded PID is alive, signalable, and a different program.

    Phase 7 Part 4's fail-first standard, step 1. This is the exact scenario
    spec line 2518 warns about — "PIDs can be reused" — and the one property
    in this whole plan where a wrong implementation causes a real incident:
    ``stop_runtime`` signalling an unrelated live process because a PID file
    named it after the process it once belonged to died and the number was
    handed to something else.

    The child must be spawned and genuinely **signalable**, not ``pid: 1``.
    ``_process_is_alive`` (locks.py) treats ``PermissionError`` from
    ``os.kill`` as "alive" deliberately — "it exists; it just is not ours to
    signal" — so a ``pid: 1`` fixture would exercise the defect without
    reproducing the incident: a real ``SIGTERM`` to launchd fails with EPERM
    and harms nothing. Only a live process this test can actually signal
    reproduces the real hazard, which is exactly why
    ``test_release_does_not_delete_another_processs_pid_file`` below is
    allowed to keep using ``pid: 1`` — that test needs no signal permission,
    only file identity, a different property.

    Run against unmodified ``locks.py`` (before psutil-based validation was
    added) this failed: ``current_owner()`` returned a populated
    ``LockOwner`` naming the sleeper process, because liveness
    (``_process_is_alive``) is the only thing ``current_owner()`` ever
    consulted — recorded in the runbook as D76's evidence.
    """
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lock = _lock(lock_dirs, "intraday_options.supervisor")
        lock.pid_path.parent.mkdir(parents=True, exist_ok=True)
        lock.pid_path.write_text(
            json.dumps(
                {
                    "pid": sleeper.pid,  # alive, and ours to signal — not launchd
                    "identity": "intraday_options.supervisor",
                    "command": ".venv/bin/python -m runtimes.intraday_options",
                    "acquired_at": "2026-08-07T09:00:00+00:00",
                }
            )
        )
        assert lock.current_owner() is None
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)


def test_a_genuinely_live_holder_is_still_recognised(lock_dirs: tuple[Path, Path]):
    """The paired over-strictness guard, fail-first standard step 2.

    Must pass both before and after the ownership-validation fix below, so
    tightening the check cannot swing the other way and make ``stop_runtime``
    refuse a supervisor that genuinely is ours — the inverse incident.
    Covers the case that would break a naive command-string match: a
    ``spawn``ed child's ``sys.argv`` is a multiprocessing bootstrap line, not
    the command that started it, and the module docstring already names a
    repo move as another way a recorded command/path goes stale. Process
    start time (what the fix actually keys on) is unaffected by either.
    """
    with _lock(lock_dirs) as lock:
        owner = lock.current_owner()
        assert owner is not None
        assert owner.pid == os.getpid()

    # A spawned child's argv does not look like the command that started it.
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
    )
    try:
        # The real value the fix actually keys on — a fixture using the
        # child's true create_time, the same way its own _write_pid_file
        # would have recorded it.
        try:
            real_create_time = psutil.Process(child.pid).create_time()
        except psutil.NoSuchProcess:
            pytest.skip(
                "the execution environment immediately reaped the child; "
                "ordinary macOS/Linux hosts still run this ownership assertion"
            )
        spawned_lock = _lock(lock_dirs, "intraday_options.spawned")
        spawned_lock.pid_path.parent.mkdir(parents=True, exist_ok=True)
        spawned_lock.pid_path.write_text(
            json.dumps(
                {
                    "pid": child.pid,
                    "identity": "intraday_options.spawned",
                    # What a spawned multiprocessing child's argv looks like —
                    # nothing like ".venv/bin/python -m runtimes...". Proves
                    # the fix does not key on this, only on create_time below.
                    "command": (
                        "-c from multiprocessing.spawn import spawn_main; "
                        "spawn_main(tracker_fd=6, pipe_handle=8)"
                    ),
                    "acquired_at": "2026-08-07T09:00:00+00:00",
                    "create_time": real_create_time,
                }
            )
        )
        owner = spawned_lock.current_owner()
        assert owner is not None
        assert owner.pid == child.pid
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_release_does_not_delete_another_processs_pid_file(lock_dirs: tuple[Path, Path]):
    lock = _lock(lock_dirs).acquire()
    # Simulate the file having been replaced by a different live process.
    # release() decides this purely on pid == os.getpid(), never on
    # create_time — the file naming a PID that is not ours is enough, with
    # no need to actually verify pid 1's ownership (which release() never
    # attempts; only current_owner()/clear_stale_pid_file() do).
    lock.pid_path.write_text(
        json.dumps(
            {
                "pid": 1,
                "identity": lock.identity,
                "command": "launchd",
                "acquired_at": "2026-07-29T09:00:00+00:00",
                "create_time": 0.0,
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


# --------------------------------------------------------- mode exclusion
def test_worker_lock_identity_has_no_room_for_mode(lock_dirs: tuple[Path, Path]):
    """Regression guard for spec line 2520 ("prevent a second worker for the
    same strategy ID even when one configuration says paper and another says
    live"). ``worker_lock`` takes no ``mode`` parameter at all, so this is true
    by construction today — this test pins that construction against a future
    "helpful" change that folds ``execution_mode`` into the identity string for
    consistency with the mode-separated persistence tables. If it ever did, a
    paper worker and a live worker for the same ``strategy_id`` would stop
    colliding and both could run at once.
    """
    identity = worker_lock(
        runtime_id="intraday_options",
        strategy_id="st01",
        lock_dir=lock_dirs[0],
        pid_dir=lock_dirs[1],
    ).identity
    assert identity == "intraday_options.st01"
    assert "paper" not in identity
    assert "live" not in identity


def test_two_worker_locks_for_the_same_strategy_id_collide_regardless_of_caller_intent(
    lock_dirs: tuple[Path, Path],
):
    """Simulates "one config says paper, one says live": ``worker_lock`` has no
    mode input to vary, so the two calls a paper-mode and a live-mode caller
    would each make are identical, and the second collides with the first.
    """
    first = worker_lock(
        runtime_id="intraday_options",
        strategy_id="st01",
        lock_dir=lock_dirs[0],
        pid_dir=lock_dirs[1],
    ).acquire()
    try:
        with pytest.raises(DuplicateProcessError, match="already holds"):
            worker_lock(
                runtime_id="intraday_options",
                strategy_id="st01",
                lock_dir=lock_dirs[0],
                pid_dir=lock_dirs[1],
            ).acquire()
    finally:
        first.release()


def test_lock_directories_are_created_on_demand(tmp_path: Path):
    lock = ProcessLock(
        "intraday_options.st01",
        lock_dir=tmp_path / "deep" / "locks",
        pid_dir=tmp_path / "deep" / "pid",
    )
    lock.acquire()
    assert lock.lock_path.parent.is_dir()
    lock.release()
