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


def rotate_launchd_logs(launchd_log_dir: Path, *, now: datetime | None = None) -> tuple[Path, ...]:
    """Rename ``launchd``'s own captured stdout/stderr files into a shape
    :func:`sweep_logs` already knows how to compress and delete.

    Phase 8 gave every LaunchAgent an independent ``StandardOutPath``/
    ``StandardErrorPath`` (``orchestration/launchd/generate_plists.py``), and
    every production ``setup_logging()`` call site leaves ``console=True`` (its
    own default), so those files do not carry a handful of incidental print
    lines — they are a full second copy of the entire application log stream,
    at whatever level is configured. ``launchd`` itself never rotates or
    truncates them: it opens each in append mode at every scheduled start and
    keeps writing to the same path forever. :func:`sweep_logs` cannot help on
    its own — by design (see its own docstring) it only ever touches a
    *rotated* backup (``name.log.<n>[.gz]``), never the active, no-suffix file,
    because compressing a file a live process still has open can corrupt it.
    ``launchd``'s captured file is permanently in that "active" shape, since
    nothing ever renames it. This function is the missing rename step, run
    once per controlled startup, immediately before :func:`sweep_logs` is
    pointed at the same directory (``common.retention.run_retention``).

    **Why renaming a file a process may still be writing to is safe.**
    Verified on this project's own target platform (macOS/Darwin), not
    assumed from POSIX documentation alone: renaming a path never retargets
    an already-open file descriptor — the process holding it (today's
    ``launchd``-spawned run, if this is its first startup call) keeps
    appending into the *renamed* inode, and ``launchd``'s *next* scheduled
    start opens a genuinely fresh, empty file at the canonical path, because
    nothing exists there anymore. So the rotated file this call produces is
    exactly "everything written up to this moment" — including, harmlessly,
    a possible sliver of *this* run's own startup lines that ``launchd``
    already wrote before Python began — and it keeps accumulating today's
    output for the rest of this run under its new, rotated name.

    **Why a same-run :func:`sweep_logs` call can never touch a file this
    function just renamed.** ``RetentionConfig.log_max_age_days`` and
    ``.log_compress_after_days`` are both declared ``gt=0`` — a minimum of
    one full day — so a file whose ``mtime`` is "now" can never satisfy
    either cutoff on the same run that created it, regardless of
    configuration. It only becomes eligible once a later day's rotation has
    made it genuinely inert (the process that was writing to it has long
    since exited). ``test_a_freshly_rotated_file_is_never_touched_by_the_same_
    runs_sweep`` (``tests/unit/test_retention.py``) pins this directly rather
    than leaving it as an inference from the two ``Field`` declarations.

    Only non-empty, canonical-named files (``*.log``, no numeric suffix
    already) are renamed — an idle day should not manufacture an empty
    backup on every single startup, and a file already shaped like a
    rotated backup was rotated by an earlier run and is `sweep_logs`'s to
    manage from here.
    """
    launchd_log_dir = Path(launchd_log_dir)
    if not launchd_log_dir.is_dir():
        return ()

    reference = now if now is not None else datetime.now(UTC)
    suffix = int(reference.timestamp())

    rotated: list[Path] = []
    for path in sorted(launchd_log_dir.iterdir()):
        if not path.is_file() or not path.name.endswith(".log"):
            continue
        if path.stat().st_size == 0:
            continue

        destination = path.with_name(f"{path.name}.{suffix}")
        # Collision guard: two controlled startups for the same runtime
        # inside the same second (manual testing, mainly — a real day's
        # startup is preceded by auth/backup/migration work that takes far
        # longer than that). Never overwrite an existing rotated backup.
        disambiguator = suffix
        while destination.exists():
            disambiguator += 1
            destination = path.with_name(f"{path.name}.{disambiguator}")

        path.rename(destination)
        rotated.append(destination)
        _log.info("rotated launchd log %s -> %s", path, destination)

    return tuple(rotated)


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
