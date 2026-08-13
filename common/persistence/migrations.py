"""Forward-only sequential SQL migrations.

Each ``.sql`` file under ``migrations/versions/`` is applied once, in filename
order, and recorded in ``schema_migrations``. Filenames carry both parts of the
identity::

    0001_walking_skeleton.sql   ->  version "0001", name "walking_skeleton"

Migration checksums and destructive-migration automation were deliberately
*not* implemented in the walking skeleton (spec section 4) — they belong to
the controlled-live phase or to the first genuinely destructive migration.
Phase 10 is that phase: :func:`MigrationRunner.verify_checksums` and
:data:`_MANUALLY_REVIEWED_DESTRUCTIVE` below are that first genuinely
destructive migration's narrow, reviewed exception.

A ``filelock`` guard means two supervisors starting at once cannot both apply
version 0001. Integrity and foreign-key checks run after the batch: a migration
that leaves dangling references must stop the runtime *before* any order
activity, not surface days later as an unexplained position.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock, Timeout

from common.logging import get_logger

from .database import Database, DatabaseError

log = get_logger(__name__)

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

#: Migrations that are exceptions to "no destructive statements" — reviewed
#: individually, hard-coded (no comment-based opt-out: a future exception
#: needs its own code change and review, not a pragma in a .sql file), each
#: with its own additional safety precondition in ``_apply_one``. Phase 10's
#: ``0006`` widens ``orders.status``'s CHECK constraint to add ``EXPIRED``
#: (verified real and documented — see ``common.models.trading.OrderStatus``)
#: via SQLite's standard "create new table, copy, drop old, rename" pattern,
#: which SQLite requires because ``ALTER TABLE`` cannot widen a CHECK.
_MANUALLY_REVIEWED_DESTRUCTIVE = frozenset({"0006"})


class MigrationError(RuntimeError):
    """Raised when a migration is malformed, or the batch fails to apply."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    #: SHA-256 of the file's exact bytes at discovery time. Compared against
    #: what was recorded when the migration was applied
    #: (:meth:`MigrationRunner.verify_checksums`) to detect a migration file
    #: edited after it was already applied to a real database.
    checksum: str = field(default="", compare=False)

    @classmethod
    def from_path(cls, path: Path) -> Migration:
        match = _FILENAME_RE.match(path.stem)
        if match is None:
            raise MigrationError(
                f"Migration filename {path.name!r} must look like '0001_description.sql'"
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return cls(
            version=match.group("version"), name=match.group("name"), path=path, checksum=digest
        )


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
    """Fail a migration that could not safely be re-run after a crash.

    ``_MANUALLY_REVIEWED_DESTRUCTIVE`` is the one, narrow, hard-coded
    exception to the DROP/ALTER-DROP guard below — never a comment inside
    the ``.sql`` file itself, so no future migration can silently opt out.
    The non-idempotent-CREATE guard still applies to every migration,
    reviewed or not: even a rebuild migration's ``CREATE TABLE`` statements
    must be ``IF NOT EXISTS`` so a crash-and-retry doesn't fail differently
    from every other migration in this runner.
    """
    if migration.version not in _MANUALLY_REVIEWED_DESTRUCTIVE and _DESTRUCTIVE_RE.search(script):
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
        self._ensure_checksum_column(conn)
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
    def run_pending(self, *, require_fresh_backup_for_destructive: bool = False) -> list[Migration]:
        """Apply every pending migration once. Returns those newly applied.

        Args:
            require_fresh_backup_for_destructive: whether a fresh
                ``backup_database`` snapshot is known to exist for *this*
                startup. Only consulted when a pending migration is in
                :data:`_MANUALLY_REVIEWED_DESTRUCTIVE` **and** the table it
                touches already holds rows — a rebuild against an empty or
                not-yet-existing table has nothing to lose, so a fresh
                database never needs to pass this. See ``_apply_one``.
        """
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self._lock_path), timeout=self._lock_timeout)
        try:
            with lock:
                return self._run_pending_locked(
                    require_fresh_backup_for_destructive=require_fresh_backup_for_destructive
                )
        except Timeout as exc:
            raise MigrationError(
                f"Timed out after {self._lock_timeout}s waiting for the migration lock at "
                f"{self._lock_path}. Another supervisor is probably migrating this database."
            ) from exc

    def _run_pending_locked(self, *, require_fresh_backup_for_destructive: bool) -> list[Migration]:
        conn = self._db.connect()
        conn.executescript(_BOOTSTRAP_SQL)
        self._ensure_checksum_column(conn)
        self.verify_checksums(conn)

        applied = self.applied_versions()
        newly_applied: list[Migration] = []

        for migration in discover_migrations(self._versions_dir):
            if migration.version in applied:
                continue
            self._apply_one(
                conn,
                migration,
                require_fresh_backup_for_destructive=require_fresh_backup_for_destructive,
            )
            newly_applied.append(migration)

        if newly_applied:
            self._verify_integrity()
        return newly_applied

    # ----------------------------------------------------------- checksums
    def _ensure_checksum_column(self, conn: sqlite3.Connection) -> None:
        """Add and backfill ``schema_migrations.checksum`` exactly once.

        Fresh databases: no pre-existing rows, so this is an immediate no-op
        steady state — the column exists from the very first migration
        onward. An existing (pre-Phase-10) database: the column is added
        (additive ``ALTER TABLE ... ADD COLUMN``, allowed by
        :func:`_reject_destructive`) and every already-applied row is
        backfilled from the file *currently on disk* for that version — a
        one-time trust point ("the file on disk right now is presumed
        correct"), not tamper detection; :meth:`verify_checksums` is what
        detects tampering, and only after this baseline is established.
        Fails closed rather than silently completing a partial backfill: a
        crash between adding the column and finishing the backfill leaves
        some rows with ``checksum IS NULL`` despite the column existing,
        which is refused on the next attempt rather than treated as "nothing
        to backfill".
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(schema_migrations)")}
        if "checksum" not in columns:
            conn.execute("ALTER TABLE schema_migrations ADD COLUMN checksum TEXT")

        unchecksummed = conn.execute(
            "SELECT version FROM schema_migrations WHERE checksum IS NULL"
        ).fetchall()
        if not unchecksummed:
            return

        on_disk = {m.version: m for m in discover_migrations(self._versions_dir)}
        backfilled = 0
        for row in unchecksummed:
            version = str(row["version"])
            migration = on_disk.get(version)
            if migration is None:
                raise MigrationError(
                    f"Applied migration version {version!r} has no corresponding file in "
                    f"{self._versions_dir}; cannot establish a checksum baseline for it. "
                    "Refusing to start rather than silently skip verification for this "
                    "version. Restore the original migration file, or resolve manually."
                )
            conn.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
                (migration.checksum, version),
            )
            backfilled += 1

        if backfilled:
            log.warning(
                "migration checksum baseline established for %d pre-existing row(s) — "
                "a one-time trust point (the file on disk right now is presumed correct), "
                "not tamper detection",
                backfilled,
            )

    def verify_checksums(self, conn: sqlite3.Connection | None = None) -> None:
        """Refuse to start if any already-applied migration's file has changed.

        Runs before any pending migration is applied, for every mode
        (paper included) — a hand-edited migration file is exactly the
        tampering scenario the controlled-live phase must catch, and it is
        cheap enough to check on every startup rather than only live ones.
        """
        connection = conn if conn is not None else self._db.connect()
        self._ensure_checksum_column(connection)
        on_disk = {m.version: m for m in discover_migrations(self._versions_dir)}
        rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations"
        ).fetchall()
        for row in rows:
            version = str(row["version"])
            migration = on_disk.get(version)
            if migration is None:
                # Deliberately not an error here: a versions_dir scoped to a
                # subset of history (as many tests do) legitimately omits
                # earlier files. discover_migrations() would already have
                # refused a genuinely missing *pending* migration's gap; a
                # recorded-but-not-discoverable version is only a problem if
                # we were about to trust its checksum, which we are not.
                continue
            if migration.checksum != row["checksum"]:
                raise MigrationError(
                    f"Migration {version} ({row['name']}) has changed since it was applied: "
                    f"recorded checksum {row['checksum']!r} != current file checksum "
                    f"{migration.checksum!r}. Refusing to start. A migration file must never "
                    "be edited after it has been applied — add a new migration instead."
                )

    def _apply_one(
        self,
        conn: sqlite3.Connection,
        migration: Migration,
        *,
        require_fresh_backup_for_destructive: bool = False,
    ) -> None:
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

        This replay-safety argument is weaker for a
        :data:`_MANUALLY_REVIEWED_DESTRUCTIVE` migration, whose script
        necessarily contains a genuine ``DROP TABLE`` — a crash in the narrow
        window between that drop and the following rename leaves no table of
        that name at all, which a plain re-run cannot recover from by
        replaying the same script (the ``SELECT ... FROM <old name>`` step
        would find nothing). :meth:`_check_destructive_preconditions` is the
        safety net for that: it refuses to even start such a migration
        against a non-empty table without an operator-confirmed fresh
        backup, and it treats the ambiguous halfway state itself (rebuilt
        table present, original gone, migration not yet recorded) as a hard
        stop requiring manual recovery from that backup — never a guess.
        """
        script = migration.path.read_text(encoding="utf-8")
        _reject_destructive(migration, script)

        if migration.version in _MANUALLY_REVIEWED_DESTRUCTIVE:
            self._check_destructive_preconditions(
                conn, migration, require_fresh_backup=require_fresh_backup_for_destructive
            )

        try:
            conn.executescript(script)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at, checksum) "
                "VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    datetime.now(UTC).isoformat(),
                    migration.checksum,
                ),
            )
        except sqlite3.Error as exc:
            raise MigrationError(f"Migration {migration.path.name} failed: {exc}") from exc

    def _check_destructive_preconditions(
        self, conn: sqlite3.Connection, migration: Migration, *, require_fresh_backup: bool
    ) -> None:
        """Guard the one reviewed-destructive migration this runner allows.

        Specific to ``0006`` (widens ``orders.status``'s CHECK — see its own
        module docstring). Generalised just enough to stay honest about what
        it actually checks: the *table* a reviewed-destructive migration
        touches, not a generic property of any future one.
        """
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

        rebuilt_name_present = "orders_new" in tables
        original_name_present = "orders" in tables

        if rebuilt_name_present and not original_name_present:
            # The narrow, ambiguous crash window: the rebuild's copy step may
            # or may not have finished before the process died. Guessing
            # either way risks silent data loss or a table stuck with the
            # narrow CHECK forever. Refuse outright.
            raise MigrationError(
                f"Migration {migration.path.name}: found 'orders_new' with no 'orders' table — "
                "a previous attempt at this migration was interrupted between its DROP and "
                "RENAME steps. Refusing to guess whether the copy completed. Restore the "
                "pre-migration backup and retry, rather than resuming automatically."
            )

        if original_name_present:
            count = conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
            if count > 0 and not require_fresh_backup:
                raise MigrationError(
                    f"Migration {migration.path.name} would rebuild 'orders' "
                    f"({count} existing row(s)) and is a reviewed-destructive exception "
                    "to the no-DROP rule. Refusing without confirmation that a fresh "
                    "backup_database() snapshot exists for this startup — pass "
                    "run_pending(require_fresh_backup_for_destructive=True) only once that "
                    "snapshot has actually been taken."
                )

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
