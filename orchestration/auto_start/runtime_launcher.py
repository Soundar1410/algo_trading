"""Spawns the enabled runtime groups and then *stays with them*.

Spawning and exiting would be wrong in four separate ways, all of which bite
only in production:

1. The controller runs under ``caffeinate -i -s``, whose sleep protection lasts
   exactly as long as the process it wraps. A controller that exits at 09:00:30
   stops protecting a session that runs to 15:15.
2. ``launchd`` reasonably treats a job whose main process has exited as
   finished, and its descendants as abandoned.
3. ``SIGTERM`` sent to the job has nothing left to propagate it to the children.
4. ``Popen`` returning means the *fork* worked. It says nothing about whether
   the runtime started. A supervisor that dies two seconds later on a bad
   migration would be reported as a successful start.

So the launcher owns its children: it verifies each one against the runtime's
own ``supervisor_lock`` before calling it started, blocks until they finish,
forwards shutdown signals with a bounded grace period, and reports what
actually happened.

**It never restarts anything.** Bounded restart is
:mod:`orchestration.process_control.supervised_launch`'s job, one level down,
and a runtime that has exited deliberately — an operator stop, a safety
shutdown, the end of the session — must stay exited. A supervisor loop here
would silently defeat both.
"""

from __future__ import annotations

import contextlib
import signal
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from common.config import ProjectPaths
from common.logging import get_logger
from common.process import supervisor_lock
from common.process.locks import ProcessLock

_log = get_logger(__name__)

#: How often the handshake and supervision loops look at the world. Small
#: enough to be responsive, large enough never to spin.
DEFAULT_POLL_INTERVAL_SECONDS = 1.0


class ProcessHandle(Protocol):
    """The slice of :class:`subprocess.Popen` this module uses.

    A protocol rather than the concrete class so tests drive the whole
    ownership lifecycle — handshake, supervision, signal propagation,
    escalation — without creating a real process.
    """

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def send_signal(self, sig: int) -> None: ...

    def kill(self) -> None: ...


Spawn = Callable[[Sequence[str]], ProcessHandle]


@dataclass
class LaunchResult:
    """What happened to one runtime group."""

    runtime_id: str
    started: bool
    detail: str
    pid: int | None = None
    #: Set once the child has exited. ``None`` while it is still running.
    exit_code: int | None = None
    #: True when a healthy instance was already running and we deferred to it.
    already_running: bool = False


@dataclass
class _Child:
    runtime_id: str
    handle: ProcessHandle
    result: LaunchResult
    signalled_at: datetime | None = field(default=None)


class RuntimeLauncher:
    """Owns one process per enabled runtime for the life of the session."""

    def __init__(
        self,
        *,
        python_bin: Path,
        paths: ProjectPaths,
        config_root: Path,
        clock: Callable[[], datetime],
        stop_event: threading.Event,
        handshake_seconds: float,
        shutdown_grace_seconds: float,
        spawn: Spawn | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._python_bin = python_bin
        self._paths = paths
        self._config_root = config_root
        self._clock = clock
        self._stop_event = stop_event
        self._handshake_seconds = handshake_seconds
        self._shutdown_grace_seconds = shutdown_grace_seconds
        self._spawn = spawn if spawn is not None else _default_spawn
        self._poll_interval = poll_interval_seconds
        self._children: list[_Child] = []
        self.results: dict[str, LaunchResult] = {}

    # ------------------------------------------------------------------ locks
    def _lock_for(self, runtime_id: str) -> ProcessLock:
        return supervisor_lock(
            runtime_id=runtime_id,
            lock_dir=self._paths.lock_root,
            pid_dir=self._paths.pid_root,
        )

    def is_running(self, runtime_id: str) -> bool:
        """Whether a *verified* live supervisor already owns this runtime.

        ``current_owner`` is the existing authority: it refuses a PID that is
        dead and a PID that has been reused by an unrelated process, which is
        precisely what makes it safe to treat as "already started" rather than
        spawning a duplicate.
        """
        return self._lock_for(runtime_id).current_owner() is not None

    # ----------------------------------------------------------------- launch
    def launch(self, runtime_ids: Sequence[str]) -> dict[str, LaunchResult]:
        """Start each runtime, independently, and verify each handshake.

        One runtime failing — to spawn, to hand shake, or by exiting early —
        never prevents the next from being attempted. That isolation is the
        whole point of doing this per runtime rather than as one batch.
        """
        for runtime_id in runtime_ids:
            try:
                result = self._launch_one(runtime_id)
            except Exception as exc:
                _log.exception("failed to launch runtime %s", runtime_id)
                result = LaunchResult(
                    runtime_id=runtime_id,
                    started=False,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            self.results[runtime_id] = result
        return dict(self.results)

    def _launch_one(self, runtime_id: str) -> LaunchResult:
        if self.is_running(runtime_id):
            owner = self._lock_for(runtime_id).current_owner()
            detail = f"already running as pid {owner.pid}" if owner else "already running"
            _log.info("runtime %s is %s; not starting a duplicate", runtime_id, detail)
            return LaunchResult(
                runtime_id=runtime_id,
                started=True,
                detail=detail,
                pid=owner.pid if owner else None,
                already_running=True,
            )

        command = [
            str(self._python_bin),
            "-m",
            "orchestration.process_control.supervised_launch",
            "--runtime-id",
            runtime_id,
            "--config-root",
            str(self._config_root),
        ]
        _log.info("starting runtime %s: %s", runtime_id, " ".join(command))
        handle = self._spawn(command)
        result = LaunchResult(
            runtime_id=runtime_id, started=False, detail="handshake pending", pid=handle.pid
        )
        child = _Child(runtime_id=runtime_id, handle=handle, result=result)
        self._children.append(child)

        self._handshake(child)
        return child.result

    def _handshake(self, child: _Child) -> None:
        """Wait for the runtime to take its own supervisor lock.

        Three outcomes, all of them reported truthfully:

        * the lock appears -> started;
        * the child exits first -> not started, with its exit code;
        * the window elapses -> not started, and the unverified child is
          stopped rather than left half-alive. A process that never took the
          lock is not a working runtime, and leaving it running would mean an
          ambiguous state nobody owns.
        """
        deadline = self._clock() + timedelta(seconds=self._handshake_seconds)
        while True:
            exit_code = child.handle.poll()
            if exit_code is not None:
                child.result.started = False
                child.result.exit_code = exit_code
                child.result.detail = f"exited with code {exit_code} before taking its lock"
                _log.error("runtime %s %s", child.runtime_id, child.result.detail)
                return

            if self.is_running(child.runtime_id):
                child.result.started = True
                child.result.detail = "supervisor lock acquired"
                _log.info("runtime %s started (pid %s)", child.runtime_id, child.result.pid)
                return

            if self._clock() >= deadline:
                child.result.started = False
                child.result.detail = (
                    f"did not take its supervisor lock within {self._handshake_seconds:.0f}s"
                )
                _log.error("runtime %s %s; stopping it", child.runtime_id, child.result.detail)
                self._stop_child(child)
                return

            if self._stop_event.wait(self._poll_interval):
                child.result.started = False
                child.result.detail = "shutdown requested during startup"
                return

    # ------------------------------------------------------------- supervision
    def supervise(self) -> dict[str, LaunchResult]:
        """Block until every owned child has exited, then report.

        Keeps this process — and therefore ``caffeinate`` and ``launchd``'s
        view of the job — alive for the whole trading session. On a shutdown
        signal the children are asked to stop and this returns once they have.
        """
        live = [child for child in self._children if child.handle.poll() is None]
        if not live:
            return dict(self.results)

        _log.info(
            "supervising %d runtime process(es); this process stays alive until they exit",
            len(live),
        )
        while True:
            if self._stop_event.is_set():
                _log.info("shutdown requested; stopping owned runtime processes")
                self.shutdown()
                break

            still_running = False
            for child in self._children:
                if child.result.exit_code is not None:
                    continue
                exit_code = child.handle.poll()
                if exit_code is None:
                    still_running = True
                    continue
                child.result.exit_code = exit_code
                # No restart, ever. A deliberate stop must stay stopped, and
                # bounded retry already happened one level down.
                _log.info(
                    "runtime %s exited with code %d; not restarting it",
                    child.runtime_id,
                    exit_code,
                )
            if not still_running:
                break
            self._stop_event.wait(self._poll_interval)

        return dict(self.results)

    def _stop_child(self, child: _Child) -> None:
        """Stop one child — the handshake-timeout path.

        A process that never took its supervisor lock is not a working runtime.
        Leaving it alive would mean a half-started state nobody owns, and one
        that a later trigger could not safely start over.
        """
        self._terminate([child])

    def shutdown(self) -> None:
        """SIGTERM every live child, wait boundedly, then SIGKILL what remains."""
        self._terminate([child for child in self._children if child.handle.poll() is None])

    def _terminate(self, children: list[_Child]) -> None:
        live = [child for child in children if child.handle.poll() is None]
        if not live:
            return
        for child in live:
            _log.info("sending SIGTERM to runtime %s (pid %s)", child.runtime_id, child.handle.pid)
            with contextlib.suppress(OSError, ProcessLookupError):  # already gone
                child.handle.send_signal(signal.SIGTERM)
            child.signalled_at = self._clock()

        deadline = self._clock() + timedelta(seconds=self._shutdown_grace_seconds)
        while self._clock() < deadline:
            if all(child.handle.poll() is not None for child in live):
                break
            # A plain sleep, not stop_event.wait: the stop event is already set
            # here, so waiting on it would return instantly and spin.
            _sleep(self._poll_interval)

        for child in live:
            exit_code = child.handle.poll()
            if exit_code is None:
                _log.error(
                    "runtime %s did not exit within %.0fs; sending SIGKILL",
                    child.runtime_id,
                    self._shutdown_grace_seconds,
                )
                with contextlib.suppress(OSError, ProcessLookupError):
                    child.handle.kill()
                exit_code = child.handle.poll()
            if exit_code is not None:
                child.result.exit_code = exit_code


def _default_spawn(command: Sequence[str]) -> ProcessHandle:
    """The real spawn. Never called in tests."""
    return subprocess.Popen(list(command))


def _sleep(seconds: float) -> None:
    """Indirected so tests can neutralise the one unavoidable real wait."""
    import time

    time.sleep(seconds)
