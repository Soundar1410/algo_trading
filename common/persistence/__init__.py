"""SQLite persistence: connections, transactions and forward-only migrations."""

from __future__ import annotations

from .database import Database, DatabaseError, connect_readonly
from .migrations import (
    Migration,
    MigrationError,
    MigrationRunner,
    discover_migrations,
    migrate,
)

__all__ = [
    "Database",
    "DatabaseError",
    "Migration",
    "MigrationError",
    "MigrationRunner",
    "connect_readonly",
    "discover_migrations",
    "migrate",
]
