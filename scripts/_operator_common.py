"""Shared plumbing for the operator control scripts.

Not a script itself — no ``main()``, no ``if __name__ == "__main__":``. It
exists so ``stop_runtime.py``, ``stop_strategy.py`` and ``square_off.py``
share exactly one implementation of "find the verified owner of a lock and
signal it" and "record an audit event" rather than three copies drifting
apart. Still swept by every structural check in
``tests/unit/test_scripts_are_read_only.py`` — no broker import, no order
endpoint, no mutating HTTP verb — the same as any file in this directory.

**The only write anything here performs is to the audit trail.** Signalling a
process is not a database write; recording that it happened is, and
``record_audit_event`` is the one write path every caller here uses.
"""

from __future__ import annotations

import getpass
import os
import signal
from pathlib import Path

from common.config import ProjectPaths, Settings, load_paths, load_settings
from common.execution import ExecutionRepository
from common.persistence import Database, MigrationRunner
from common.process.locks import ProcessLock


def load_project(explicit_root: str | None = None) -> tuple[Settings, ProjectPaths]:
    """The one place every operator script resolves settings and paths from."""
    settings = load_settings()
    paths = load_paths(explicit_root, settings=settings)
    return settings, paths


def open_audit_repository(database_path: Path) -> ExecutionRepository:
    """An :class:`ExecutionRepository` guaranteed to have ``audit_events``.

    A control script can legitimately run against a runtime that has never
    been started — ``stop_runtime`` on one nobody started yet, say — in which
    case no supervisor has ever applied a migration and the database may not
    even exist. Every control script's only write is an audit event, so this
    runs the (idempotent, additive-only) migrations before handing back a
    repository, the same way every worker already does at its own startup,
    rather than letting ``record_audit_event`` fail with "no such table" the
    one time it matters least to fail loudly.
    """
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    return ExecutionRepository(database)


def current_actor() -> str:
    """The OS username issuing the command. Never a secret (migration 0004)."""
    try:
        return getpass.getuser()
    except OSError:
        return "unknown"


def terminate_verified_owner(
    lock: ProcessLock,
    *,
    repository: ExecutionRepository,
    runtime_id: str,
    strategy_id: str | None,
    action: str,
) -> tuple[bool, str]:
    """Signal ``lock``'s verified owner with ``SIGTERM``, and audit either way.

    Returns ``(stopped, message)``. Refuses — never signals anything — when
    :meth:`~common.process.locks.ProcessLock.current_owner` finds no
    *verified* live owner: an unverified PID is precisely a PID-reuse hazard
    (see that module's own docstring), and refusing to guess is the
    fail-closed direction for the one property in this codebase where a
    wrong answer means signalling the wrong process. The supervisor's own
    ``shutdown_signals`` handler (``common.process.signals``) turns the
    ``SIGTERM`` into an orderly shutdown; this function does nothing beyond
    sending it and recording that it did.
    """
    owner = lock.current_owner()
    actor = current_actor()

    if owner is None:
        message = (
            f"No verified owner for {lock.identity!r} — nothing to stop. Either it "
            "is not running, or its PID file is stale/unverifiable (a reused PID; "
            "see common.process.locks)."
        )
        repository.record_audit_event(
            runtime_id=runtime_id,
            action=action,
            actor=actor,
            strategy_id=strategy_id,
            detail=message,
        )
        return False, message

    try:
        os.kill(owner.pid, signal.SIGTERM)
    except ProcessLookupError:
        message = (
            f"pid {owner.pid} ({lock.identity}) exited between being verified and "
            "being signalled; nothing left to stop."
        )
        repository.record_audit_event(
            runtime_id=runtime_id,
            action=action,
            actor=actor,
            strategy_id=strategy_id,
            detail=message,
        )
        return False, message
    except PermissionError as exc:
        message = f"verified owner pid {owner.pid} ({lock.identity}) could not be signalled: {exc}"
        repository.record_audit_event(
            runtime_id=runtime_id,
            action=action,
            actor=actor,
            strategy_id=strategy_id,
            detail=message,
        )
        return False, message

    message = f"sent SIGTERM to pid {owner.pid} ({lock.identity}), held since {owner.acquired_at}"
    repository.record_audit_event(
        runtime_id=runtime_id,
        action=action,
        actor=actor,
        strategy_id=strategy_id,
        detail=message,
    )
    return True, message
