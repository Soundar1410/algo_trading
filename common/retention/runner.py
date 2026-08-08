"""The retention entry point.

One function, called once per controlled startup — see
``runtimes/intraday_options/__main__.py:main``. Never a cron, never a thread
on the trading path: retention runs synchronously after migration and before
a runtime group starts trading, the same way migration itself runs before
any worker is spawned.

:func:`~common.retention.backup.backup_database` is deliberately not called
from here. It must run *before* migration, while everything in this module
runs *after* — the database sweep needs the migrated schema to exist, and
compressing/deleting logs or pruning the scrip cache have no ordering
dependency on migration at all, so there is nothing gained by splitting them
further. Both functions are invoked from the same caller, back to back — see
that call site for the actual ordering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from common.logging import get_logger
from common.market_data.scrip_master import ScripMasterCache
from common.persistence import Database

from .database import purge_old_rows
from .logs import LogRetentionReport, rotate_launchd_logs, sweep_logs

_log = get_logger(__name__)


@dataclass(frozen=True)
class RetentionReport:
    """What one retention sweep did, for the caller's startup log line."""

    rows_deleted: dict[str, int] = field(default_factory=dict)
    logs: LogRetentionReport = field(default_factory=LogRetentionReport)
    #: Empty (never touched) when the caller passes no ``launchd_log_dir`` —
    #: e.g. every existing test that predates Phase 8's launchd rotation.
    launchd_logs: LogRetentionReport = field(default_factory=LogRetentionReport)
    scrip_masters_pruned: int = 0


def run_retention(
    *,
    database: Database,
    log_dir: Path,
    cache_dir: Path,
    log_max_age_days: int,
    log_compress_after_days: int,
    db_row_max_age_days: int,
    db_delete_batch_limit: int,
    scrip_cache_retain_count: int,
    launchd_log_dir: Path | None = None,
    now: datetime | None = None,
) -> RetentionReport:
    """Run every sweep once: old rows, old logs, old scrip-master copies.

    Each sweep touches a disjoint resource (the database, the shared log
    directory, the scrip-master cache), so there is no need for one
    transaction spanning all three — only the database sweep needs a
    transaction at all, and :func:`~common.retention.database.purge_old_rows`
    already provides it.

    :meth:`~common.market_data.scrip_master.ScripMasterCache.prune` existed
    since Phase 4 with no caller anywhere outside its own tests; this is that
    caller.

    ``launchd_log_dir``, when given (Phase 8), is rotated with
    :func:`~common.retention.logs.rotate_launchd_logs` — a rename step
    ``sweep_logs`` cannot substitute for, since it deliberately never touches
    an unrotated, potentially-still-open file — and then swept with the same
    ``log_max_age_days``/``log_compress_after_days`` policy as ``log_dir``,
    reusing the one retention policy rather than inventing a second. Omitted
    (``None``) keeps this call a no-op, so every pre-Phase-8 caller is
    unaffected.
    """
    reference = now if now is not None else datetime.now(UTC)

    rows_deleted = purge_old_rows(
        database,
        max_age_days=db_row_max_age_days,
        batch_limit=db_delete_batch_limit,
        now=reference,
    )
    logs_report = sweep_logs(
        log_dir,
        max_age_days=log_max_age_days,
        compress_after_days=log_compress_after_days,
        now=reference,
    )

    launchd_logs_report = LogRetentionReport()
    if launchd_log_dir is not None:
        # Rotate first: sweep_logs only ever manages a *rotated* backup, and
        # launchd's own captured file is permanently unrotated until this
        # renames it. See rotate_launchd_logs's own docstring for why this
        # is safe to do unconditionally, every run.
        rotate_launchd_logs(launchd_log_dir, now=reference)
        launchd_logs_report = sweep_logs(
            launchd_log_dir,
            max_age_days=log_max_age_days,
            compress_after_days=log_compress_after_days,
            now=reference,
        )

    pruned = ScripMasterCache(cache_dir).prune(keep=scrip_cache_retain_count)

    _log.info(
        "retention sweep complete: %d table(s) had rows purged, %d log(s) compressed, "
        "%d log(s) deleted, %d launchd log(s) compressed, %d launchd log(s) deleted, "
        "%d scrip master(s) pruned",
        len(rows_deleted),
        len(logs_report.compressed),
        len(logs_report.deleted),
        len(launchd_logs_report.compressed),
        len(launchd_logs_report.deleted),
        pruned,
    )
    return RetentionReport(
        rows_deleted=rows_deleted,
        logs=logs_report,
        launchd_logs=launchd_logs_report,
        scrip_masters_pruned=pruned,
    )
