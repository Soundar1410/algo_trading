"""Snapshots of ``positional_stocks.db`` before a decide run writes (D104).

The same method as the two existing runtimes' pre-migration backups
(:mod:`common.retention.backup`): SQLite's online backup API, then a proof
that the snapshot opens and restores. It is **re-implemented here, not
imported**: ``common.retention``'s package ``__init__`` imports its runner,
which imports the scrip master — a module the offline decide run must never
load (spec 10.3). ``common/retention`` itself is not modified.

Snapshots go to ``data/backups/positional_stocks_<UTC timestamp>.db``; the
newest :data:`~.run_config.BACKUPS_KEPT` are kept.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from common.logging import get_logger

_log = get_logger(__name__)

#: Zero-padded and big-endian, so file names sort chronologically, with
#: microseconds so two snapshots in one second never collide.
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"


class BackupError(RuntimeError):
    """A snapshot could not be taken or did not verify. The run must not write."""


def snapshot(
    db_path: Path, backup_dir: Path, *, keep: int, now: datetime | None = None
) -> Path | None:
    """Back up ``db_path``, verify it, prune old snapshots.

    Returns the snapshot, or ``None`` when the database does not exist yet
    (a first run has nothing to back up).

    Raises:
        BackupError: the snapshot failed its integrity or restore check.
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        return None
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now if now is not None else datetime.now(UTC)).strftime(_TIMESTAMP_FORMAT)
    dest = backup_dir / f"{db_path.stem}_{stamp}.db"

    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    try:
        _verify(dest)
    except sqlite3.Error as exc:
        raise BackupError(f"snapshot {dest} failed verification: {exc}") from exc
    _log.info("backed up %s to %s", db_path, dest)
    prune(db_path.stem, backup_dir, keep=keep)
    return dest


def _verify(path: Path) -> None:
    """The snapshot opens clean, and restores (backup API the other way) clean."""
    _check(path)
    with tempfile.TemporaryDirectory(prefix="positional_stocks_restore_") as directory:
        restored = Path(directory) / path.name
        source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(str(restored))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        _check(restored)


def _check(path: Path) -> None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise sqlite3.DatabaseError(f"integrity_check: {integrity}")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.DatabaseError("foreign_key_check reported violations")
    finally:
        conn.close()


def prune(stem: str, backup_dir: Path, *, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` snapshots of ``stem``."""
    existing = sorted(Path(backup_dir).glob(f"{stem}_*.db"))
    doomed = existing[:-keep] if keep > 0 else existing
    for path in doomed:
        path.unlink(missing_ok=True)
    return doomed
