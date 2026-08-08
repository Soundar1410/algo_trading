"""Forward-only sequential SQL migrations.

Each ``.sql`` file under ``migrations/versions/`` is applied once, in filename
order, and recorded in ``schema_migrations``. Filenames carry both parts of the
identity::

    0001_walking_skeleton.sql   ->  version "0001", name "walking_skeleton"

Deliberately *not* implemented here (spec section 4): migration checksums and
destructive-migration automation. Those belong to the controlled-live phase or
to the first genuinely destructive migration. Adding them now would be
unverifiable ceremony.

A ``filelock`` guard means two supervisors starting at once cannot both apply
version 0001. Integrity and foreign-key checks run after the batch: a migration
that leaves dangling references must stop the runtime *before* any order
activity, not surface days later as an unexplained position.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock, Timeout

from .database import Database, DatabaseError

VERSIONS_DIR = Path(__file__).resolve().parent / "migrations" / "versions"

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""

_FILENAME_RE = re.compile(r"^(?P<version>\d+)_(?P<name>.+)$")

DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0


class MigrationError(RuntimeError):
    """Raised when a migration is malformed, or the batch fails to apply."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path

    @classmethod
    def from_path(cls, path: Path) -> Migration:
        match = _FILENAME_RE.match(path.stem)
        if match is None:
            raise MigrationError(
                f"Migration filename {path.name!r} must look like '0001_description.sql'"
            )
        return cls(version=match.group("version"), name=match.group("name"), path=path)


#: Statements that make a migration non-replayable. The runner's crash-safety
#: rests on every script being a no-op on re-run, so these are rejected rather
#: than trusted to be used carefully. This is a *guard*, not the destructive
#: migration tooling the spec defers to the controlled-live phase.
#:
#: Phase 7 Part 5 added :func:`common.retention.backup_database`, called
#: before this runner on every controlled startup — so a pre-migration
#: snapshot now exists by the time a migration runs. That is *backup only*.
#: There is still no rollback machinery: nothing here restores from that
#: snapshot, verifies it against a running schema, or replays writes made
#: since it was taken. A genuinely destructive migration still needs that
#: built and tested before this list is revisited.
_DESTRUCTIVE_RE = re.compile(
    r"(?im)^\s*(DROP\s+(TABLE|INDEX|VIEW|TRIGGER)|DELETE\s+FROM|TRUNCATE|"
    r"ALTER\s+TABLE\s+\S+\s+DROP)\b"
)

#: A bare CREATE without IF NOT EXISTS fails on replay, so it is rejected too.
_NON_IDEMPOTENT_CREATE_RE = re.compile(
    r"(?im)^\s*CREATE\s+(?!.*\bIF\s+NOT\s+EXISTS\b)"
    r"(UNIQUE\s+)?(TABLE|INDEX|VIEW|TRIGGER)\b"
)


def _reject_destructive(migration: Migration, script: str) -> None:
    """Fail a migration that could not safely be re-run after a crash."""
    if _DESTRUCTIVE_RE.search(script):
        raise MigrationError(
            f"Migration {migration.path.name} contains a destructive statement. "
            "Migrations must be additive and re-runnable; a destructive change is "
            "deferred to the controlled-live phase, which must bring rollback "
            "machinery with it — a pre-migration backup alone (Phase 7 Part 5) is "
            "not enough to safely run one."
        )
    if _NON_IDEMPOTENT_CREATE_RE.search(script):
        raise MigrationError(
            f"Migration {migration.path.name} has a CREATE without IF NOT EXISTS. "
            "Every statement must be a no-op on re-run — the runner relies on "
            "replay rather than transactional atomicity (see MigrationRunner._apply_one)."
        )


def discover_migrations(versions_dir: Path | None = None) -> list[Migration]:
    """Return migrations in filename order, rejecting duplicate versions."""
    directory = versions_dir if versions_dir is not None else VERSIONS_DIR
    if not directory.is_dir():
        return []

    migrations = [Migration.from_path(p) for p in sorted(directory.glob("*.sql"))]

    seen: dict[str, Migration] = {}
    for migration in migrations:
        if migration.version in seen:
            raise MigrationError(
                f"Duplicate migration version {migration.version!r}: "
                f"{seen[migration.version].path.name} and {migration.path.name}"
            )
        seen[migration.version] = migration
    return migrations


class MigrationRunner:
    """Applies pending migrations to one database, under a cross-process lock."""

    def __init__(
        self,
        database: Database,
        *,
        versions_dir: Path | None = None,
        lock_path: Path | None = None,
        lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._db = database
        self._versions_dir = versions_dir if versions_dir is not None else VERSIONS_DIR
        self._lock_path = lock_path or database.path.with_suffix(
            database.path.suffix + ".migrate.lock"
        )
        self._lock_timeout = lock_timeout_seconds

    # ------------------------------------------------------------- querying
    def applied_versions(self) -> set[str]:
        conn = self._db.connect()
        conn.executescript(_BOOTSTRAP_SQL)
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        return {str(row["version"]) for row in rows}

    def current_version(self) -> str | None:
        """Highest applied version, or None on a fresh database."""
        applied = self.applied_versions()
        return max(applied) if applied else None

    def pending(self) -> list[Migration]:
        applied = self.applied_versions()
        return [m for m in discover_migrations(self._versions_dir) if m.version not in applied]

    # -------------------------------------------------------------- applying
    def run_pending(self) -> list[Migration]:
        """Apply every pending migration once. Returns those newly applied."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self._lock_path), timeout=self._lock_timeout)
        try:
            with lock:
                return self._run_pending_locked()
        except Timeout as exc:
            raise MigrationError(
                f"Timed out after {self._lock_timeout}s waiting for the migration lock at "
                f"{self._lock_path}. Another supervisor is probably migrating this database."
            ) from exc

    def _run_pending_locked(self) -> list[Migration]:
        conn = self._db.connect()
        conn.executescript(_BOOTSTRAP_SQL)

        applied = self.applied_versions()
        newly_applied: list[Migration] = []

        for migration in discover_migrations(self._versions_dir):
            if migration.version in applied:
                continue
            self._apply_one(conn, migration)
            newly_applied.append(migration)

        if newly_applied:
            self._verify_integrity()
        return newly_applied

    def _apply_one(self, conn: sqlite3.Connection, migration: Migration) -> None:
        """Apply one migration, then record it.

        These two steps are **not** one transaction, and cannot be: sqlite3's
        ``executescript()`` issues an implicit COMMIT before running, so
        wrapping it in ``BEGIN``/``COMMIT`` would silently buy nothing. Rather
        than pretend otherwise, safety comes from replay instead of atomicity:

        * :func:`_reject_destructive` enforces that a migration only contains
          re-runnable statements (``CREATE ... IF NOT EXISTS``), and
        * the recording INSERT happens last.

        So a crash between the two leaves the schema change applied but
        unrecorded, and the next startup simply re-runs a script that is a no-op
        the second time and then records it. The failure mode is a repeated
        no-op, never a half-applied schema.
        """
        script = migration.path.read_text(encoding="utf-8")
        _reject_destructive(migration, script)

        try:
            conn.executescript(script)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, datetime.now(UTC).isoformat()),
            )
        except sqlite3.Error as exc:
            raise MigrationError(f"Migration {migration.path.name} failed: {exc}") from exc

    def _verify_integrity(self) -> None:
        violations = self._db.foreign_key_check()
        if violations:
            raise DatabaseError(
                f"Foreign-key violations in {self._db.path} after migration: {violations}"
            )
        problems = self._db.integrity_check()
        if problems:
            raise DatabaseError(f"Integrity check failed for {self._db.path}: {problems}")


def migrate(
    database: Database,
    *,
    versions_dir: Path | None = None,
) -> list[Migration]:
    """Convenience wrapper: apply all pending migrations to ``database``."""
    return MigrationRunner(database, versions_dir=versions_dir).run_pending()
