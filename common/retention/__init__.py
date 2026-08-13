"""Retention and backups (Phase 7 Part 5, spec section 12).

One entry point, :func:`run_retention`, invoked once per controlled startup —
see ``runtimes/intraday_options/__main__.py:main``. Never a cron, never a
thread on the trading path: retention runs synchronously at startup, the same
way :func:`~common.persistence.migrations.migrate` already does, and for the
same reason (spec section 13: a corrupt database or a failed migration must
stop the runtime before order activity begins — unbounded storage growth is a
slower version of the same problem).

:func:`backup_database` is separate and is called *before* migration, from
the same startup call site; everything :func:`run_retention` does runs
*after*. See ``runner.py``'s docstring for why the two cannot be one call.
"""

from __future__ import annotations

from .backup import backup_database, verify_backup_restorable
from .database import purge_old_rows
from .logs import LogRetentionReport, rotate_launchd_logs, sweep_logs
from .policy import NEVER_PURGED_TABLES, RETAINED_TABLES
from .runner import RetentionReport, run_retention

__all__ = [
    "NEVER_PURGED_TABLES",
    "RETAINED_TABLES",
    "LogRetentionReport",
    "RetentionReport",
    "backup_database",
    "purge_old_rows",
    "rotate_launchd_logs",
    "run_retention",
    "sweep_logs",
    "verify_backup_restorable",
]
