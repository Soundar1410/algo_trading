# Migration versions

Sequential, forward-only SQL migrations. One file per migration:

```
0001_walking_skeleton.sql
0002_paper_foundation.sql
```

The filename is the identity — `0001` is the version, `walking_skeleton` the
name — and both are recorded in `schema_migrations` with the applied timestamp.

## Rules

1. **Additive by default.** No `DROP`, `DELETE FROM`, `TRUNCATE` or
   `ALTER ... DROP`. The runner rejects them unless the exact migration is
   explicitly reviewed in `_MANUALLY_REVIEWED_DESTRUCTIVE` and its backup/
   recovery preconditions pass.
2. **Every statement idempotent.** `CREATE TABLE IF NOT EXISTS`,
   `CREATE INDEX IF NOT EXISTS`. The runner rejects a bare `CREATE`.

Both rules exist because `sqlite3.executescript()` implicitly commits before it
runs, so a migration cannot be applied and recorded in one transaction. Safety
comes from replay instead: a crash between applying and recording leaves the
next startup re-running a script that is a no-op, then recording it. See
`MigrationRunner._apply_one`.

Applied migration **checksums are enforced** at every startup. Editing or deleting
an applied migration file stops startup; create a new migration instead. Databases
created before checksum support receive a one-time baseline only when every
applied migration file is present.

## Status

The production migrations in this directory are active and forward-only. The
separate account-shared history lives under `../account_versions/` and is driven
by the same runner and checksum rules.
