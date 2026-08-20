"""Exactly one notification per trading date, across every process.

The obvious implementation — "if the marker file is absent, send, then write
it" — is a race with a window wide enough to lose in practice: ``RunAtLoad``
and the 09:00 calendar trigger can fire within milliseconds of each other, and
both would see no marker. The legacy system had exactly this shape (a plain
``write_text`` marker) and relied on there only ever being one trigger.

So the claim, the delivery and the record all happen **inside one file lock**:

    with lock:
        if already delivered for this date: return False
        delivered = deliver()          # bounded: N attempts, small fixed delay
        if delivered: record it
        return delivered

Holding the lock across delivery is affordable precisely because delivery is
bounded (three attempts, five seconds apart, five-second HTTP timeouts — call
it thirty seconds worst case) and the lock timeout is comfortably longer. It
buys the property that matters: no second process can observe "not yet
delivered" while the first is mid-send.

A failed delivery is deliberately **not** recorded. The day stays unclaimed so
a later trigger may legitimately try again — which is the difference between
"we told the operator" and "we tried once and lost the message".

``filelock`` and an atomic ``os.replace`` are the same primitives
:mod:`common.process.locks` and :mod:`common.authentication.token_cache`
already use; this adds no new concurrency mechanism to the project.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from filelock import FileLock, Timeout

from common.logging import get_logger

_log = get_logger(__name__)

#: Long enough to cover a bounded Telegram send held under the lock, short
#: enough that a crashed holder does not stall a runtime start for a whole day.
DEFAULT_LOCK_TIMEOUT_SECONDS = 120.0

#: Notification kinds this claim arbitrates. Both are once-per-trading-date.
KIND_AUTH_SUCCESS = "auth_success"
KIND_GIVE_UP = "give_up"


@dataclass(frozen=True)
class ClaimResult:
    """What happened when a send was attempted."""

    delivered: bool
    #: True when this process performed the delivery; False when another
    #: process (or an earlier trigger today) had already done it.
    sent_by_us: bool
    reason: str


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """Same durability discipline as ``common.authentication.token_cache``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same directory as the target: os.replace is only atomic within one
    # filesystem, which is what makes a reader never see a half-written record.
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class DailyNotificationClaim:
    """Arbitrates once-per-trading-date delivery of one notification kind."""

    def __init__(
        self,
        path: Path,
        *,
        lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._path = Path(path)
        self._lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        self._lock_timeout = lock_timeout_seconds

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> dict[str, str]:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # An unreadable record is treated as "nothing delivered". The cost
            # of being wrong is one duplicate message; the cost of the other
            # direction is silence on the day something went wrong.
            _log.warning("auto-start notification record at %s is unreadable", self._path)
            return {}
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def already_delivered(self, *, day: date, kind: str) -> bool:
        """Pure read — no lock, no write. For status commands and tests."""
        return self._read().get(kind) == day.isoformat()

    def send_once(
        self,
        *,
        day: date,
        kind: str,
        deliver: Callable[[], bool],
    ) -> ClaimResult:
        """Deliver ``kind`` for ``day`` at most once across every process.

        ``deliver`` must be bounded and must return whether delivery actually
        succeeded. Returning ``True`` optimistically — because a message was
        queued rather than accepted — would record a day as notified when
        nobody was notified, which is the one outcome this class exists to
        prevent.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self._lock_path))
        try:
            lock.acquire(timeout=self._lock_timeout)
        except Timeout:
            # Another process holds the claim and is mid-delivery. It will
            # either succeed (nothing for us to do) or fail (and leave the day
            # unclaimed for a later trigger). Either way, not ours to send.
            _log.warning(
                "another process holds the %s notification claim for %s; not sending",
                kind,
                day.isoformat(),
            )
            return ClaimResult(False, False, "claim held by another process")

        try:
            state = self._read()
            if state.get(kind) == day.isoformat():
                return ClaimResult(True, False, "already delivered today")

            delivered = bool(deliver())
            if not delivered:
                _log.error(
                    "%s notification for %s was not delivered; leaving the day unclaimed",
                    kind,
                    day.isoformat(),
                )
                return ClaimResult(False, True, "delivery failed; not recorded")

            state[kind] = day.isoformat()
            _atomic_write_json(self._path, dict(state))
            return ClaimResult(True, True, "delivered and recorded")
        finally:
            lock.release()
