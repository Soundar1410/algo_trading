"""The one channel ``scripts/square_off.py`` uses to ask a running worker to
close out — a file, never a write to ``positions``.

The plan's own constraint (Phase 7 Part 4) is explicit: the operator command
"writes a request file the running worker polls and executes through its own
square-off path" and "never touches ``positions`` directly". A second writer
opening the strategy's live database while a worker holds it would be exactly
the "opens a second writer" hazard the whole operator-command design (plan
decisions, top of file) exists to avoid — so the request is a file the worker
already owns the polling loop for, not a database row from outside.

One file per ``(runtime_id, strategy_id)``, written atomically (temp file +
``os.replace`` via :meth:`Path.replace`, the same pattern
:mod:`common.process.locks` uses for the PID file) so a worker's poll can
never observe a half-written request. The worker does not clear the file the
moment it *sees* the request — only once the square-off has actually
completed — so a crash between "noticed" and "acted" does not silently lose
the request; the next poll (or the next restart, since a request outlives a
process the same way a PID file's stale state does) sees it again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class SquareOffRequest:
    """What an operator asked for. ``requested_by`` is an OS username, never a secret."""

    requested_at: str
    requested_by: str
    reason: str


def square_off_request_path(runtime_root: Path, runtime_id: str, strategy_id: str) -> Path:
    """Where this strategy's request file lives, if one has been written.

    ``runtime_root`` is :attr:`common.config.paths.ProjectPaths.runtime_root` —
    passed in rather than imported, so this module (like
    :mod:`common.process.locks`) keeps no dependency on ``common.config``; a
    spawned worker already has the path as a plain field on ``WorkerConfig``.
    """
    return runtime_root / "square_off_requests" / f"{runtime_id}.{strategy_id}.json"


def write_square_off_request(path: Path, *, requested_by: str, reason: str) -> SquareOffRequest:
    """Write the request atomically. Creates the parent directory if needed."""
    request = SquareOffRequest(
        requested_at=datetime.now(UTC).isoformat(),
        requested_by=requested_by,
        reason=reason,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {
                "requested_at": request.requested_at,
                "requested_by": request.requested_by,
                "reason": request.reason,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    return request


def read_square_off_request(path: Path) -> SquareOffRequest | None:
    """None when no request is pending, or the file is unparseable (treated
    the same as absent — a half-written file cannot happen, see the module
    docstring, so an unparseable one means something outside this system's
    control touched it, and refusing to act on it is the fail-closed
    direction, matching :func:`common.process.locks.LockOwner.from_json`."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SquareOffRequest(
            requested_at=str(data["requested_at"]),
            requested_by=str(data["requested_by"]),
            reason=str(data["reason"]),
        )
    except (ValueError, KeyError, TypeError, OSError):
        return None


def clear_square_off_request(path: Path) -> None:
    """Remove the request file. Safe to call when none exists."""
    path.unlink(missing_ok=True)
