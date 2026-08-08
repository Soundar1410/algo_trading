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
from .logs import LogRetentionReport, sweep_logs

_log = get_logger(__name__)


@dataclass(frozen=True)
class RetentionReport:
    """What one retention sweep did, for the caller's startup log line."""

    rows_deleted: dict[str, int] = field(default_factory=dict)
    logs: LogRetentionReport = field(default_factory=LogRetentionReport)
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
    pruned = ScripMasterCache(cache_dir).prune(keep=scrip_cache_retain_count)

    _log.info(
        "retention sweep complete: %d table(s) had rows purged, %d log(s) compressed, "
        "%d log(s) deleted, %d scrip master(s) pruned",
        len(rows_deleted),
        len(logs_report.compressed),
        len(logs_report.deleted),
        pruned,
    )
    return RetentionReport(rows_deleted=rows_deleted, logs=logs_report, scrip_masters_pruned=pruned)
