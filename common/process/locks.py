"""Exclusive locks that refuse duplicate supervisors and workers.

Two independent strategies are used together, because each alone is unsound:

* **A file lock** (``filelock``) is the real mutual exclusion. It is released by
  the operating system when the holder dies, so a crashed worker does not leave
  a lock that blocks its own restart.
* **A PID file** records *who* holds it, so an operator can see what to kill.

The spec is explicit that a PID file alone is insufficient because PIDs are
reused: a stale file naming PID 4242 proves nothing once the OS has recycled
that number onto an unrelated process. So the PID file additionally stores the
process's start time (``psutil.Process.create_time()``) and a PID is only
believed to be alive *and ours* when the running process at that PID has the
*exact same* start time — a reused PID cannot share the original process's
creation instant. The lock, not the PID file, is what actually enforces
exclusion — the PID file is diagnostic.

**Command/executable/project-root are recorded but never the decision.**
Phase 7 Part 4 found this module's own docstring had claimed command-path
validation for a while without implementing it — the previous version of this
comment said "the PID file additionally stores the process's command path and
start marker, and a PID is only believed to be alive *and ours* when those
match", but nothing ever compared ``command``. Matching on it was considered
and rejected: a ``spawn``ed worker's ``sys.argv`` is a multiprocessing
bootstrap line, not the command that started it, and moving the repository
changes every recorded path — either would make a genuinely-ours process look
foreign, which is the *inverse* incident (``stop_runtime`` refusing to signal
a supervisor that really is ours). ``create_time()`` is exact and stable
across neither of those; command/executable/project_root stay purely
diagnostic, surfaced only in the ``DuplicateProcessError`` message a human
reads.

Duplicate detection must be fast and non-blocking: a second worker should exit
immediately with a clear message, not hang waiting for a lock that a healthy
first worker will hold all day.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

import psutil
from filelock import FileLock, Timeout

from common.logging import get_logger

_log = get_logger(__name__)

#: Non-blocking by design; see the module docstring.
DEFAULT_LOCK_TIMEOUT_SECONDS = 0.1


class DuplicateProcessError(RuntimeError):
    """Raised when another live process already holds this identity's lock."""


def _discover_project_root() -> str:
    """Best-effort project root, for the diagnostic record only.

    Mirrors ``common.config.paths._discover_root_from_source``'s own
    walk-to-``pyproject.toml`` technique, duplicated rather than imported so
    this module keeps no dependency on ``common.config`` — it is used from
    contexts (a spawned worker, very early startup) where staying minimal
    matters more than sharing four lines.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return str(parent)
    return ""


@dataclass(frozen=True)
class LockOwner:
    """What a PID file records about its holder.

    ``create_time`` is the ownership decision (see the module docstring).
    ``command``, ``executable`` and ``project_root`` are diagnostics only,
    surfaced in a human-readable message — never compared.
    """

    pid: int
    identity: str
    command: str
    acquired_at: str
    create_time: float
    executable: str = ""
    project_root: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "pid": self.pid,
                "identity": self.identity,
                "command": self.command,
                "acquired_at": self.acquired_at,
                "create_time": self.create_time,
                "executable": self.executable,
                "project_root": self.project_root,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> LockOwner | None:
        try:
            data = json.loads(text)
            return cls(
                pid=int(data["pid"]),
                identity=str(data["identity"]),
                command=str(data["command"]),
                acquired_at=str(data["acquired_at"]),
                create_time=float(data["create_time"]),
                executable=str(data.get("executable", "")),
                project_root=str(data.get("project_root", "")),
            )
        except (ValueError, KeyError, TypeError):
            return None


def _verified_owner(pid: int, expected_create_time: float) -> bool:
    """True when ``pid`` names a live process that is *the same process*
    which wrote the PID file — not merely a PID that currently happens to be
    alive.

    PIDs are recycled by the OS (spec line 2518), so "a process with this PID
    exists" was never enough; ``psutil.Process.create_time()`` reads a value
    the kernel assigns once, at process creation, and a reused PID cannot
    share it. Exact equality, deliberately: the value survives a JSON
    round-trip losslessly (Python's float repr is round-trip-exact), and any
    tolerance wide enough to matter — a second, say — would let a process
    started and killed within that window of the original slip through as a
    false match, which is precisely the failure mode this check exists to
    close.
    """
    if pid <= 0:
        return False
    try:
        actual_create_time = psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        # Cannot verify identity for a process we cannot introspect. Treated
        # as not-ours, not as ours-by-default: a process this system actually
        # started runs at our own privilege level, so AccessDenied should
        # never legitimately fire against our own PID file, and defaulting
        # to "not verified" is the fail-closed direction for the one property
        # in this codebase where a wrong answer signals the wrong process.
        return False
    return actual_create_time == expected_create_time


class ProcessLock:
    """An exclusive, non-blocking lock for one process identity."""

    def __init__(
        self,
        identity: str,
        *,
        lock_dir: Path,
        pid_dir: Path,
        timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._identity = identity
        self._lock_dir = Path(lock_dir)
        self._pid_dir = Path(pid_dir)
        self._timeout = timeout_seconds
        self._lock: FileLock | None = None

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def lock_path(self) -> Path:
        return self._lock_dir / f"{self._identity}.lock"

    @property
    def pid_path(self) -> Path:
        return self._pid_dir / f"{self._identity}.pid"

    def current_owner(self) -> LockOwner | None:
        """Who the PID file claims holds this lock, if anyone verified.

        Returns None when the file is absent, unparseable, names a PID that is
        no longer running, or names a PID that is running but is *not the
        process that wrote the file* — a reused PID (see the module
        docstring). Pure read: never touches the file on disk. See
        :meth:`clear_stale_pid_file` for the mutating counterpart.
        """
        owner = self._read_pid_file()
        if owner is None:
            return None
        return owner if _verified_owner(owner.pid, owner.create_time) else None

    def _read_pid_file(self) -> LockOwner | None:
        if not self.pid_path.is_file():
            return None
        return LockOwner.from_json(self.pid_path.read_text(encoding="utf-8"))

    def clear_stale_pid_file(self) -> bool:
        """Remove the PID file if — and only if — it does not verify.

        Spec step 6's second clause ("remove PID files only during controlled
        shutdown or verified stale cleanup") has always had the first half;
        this is the second. "Verified" means the same thing
        :meth:`current_owner` means it: the recorded PID is dead, or it is
        alive but is not the process that wrote the file. A file naming a
        live, verified owner is never touched — this is what makes it safe to
        call unconditionally, including right before :meth:`acquire` takes
        the lock: a genuinely-held lock's PID file always verifies, so this
        can only ever clean up an orphan a ``SIGKILL`` left behind, never a
        live holder's own record.

        Returns whether a file was actually removed.
        """
        owner = self._read_pid_file()
        if owner is not None and _verified_owner(owner.pid, owner.create_time):
            return False
        if not self.pid_path.is_file():
            return False
        self.pid_path.unlink(missing_ok=True)
        _log.info(
            "removed a stale PID file identity=%s (%s)",
            self._identity,
            "unparseable" if owner is None else f"pid {owner.pid} not verified",
        )
        return True

    def acquire(self) -> ProcessLock:
        """Take the lock or raise :class:`DuplicateProcessError` immediately."""
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        self._pid_dir.mkdir(parents=True, exist_ok=True)

        # A crash (SIGKILL) skips release() entirely and orphans the PID
        # file, even though the OS already released the flock itself. Swept
        # here, before contending for the lock, so the file on disk never
        # misrepresents a dead process as this identity's owner — safe
        # unconditionally, see this method's own docstring.
        self.clear_stale_pid_file()

        lock = FileLock(str(self.lock_path))
        try:
            lock.acquire(timeout=self._timeout)
        except Timeout as exc:
            owner = self.current_owner()
            detail = (
                f" held by pid {owner.pid} ({owner.command}) since {owner.acquired_at}"
                if owner
                else ""
            )
            raise DuplicateProcessError(
                f"Refusing to start: another process already holds {self._identity!r}{detail}. "
                "Duplicate workers would place duplicate orders against the same strategy."
            ) from exc

        self._lock = lock
        self._write_pid_file()
        _log.info("acquired process lock identity=%s pid=%d", self._identity, os.getpid())
        return self

    def _write_pid_file(self) -> None:
        pid = os.getpid()
        try:
            create_time = psutil.Process(pid).create_time()
        except psutil.NoSuchProcess:  # pragma: no cover - our own live PID
            create_time = 0.0
        owner = LockOwner(
            pid=pid,
            identity=self._identity,
            command=" ".join(sys.argv) or sys.executable,
            acquired_at=datetime.now(UTC).isoformat(),
            create_time=create_time,
            executable=sys.executable,
            project_root=_discover_project_root(),
        )
        # Atomic replace so a reader never sees a half-written record.
        temporary = self.pid_path.with_suffix(".pid.tmp")
        temporary.write_text(owner.to_json(), encoding="utf-8")
        temporary.replace(self.pid_path)

    def release(self) -> None:
        """Release and clean up. Safe to call more than once."""
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        # Only remove a PID file we still own — never another process's.
        if self.pid_path.is_file():
            owner = LockOwner.from_json(self.pid_path.read_text(encoding="utf-8"))
            if owner is not None and owner.pid == os.getpid():
                self.pid_path.unlink(missing_ok=True)

    def __enter__(self) -> ProcessLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def worker_lock(
    *,
    runtime_id: str,
    strategy_id: str,
    lock_dir: Path,
    pid_dir: Path,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> ProcessLock:
    """One lock per enabled strategy — the duplicate-worker guard."""
    return ProcessLock(
        f"{runtime_id}.{strategy_id}",
        lock_dir=lock_dir,
        pid_dir=pid_dir,
        timeout_seconds=timeout_seconds,
    )


def supervisor_lock(
    *,
    runtime_id: str,
    lock_dir: Path,
    pid_dir: Path,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> ProcessLock:
    """One lock per strategy group."""
    return ProcessLock(
        f"{runtime_id}.supervisor",
        lock_dir=lock_dir,
        pid_dir=pid_dir,
        timeout_seconds=timeout_seconds,
    )
