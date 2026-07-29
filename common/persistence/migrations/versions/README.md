# Migration versions

Sequential, forward-only SQL migrations. One file per migration:

```
0001_walking_skeleton.sql
0002_paper_foundation.sql
```

The filename is the identity — `0001` is the version, `walking_skeleton` the
name — and both are recorded in `schema_migrations` with the applied timestamp.

## Rules

1. **Additive only.** No `DROP`, `DELETE FROM`, `TRUNCATE` or `ALTER ... DROP`.
   The runner rejects them.
2. **Every statement idempotent.** `CREATE TABLE IF NOT EXISTS`,
   `CREATE INDEX IF NOT EXISTS`. The runner rejects a bare `CREATE`.

Both rules exist because `sqlite3.executescript()` implicitly commits before it
runs, so a migration cannot be applied and recorded in one transaction. Safety
comes from replay instead: a crash between applying and recording leaves the
next startup re-running a script that is a no-op, then recording it. See
`MigrationRunner._apply_one`.

Migration **checksums** are deliberately not implemented — the spec defers them
to the controlled-live phase or to the first genuinely destructive migration.

## Status

Empty by design. Phase 0 ships the runner and the `schema_migrations` table
only. The walking-skeleton tables (`runtime_sessions`, `runtime_heartbeats`,
`signals`, `order_intents`, `orders`, `fills`, `positions`, `strategy_state`,
`notifications`, `errors`) arrive in **Phase 1**, where they gain their first
consumer. Tests drive the runner with fixture migrations under
`tests/fixtures/migrations/`.
