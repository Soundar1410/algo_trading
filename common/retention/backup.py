"""Pre-migration database backups (spec section 12: "Backup operational DB
before schema migration" / "Retain a configurable number of daily backups").

**This is backup only.** It is deliberately *not* the destructive-migration
rollback machinery ``common.persistence.migrations`` defers to the
controlled-live phase (see that module's own docstring and
``_reject_destructive``'s error message). Restoring one of these files back
into a running system — checking schema compatibility, deciding what
"rollback" means for a database a worker may already be writing to, replaying
anything written since the snapshot — is a separate, harder problem this
module does not attempt. All it does is produce a consistent copy before
migration runs, and prune old copies.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from common.logging import get_logger

_log = get_logger(__name__)

#: Microsecond precision so two backups of the same database taken within one
#: second (an immediate restart, say) never collide on filename.
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"


def backup_database(
    db_path: Path,
    backup_dir: Path,
    *,
    retain_count: int,
    now: datetime | None = None,
) -> Path | None:
    """Snapshot ``db_path`` into ``backup_dir`` and prune old snapshots.

    Uses SQLite's own online backup API (``sqlite3.Connection.backup``)
    rather than a raw file copy, so a backup taken while the database is in
    WAL mode still produces a consistent single-file snapshot instead of
    copying a data file whose latest writes live in a separate ``-wal`` file.

    Returns the new backup's path, or ``None`` when there is nothing to back
    up yet — a fresh install, before the operational database has ever been
    created, must not fail startup over a database that does not exist.
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        _log.debug("no database at %s yet; nothing to back up", db_path)
        return None

    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now if now is not None else datetime.now(UTC)).strftime(_TIMESTAMP_FORMAT)
    dest = backup_dir / f"{db_path.stem}_{stamp}.db"

    source_conn = sqlite3.connect(str(db_path))
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()

    _log.info("backed up %s to %s", db_path, dest)
    _prune_backups(db_path, backup_dir, retain_count=retain_count)
    return dest


def _prune_backups(db_path: Path, backup_dir: Path, *, retain_count: int) -> int:
    """Delete all but the newest ``retain_count`` backups of this database.

    Filenames sort chronologically (the timestamp format is zero-padded and
    big-endian), so ``sorted()`` is enough — the same trick
    :meth:`~common.market_data.scrip_master.ScripMasterCache.prune` already
    relies on for its own dated filenames.
    """
    existing = sorted(backup_dir.glob(f"{db_path.stem}_*.db"))
    doomed = existing[:-retain_count] if retain_count > 0 else existing
    for path in doomed:
        path.unlink(missing_ok=True)
    if doomed:
        _log.info("pruned %d old backup(s) of %s", len(doomed), db_path.stem)
    return len(doomed)
