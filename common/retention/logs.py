"""Log compression and age-based deletion, layered on top of the size cap.

:mod:`common.logging.setup` already caps each log file's on-disk footprint
via ``RotatingFileHandler`` (10MB x 10 backups, per name). That cap bounds
size, not age or plain-text footprint: an idle log rotates slowly, so
``algo_trading.log.3`` can sit around, uncompressed, for months. This module
adds the other two axes spec section 12 asks for ("Compress old logs" /
"Implement retention for: Logs."): gzip a rotated backup once it stops being
"the log from a few hours ago", and delete anything — compressed or not —
past a configured age.

Only rotated backups are touched. The active file (``algo_trading.log``,
``io_alpha.log`` — no numeric suffix) is never compressed or deleted: it is
open for writing by a live process, and gzip-in-place against an open file
handle is exactly the kind of "clever" idea that corrupts a log during the
one week nobody is watching.
"""

from __future__ import annotations

import gzip
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from common.logging import get_logger

_log = get_logger(__name__)

#: Matches a ``RotatingFileHandler`` backup — "algo_trading.log.3" or an
#: already-compressed "algo_trading.log.3.gz" — never the active
#: "algo_trading.log" itself, which carries no numeric suffix.
_ROTATED_BACKUP_RE = re.compile(r"\.log\.\d+(\.gz)?$")


@dataclass(frozen=True)
class LogRetentionReport:
    """What one log sweep did, for the caller's startup log line."""

    compressed: tuple[Path, ...] = ()
    deleted: tuple[Path, ...] = ()


def sweep_logs(
    log_dir: Path,
    *,
    max_age_days: int,
    compress_after_days: int,
    now: datetime | None = None,
) -> LogRetentionReport:
    """Compress rotated backups past ``compress_after_days``, delete past ``max_age_days``.

    A file's age is its filesystem mtime, which is what
    ``RotatingFileHandler`` sets at rotation time — the moment it stopped
    being written to, which is exactly what "how old is this backup" means
    here.

    ``log_dir`` not existing (nothing has logged yet) is not an error — an
    empty report comes back instead.
    """
    log_dir = Path(log_dir)
    if not log_dir.is_dir():
        return LogRetentionReport()

    reference = now if now is not None else datetime.now(UTC)
    delete_cutoff = reference - timedelta(days=max_age_days)
    compress_cutoff = reference - timedelta(days=compress_after_days)

    compressed: list[Path] = []
    deleted: list[Path] = []

    for path in sorted(log_dir.iterdir()):
        if not path.is_file() or not _ROTATED_BACKUP_RE.search(path.name):
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

        if mtime < delete_cutoff:
            path.unlink(missing_ok=True)
            deleted.append(path)
            continue

        if path.suffix != ".gz" and mtime < compress_cutoff:
            compressed.append(_compress(path))

    if compressed or deleted:
        _log.info(
            "log retention in %s: compressed %d, deleted %d",
            log_dir,
            len(compressed),
            len(deleted),
        )
    return LogRetentionReport(compressed=tuple(compressed), deleted=tuple(deleted))


def _compress(path: Path) -> Path:
    """Gzip ``path`` and remove the plaintext original.

    The plaintext is only unlinked after the ``.gz`` write completes, so a
    crash mid-compress leaves the original behind to retry next startup
    rather than losing the backup.
    """
    dest = path.with_name(path.name + ".gz")
    with path.open("rb") as src, gzip.open(dest, "wb") as out:
        shutil.copyfileobj(src, out)
    path.unlink()
    return dest
