"""SQLite connection management.

One database per runtime group (spec section 11), so a corrupt or locked
``intraday_options.db`` cannot take down positional options.

Standard-library ``sqlite3`` only — no ORM. The schema is small, and raw SQL in a
repository is easier to audit than a mapping layer when the question is "did this
fill really get written before we told the strategy it was filled?".

Pragmas applied to every write connection:

* ``journal_mode=WAL`` — the read-only dashboard never blocks a trading write.
* ``foreign_keys=ON`` — off by default in SQLite, and orphaned fills pointing at
  a vanished order are exactly the corruption we cannot detect later.
* ``busy_timeout`` — wait for a competing writer rather than failing instantly.
* ``synchronous=FULL`` — durability over throughput. This platform writes a few
  rows per trade, not thousands per second; losing the last fill on a power cut
  costs far more than the fsync.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_BUSY_TIMEOUT_MS = 5_000


class DatabaseError(RuntimeError):
    """Raised when the database cannot be opened or fails an integrity check."""


def _apply_pragmas(conn: sqlite3.Connection, *, busy_timeout_ms: int) -> None:
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")


class Database:
    """Owns one SQLite connection for one runtime group's operational database."""

    def __init__(
        self,
        path: Path | str,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self._path = Path(path)
        self._busy_timeout_ms = busy_timeout_ms
        self._connection: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    def connect(self) -> sqlite3.Connection:
        """Open (once) and return the write connection."""
        if self._connection is not None:
            return self._connection

        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # isolation_level=None: transactions are opened explicitly by
            # ``transaction()`` rather than by sqlite3's implicit heuristics,
            # so a fill and its position update cannot end up in two
            # transactions by accident.
            conn = sqlite3.connect(
                str(self._path),
                isolation_level=None,
                timeout=self._busy_timeout_ms / 1000,
            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"Cannot open database {self._path}: {exc}") from exc

        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn, busy_timeout_ms=self._busy_timeout_ms)
        self._connection = conn
        return conn

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block in one transaction; roll back on any exception.

        Keep network calls out of these blocks — a broker submission inside an
        open transaction holds a write lock for the round trip.
        """
        conn = self.connect()
        conn.execute("BEGIN")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ------------------------------------------------------------- integrity
    def integrity_check(self) -> list[str]:
        """``PRAGMA integrity_check``. Empty list means healthy."""
        rows = self.connect().execute("PRAGMA integrity_check").fetchall()
        problems = [row[0] for row in rows if row[0] != "ok"]
        return problems

    def foreign_key_check(self) -> list[tuple[object, ...]]:
        """``PRAGMA foreign_key_check``. Empty list means no violations."""
        return [tuple(row) for row in self.connect().execute("PRAGMA foreign_key_check").fetchall()]

    def journal_mode(self) -> str:
        row = self.connect().execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower()


def connect_readonly(path: Path | str, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS):  # type: ignore[no-untyped-def]
    """Open a database read-only, for the dashboard.

    Uses SQLite's ``mode=ro`` URI so a write is refused by the driver itself.
    Enforcing this in the connection rather than by convention is what makes
    "the dashboard is read-only" a property of the system instead of a promise:
    the dashboard cannot run a migration or mutate operational state even if
    someone later imports the wrong helper into a Streamlit page.
    """
    db_path = Path(path)
    if not db_path.is_file():
        raise DatabaseError(f"Cannot open {db_path} read-only: file does not exist")
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=busy_timeout_ms / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    return conn
