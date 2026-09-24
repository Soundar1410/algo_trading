"""Open and migrate ``positional_stocks.db`` from its own migration set.

Spec 9 (v1.2i): the database is migrated by the shared
:class:`~common.persistence.migrations.MigrationRunner`, given
:data:`STOCK_VERSIONS_DIR` through ``versions_dir`` — never from the shared
``common/persistence/migrations/versions/``, which the two paper runtimes
apply and checksum at every start.
"""

from __future__ import annotations

from pathlib import Path

from common.persistence import Database, DatabaseError, MigrationRunner

#: This runtime's own migrations. Nothing here is ever copied to the shared
#: directory (see this folder's README).
STOCK_VERSIONS_DIR = Path(__file__).resolve().parent / "migrations"

#: The runtime id whose database this is: ``data/operational/positional_stocks.db``
#: through :meth:`~common.config.paths.ProjectPaths.database_path`.
RUNTIME_ID = "positional_stocks"


def open_stock_database(path: Path | str) -> Database:
    """Open ``path``, apply the stock migrations, and check integrity.

    Raises:
        DatabaseError: the database fails its integrity or foreign-key check.
        MigrationError: a migration is malformed, or an applied one was edited.
    """
    database = Database(path)
    MigrationRunner(database, versions_dir=STOCK_VERSIONS_DIR).run_pending()
    problems = database.integrity_check()
    if problems:
        raise DatabaseError(f"{path}: integrity check failed: {problems}")
    return database
