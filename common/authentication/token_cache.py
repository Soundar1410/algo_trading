"""On-disk access-token cache, and the credential-rejection cooldown.

Two small persistent artefacts, both written the same crash-safe way.

**The token cache** lets a 24-hour token minted once before the market opens be
reused by every supervisor and worker, across restarts. Only the first process
each day performs a login; the rest read this file. That matters because Dhan
caps token generation at roughly one request every two minutes.

**The rejection cooldown** is the reason a wrong PIN costs exactly one login
attempt rather than one per process per restart. See :class:`RejectionCooldown`.

Both are written atomically: a temporary file in the *same directory*, flushed
and ``fsync``-ed, ``chmod 0600``, then ``os.replace``. ``os.replace`` is atomic
within a filesystem, so a crash at any point leaves either the complete old file
or the complete new one — never a half-written token. The same-directory
requirement is not incidental: across filesystems ``os.replace`` degrades to a
copy, which is exactly the non-atomic behaviour being avoided.

Ported from the reference repository's ``framework/auth/token_store.py``, with
three additions the spec requires (line 1395): client-identity validation,
expiry checking, and a refresh lock built on ``filelock`` (already a project
dependency, and what :mod:`common.process.locks` uses) rather than raw ``fcntl``.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from common.logging import get_logger

from .exceptions import TokenStoreError
from .jwt_claims import decode_token_exp

_log = get_logger(__name__)

#: Regenerate once a token is within this many seconds of expiry, so a long
#: session never runs off the end of its token mid-trade.
DEFAULT_EXPIRY_MARGIN_SECONDS = 300

#: How long a credential rejection suppresses further attempts. Comfortably
#: longer than Dhan's ~2-minute generation cap: the cooldown exists to stop
#: repeated *rejected* logins, which is a lockout risk, not merely a rate one.
DEFAULT_COOLDOWN_SECONDS = 600

#: Owner-only. The file holds a live bearer token.
_SECRET_FILE_MODE = 0o600


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as JSON to ``path``, atomically, owner-readable only."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                # Without fsync the rename can be durable while the contents are
                # not, which on a power loss yields a valid-looking empty file.
                os.fsync(handle.fileno())
            os.chmod(tmp, _SECRET_FILE_MODE)
            os.replace(tmp, path)
        finally:
            # Only reached if replace never happened; after a successful replace
            # the temp name no longer exists.
            tmp.unlink(missing_ok=True)
    except OSError as exc:
        raise TokenStoreError(f"Could not write {path}: {exc}") from exc


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON object, or None if absent/unreadable/not an object."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Corrupt is treated as absent, never raised: a bad file must not be able
        # to wedge startup when the fix is simply to mint a new token.
        _log.warning("ignoring unreadable file %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class StoredToken:
    """A cached token together with the identity it belongs to."""

    access_token: str
    client_id: str
    created_at: str
    expiry_time: str | None

    def seconds_until_expiry(self, *, now: float | None = None) -> float | None:
        """Seconds until the JWT ``exp`` claim, or None if undeterminable.

        None means "cannot tell", which callers treat as usable — we cannot prove
        it is expired, and a genuinely dead token is rejected by the first real
        API call, which is cheaper than a needless regeneration.
        """
        exp = decode_token_exp(self.access_token)
        if exp is None:
            return None
        moment = time.time() if now is None else now
        return exp - moment

    def is_usable(
        self,
        *,
        margin_seconds: int = DEFAULT_EXPIRY_MARGIN_SECONDS,
        now: float | None = None,
    ) -> bool:
        remaining = self.seconds_until_expiry(now=now)
        if remaining is None:
            return True
        return remaining > margin_seconds


class TokenCache:
    """Reads and writes the cached access token, corruption- and identity-safe."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock_path = self._path.with_suffix(self._path.suffix + ".lock")

    @property
    def path(self) -> Path:
        return self._path

    def load(self, *, expected_client_id: str | None = None) -> StoredToken | None:
        """Return the cached token, or None if it is unusable for any reason.

        Absent, corrupt, missing a token, or belonging to a *different* client id
        all return None. The identity check is the spec's requirement (line 1395)
        and matters concretely: a token cached under another account would
        otherwise be presented as this account's, and the resulting failures
        would look like anything but a mixed-up cache.
        """
        data = _read_json(self._path)
        if data is None:
            return None
        token = data.get("access_token")
        if not token:
            _log.warning("ignoring token cache %s: no access_token", self._path)
            return None

        cached_client = str(data.get("client_id") or "")
        if expected_client_id is not None and cached_client != expected_client_id:
            # Deliberately does not log either id — one is a secret, and the
            # other is enough to correlate with one.
            _log.warning(
                "ignoring token cache %s: it belongs to a different client identity",
                self._path,
            )
            return None

        return StoredToken(
            access_token=str(token),
            client_id=cached_client,
            created_at=str(data.get("created_at", "")),
            expiry_time=str(data["expiry_time"]) if data.get("expiry_time") else None,
        )

    def save(
        self,
        access_token: str,
        *,
        client_id: str,
        expiry_time: str | None = None,
    ) -> StoredToken:
        """Atomically persist a token with ``0600`` permissions."""
        stored = StoredToken(
            access_token=access_token,
            client_id=client_id,
            created_at=datetime.now(UTC).isoformat(),
            expiry_time=expiry_time,
        )
        _atomic_write_json(
            self._path,
            {
                "access_token": stored.access_token,
                "client_id": stored.client_id,
                "created_at": stored.created_at,
                "expiry_time": stored.expiry_time,
            },
        )
        # The path and expiry are safe to log; the token never is.
        _log.info("token cache written path=%s expiry=%s", self._path, expiry_time or "unknown")
        return stored

    def clear(self) -> None:
        """Delete the cache. Best-effort — failing to clear is not fatal."""
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            _log.warning("could not clear token cache %s: %s", self._path, exc)

    @contextmanager
    def refresh_lock(self, *, timeout: float = 60.0) -> Iterator[None]:
        """Serialise token *generation* across processes.

        N workers starting together must produce one login, not N. The winner
        generates and writes; the losers block here, then re-read the cache and
        find the token already present.

        A timeout is an error rather than a silent fall-through to generating
        anyway — falling through is precisely how one bad start becomes N login
        attempts.
        """
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self._lock_path), timeout=timeout)
        try:
            lock.acquire()
        except Timeout as exc:
            raise TokenStoreError(
                f"Timed out after {timeout}s waiting for the token refresh lock "
                f"({self._lock_path}). Refusing to generate a token concurrently."
            ) from exc
        try:
            yield
        finally:
            lock.release()


@dataclass(frozen=True)
class Rejection:
    """A recorded credential rejection. Carries no secret — a time and a reason."""

    rejected_at: float
    reason: str

    def remaining(self, *, cooldown_seconds: int, now: float | None = None) -> float:
        moment = time.time() if now is None else now
        return max(0.0, (self.rejected_at + cooldown_seconds) - moment)


class RejectionCooldown:
    """Suppresses login attempts after Dhan has rejected the credentials.

    This closes the gap that makes "we never retry" insufficient on its own. The
    refresh lock serialises generation and re-checks the cache — but only for
    *success*. On a rejection nothing is written, so every concurrent starter
    finds an empty cache and makes its own doomed request (N workers ⇒ N rejected
    logins), and every re-run of the bootstrap adds one more.

    Recording the rejection under the same lock means later attempts see the
    verdict and refuse **without contacting Dhan at all**.

    Clearing is deliberate — delete the file or pass ``--force`` — so fixing the
    PIN in ``.env`` never means waiting out a timer.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self._path = Path(path)
        self._cooldown_seconds = cooldown_seconds

    @property
    def path(self) -> Path:
        return self._path

    @property
    def cooldown_seconds(self) -> int:
        return self._cooldown_seconds

    def record(self, reason: str) -> None:
        """Persist a rejection. The reason is a message, never a credential."""
        _atomic_write_json(
            self._path,
            {
                "rejected_at": time.time(),
                "rejected_at_iso": datetime.now(UTC).isoformat(),
                "reason": reason,
            },
        )
        _log.error(
            "credential rejection recorded; suppressing login attempts for %ss",
            self._cooldown_seconds,
        )

    def active(self, *, now: float | None = None) -> Rejection | None:
        """The rejection still in force, or None."""
        data = _read_json(self._path)
        if data is None:
            return None
        raw_at = data.get("rejected_at")
        if not isinstance(raw_at, int | float) or isinstance(raw_at, bool):
            _log.warning("ignoring malformed cooldown file %s", self._path)
            return None
        rejection = Rejection(rejected_at=float(raw_at), reason=str(data.get("reason", "")))
        if rejection.remaining(cooldown_seconds=self._cooldown_seconds, now=now) <= 0:
            return None
        return rejection

    def clear(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            _log.warning("could not clear cooldown file %s: %s", self._path, exc)
