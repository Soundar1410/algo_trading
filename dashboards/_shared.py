"""Read-only database access shared by every dashboard page.

Centralises exactly one thing: what a page shows when its database is
missing, locked, or has pending migrations — a message, never a traceback.
Every page module in this package imports only this module, :mod:`common.
health.snapshot` and :func:`common.persistence.connect_readonly`. No page
imports a broker, a feed, or a write connection — enforced by
``tests/unit/test_dashboard.py``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from common.health import HealthSnapshot, read_snapshot
from common.persistence import DatabaseError, connect_readonly


@dataclass(frozen=True)
class SnapshotUnavailable:
    """Why a page has nothing to show. Rendered as a message, not raised."""

    reason: str


def load_snapshot(
    database_path: Path | str, runtime_id: str, trading_date: str
) -> HealthSnapshot | SnapshotUnavailable:
    """Read one runtime's health snapshot, or explain why it could not be read.

    Three failure modes an operator will actually hit, all handled here so no
    page has to:

    * **No database yet** — the runtime has never started. Checked before
      ``connect_readonly`` is even called, since that function raises for
      exactly this case and a pre-check gives a clearer message than
      catching its exception would.
    * **Locked** — a writer is mid-transaction past the read connection's
      busy timeout. Surfaces as ``sqlite3.OperationalError``.
    * **Pre-migration or corrupt** — the file exists but a table
      :func:`~common.health.snapshot.read_snapshot` queries does not (an old
      schema, or a database this project never wrote), or the file is not a
      valid SQLite database at all. Both surface as a ``sqlite3.Error``
      subclass the moment a query runs, not at connection time — SQLite
      opens lazily.
    """
    db_path = Path(database_path)
    if not db_path.is_file():
        return SnapshotUnavailable(f"No database yet at {db_path}. Start the supervisor first.")

    try:
        conn = connect_readonly(db_path)
    except DatabaseError as exc:
        return SnapshotUnavailable(str(exc))

    try:
        return read_snapshot(conn, runtime_id=runtime_id, trading_date=trading_date)
    except sqlite3.Error as exc:
        return SnapshotUnavailable(f"Database not ready ({type(exc).__name__}): {exc}")
    finally:
        conn.close()
