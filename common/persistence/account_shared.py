"""The account-scoped shared database (Phase 10).

One database, shared by every live worker across every runtime group
authenticated to the same Dhan account — order-rate windows, risk
reservations, realised-P&L events, open-position/MTM state, live
confirmations, preflight results and the account-state provenance marker.
Deliberately a *separate* database from each runtime group's own
``<runtime_id>.db`` (spec section 11 keeps one database per runtime group
for failure isolation): these tables must be readable and writable by every
live worker in every group, which a per-group file cannot do without a
fragile cross-file join across independently-WAL'd databases.

Migrated by the exact same :class:`~common.persistence.migrations.MigrationRunner`
machinery every runtime group's database already uses, pointed at this
module's own ``versions_dir`` and this database's own ``schema_migrations``
row-set — no new migration framework, no new locking primitive.
"""

from __future__ import annotations

from pathlib import Path

from .database import Database
from .migrations import Migration, MigrationRunner

ACCOUNT_VERSIONS_DIR = Path(__file__).resolve().parent / "migrations" / "account_versions"


def open_account_shared_database(path: Path | str) -> Database:
    """Open (creating the file/parent directory if needed) the shared
    account database at ``path`` — normally
    ``ProjectPaths.account_shared_database_path``."""
    return Database(path)


def migrate_account_shared_database(
    database: Database, *, versions_dir: Path | None = None
) -> list[Migration]:
    """Apply every pending account-shared migration. Purely additive so far
    (no reviewed-destructive exception exists in this versions_dir) — no
    ``require_fresh_backup_for_destructive`` argument is needed here."""
    runner = MigrationRunner(database, versions_dir=versions_dir or ACCOUNT_VERSIONS_DIR)
    return runner.run_pending()
