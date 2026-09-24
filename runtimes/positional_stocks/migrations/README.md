# `positional_stocks` migrations

The **own** migration set of `positional_stocks.db` (spec 9, v1.2i), applied by
the shared `MigrationRunner` through its `versions_dir` argument — see
`runtimes/positional_stocks/database.py`. The shared rules apply unchanged:
forward-only, one file per version, every statement `IF NOT EXISTS`, no
destructive statements, checksums verified at every open.

**Nothing here may be copied into `common/persistence/migrations/versions/`.**
The two paper runtimes migrate from that directory at every start and refuse to
start if an applied file there is edited or missing.

**Disposable until Phase 5.** `positional_stocks.db` holds only paper history
that has not started yet. If a migration here is edited during development,
delete `data/operational/positional_stocks.db` rather than work around the
checksum check.

Every table is prefixed `stock_` and carries `strategy_id`.
