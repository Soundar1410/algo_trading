"""Retention policy — what is eligible for age-based deletion, and why.

Pure decisions only: no filesystem access, no database connection. Kept
separate from :mod:`common.retention.database` and :mod:`common.retention.logs`
so the *rule* ("these five tables, this column, never those four") is
reviewable in one place, independent of how it is enforced.

The defaults below are this module's own — ``common.config.models.
RetentionConfig`` duplicates each literal (the same discipline
``HealthConfig`` already follows for ``common.health.heartbeat.
DEFAULT_INTERVAL_SECONDS``; see ``tests/unit/test_config_loader.py``) rather
than importing them, so a config-layer change to a default is a deliberate
edit in both places, not a silent drift.
"""

from __future__ import annotations

from datetime import datetime, timedelta

#: table -> the column its age is measured on. A fixed, explicit allowlist —
#: not "every table matching a naming convention" — so a table added here is
#: a reviewed decision, never an accident of prefix-matching.
#:
#: All five are diagnostic/observability tables, exactly the kind spec
#: section 12 names ("Heartbeats. ... Notifications.") plus this project's
#: own two event logs (``feed_events``, ``auth_events``) and ``errors`` —
#: none of them trading records.
RETAINED_TABLES: dict[str, str] = {
    "runtime_heartbeats": "beat_at",
    "notifications": "created_at",
    "errors": "occurred_at",
    "feed_events": "occurred_at",
    "auth_events": "occurred_at",
}

#: The trading tables retention must never touch. Not consulted by the
#: deletion logic in :mod:`common.retention.database` — ``RETAINED_TABLES``
#: above is an allowlist, so nothing outside it is ever reachable — but
#: stated here explicitly so a reviewer, and
#: ``tests/unit/test_retention.py``, can check the two sets stay disjoint
#: without reading the SQL. ``trade_ledger`` (migration 0008) joined this
#: set the same day it was created: it is a durable trading record, not an
#: operational/diagnostic log, and the whole point of writing it was to
#: survive longer than a re-derivable read-model query — deleting it by
#: age would defeat that.
NEVER_PURGED_TABLES = frozenset({"orders", "fills", "positions", "order_intents", "trade_ledger"})

DEFAULT_LOG_MAX_AGE_DAYS = 30
DEFAULT_LOG_COMPRESS_AFTER_DAYS = 1
DEFAULT_DB_ROW_MAX_AGE_DAYS = 90
DEFAULT_DB_DELETE_BATCH_LIMIT = 5000
DEFAULT_BACKUP_RETAIN_COUNT = 7
DEFAULT_SCRIP_CACHE_RETAIN_COUNT = 3


def cutoff(*, now: datetime, max_age_days: int) -> datetime:
    """The instant below which something counts as expired."""
    return now - timedelta(days=max_age_days)


def cutoff_iso(*, now: datetime, max_age_days: int) -> str:
    """As :func:`cutoff`, formatted for comparison against a TEXT timestamp column."""
    return cutoff(now=now, max_age_days=max_age_days).isoformat()
