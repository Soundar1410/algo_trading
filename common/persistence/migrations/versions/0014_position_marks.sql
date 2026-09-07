-- Real-time mark-to-market for open single-leg positions (dashboard "Open
-- Positions" page). Phase 1 of the live-P&L feature — multi-leg
-- (`strategy_legs`) marks are a deliberately separate follow-on phase, not
-- part of this migration.
--
-- One new additive table, no `ALTER TABLE`: SQLite has no `ADD COLUMN IF
-- NOT EXISTS`, so an `ALTER TABLE` migration is not safely replayable after
-- a partial apply the way `MigrationRunner` requires — the same reasoning
-- 0011/0012/0013 already give for choosing a new side table over widening
-- an existing one.
--
-- `position_marks` is the per-runtime-group, both-modes counterpart of the
-- account-shared, live-only `live_position_mtm`
-- (account_versions/0001_account_shared_foundation.sql) — "latest
-- mark-to-market only, overwritten on every mark, never appended", for the
-- identical double-counting reason: summing/joining this table must always
-- reflect the current picture, never an accumulation of repeated snapshots.
-- Unlike `live_position_mtm`, this table is not account-scoped and carries
-- both `paper` and `live` rows, keyed exactly like `positions`' own unique
-- key (0001_walking_skeleton.sql) so a join against an open position is a
-- plain equi-join on all four columns.
--
-- Written once per closed underlying candle, from the same per-candle
-- checkpoint that already persists `highest_favourable`/`lowest_favourable`
-- (`TradingEngine._persist_open_position_checkpoint`) — no new write
-- frequency, matching that checkpoint's own "limitation 24" constraint.
-- `as_of` lets a reader (the dashboard) decide for itself whether a mark is
-- fresh enough to show as current, or a dead worker's last-known price —
-- this table alone never claims freshness.
CREATE TABLE IF NOT EXISTS position_marks (
    strategy_id     TEXT NOT NULL,
    execution_mode  TEXT NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    trading_date    TEXT NOT NULL,
    security_id     TEXT NOT NULL,
    last_price      REAL NOT NULL,
    unrealised_pnl  REAL NOT NULL,
    as_of           TEXT NOT NULL,
    PRIMARY KEY (strategy_id, execution_mode, trading_date, security_id)
);
