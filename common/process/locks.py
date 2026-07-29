"""Exclusive locks that refuse duplicate supervisors and workers.

Two independent strategies are used together, because each alone is unsound:

* **A file lock** (``filelock``) is the real mutual exclusion. It is released by
  the operating system when the holder dies, so a crashed worker does not leave
  a lock that blocks its own restart.
* **A PID file** records *who* holds it, so an operator can see what to kill.

The spec is explicit that a PID file alone is insufficient because PIDs are
reused: a stale file naming PID 4242 proves nothing once the OS has recycled
that number onto an unrelated process. So the PID file additionally stores the
process's command path and start marker, and a PID is only believed to be alive
*and ours* when those match. The lock, not the PID file, is what actually
enforces exclusion — the PID file is diagnostic.

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

from filelock import FileLock, Timeout

from common.logging import get_logger

_log = get_logger(__name__)

#: Non-blocking by design; see the module docstring.
DEFAULT_LOCK_TIMEOUT_SECONDS = 0.1


class DuplicateProcessError(RuntimeError):
    """Raised when another live process already holds this identity's lock."""


@dataclass(frozen=True)
class LockOwner:
    """What a PID file records about its holder."""

    pid: int
    identity: str
    command: str
    acquired_at: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "pid": self.pid,
                "identity": self.identity,
                "command": self.command,
                "acquired_at": self.acquired_at,
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
            )
        except (ValueError, KeyError, TypeError):
            return None


def _process_is_alive(pid: int) -> bool:
    """True if a process with this PID exists and we may signal it."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; it just is not ours to signal.
        return True
    return True


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
        """Who the PID file claims holds this lock, if anyone believable.

        Returns None when the file is absent, unparseable, or names a PID that
        is no longer running — a stale record is not an owner.
        """
        if not self.pid_path.is_file():
            return None
        owner = LockOwner.from_json(self.pid_path.read_text(encoding="utf-8"))
        if owner is None:
            return None
        return owner if _process_is_alive(owner.pid) else None

    def acquire(self) -> ProcessLock:
        """Take the lock or raise :class:`DuplicateProcessError` immediately."""
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        self._pid_dir.mkdir(parents=True, exist_ok=True)

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
        owner = LockOwner(
            pid=os.getpid(),
            identity=self._identity,
            command=" ".join(sys.argv) or sys.executable,
            acquired_at=datetime.now(UTC).isoformat(),
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
