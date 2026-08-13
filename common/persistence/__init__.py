"""SQLite persistence: connections, transactions and forward-only migrations."""

from __future__ import annotations

from .account_shared import (
    ACCOUNT_VERSIONS_DIR,
    migrate_account_shared_database,
    open_account_shared_database,
)
from .database import Database, DatabaseError, connect_readonly
from .migrations import (
    Migration,
    MigrationError,
    MigrationRunner,
    discover_migrations,
    migrate,
)

__all__ = [
    "ACCOUNT_VERSIONS_DIR",
    "Database",
    "DatabaseError",
    "Migration",
    "MigrationError",
    "MigrationRunner",
    "connect_readonly",
    "discover_migrations",
    "migrate",
    "migrate_account_shared_database",
    "open_account_shared_database",
]
