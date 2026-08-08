"""Bounded age-based deletion for the tables in :data:`policy.RETAINED_TABLES`.

Never ``orders``/``fills``/``positions``/``order_intents`` — trading history
is retained forever; deletion here is scoped to exactly the five
diagnostic/observability tables the policy module names.
"""

from __future__ import annotations

from datetime import UTC, datetime

from common.logging import get_logger
from common.persistence import Database

from .policy import RETAINED_TABLES, cutoff_iso

_log = get_logger(__name__)


def purge_old_rows(
    database: Database,
    *,
    max_age_days: int,
    batch_limit: int,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete rows older than ``max_age_days`` from every table in ``RETAINED_TABLES``.

    One transaction for the whole sweep — a crash mid-purge must not leave
    ``notifications`` trimmed and ``errors`` untouched, silently disagreeing
    about how far back their history goes.

    Bounded per table by ``batch_limit``, via a ``LIMIT``ed subquery rather
    than ``DELETE ... LIMIT`` directly: Python's stdlib ``sqlite3`` is not
    built with ``SQLITE_ENABLE_UPDATE_DELETE_LIMIT``, so the latter is not
    portable, while a ``LIMIT`` on the inner ``SELECT`` always works. A
    database that has accumulated months of backlog the first time this runs
    does not hold the write lock for one unbounded delete; each controlled
    startup chips away at the rest.

    Returns rows deleted per table; a table with nothing to delete is
    omitted from the result.
    """
    reference = now if now is not None else datetime.now(UTC)
    cutoff = cutoff_iso(now=reference, max_age_days=max_age_days)
    deleted: dict[str, int] = {}
    with database.transaction() as conn:
        for table, column in RETAINED_TABLES.items():
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE id IN "
                f"(SELECT id FROM {table} WHERE {column} < ? ORDER BY {column} LIMIT ?)",
                (cutoff, batch_limit),
            )
            if cursor.rowcount > 0:
                deleted[table] = cursor.rowcount
    if deleted:
        _log.info("retention: purged old rows %s", deleted)
    return deleted
