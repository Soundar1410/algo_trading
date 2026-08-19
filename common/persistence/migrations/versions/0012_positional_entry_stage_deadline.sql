-- Bounded staged-entry timeout for the positional multi-leg engine (Phase 5A
-- gap-closing correction: cross-evaluation staged-entry resume must be
-- bounded, not resume-forever). Generic to any positional strategy — nothing
-- here is specific to weekly_delta_neutral.
--
-- Correction from independent review, same reasoning as 0011's own header:
-- do NOT widen strategy_cycles with an ALTER TABLE. SQLite has no supported
-- `ADD COLUMN IF NOT EXISTS`, so an ALTER TABLE migration is not safely
-- replayable after a partial apply the way `MigrationRunner` requires every
-- other migration in this tree to be. A dedicated, additive side table
-- sidesteps this entirely and is replay-safe by construction.
--
-- Unlike the append-only strategy_cycle_* history tables, this one row per
-- cycle_id is genuinely mutable (upserted), exactly like strategy_cycles and
-- strategy_cycle_legs themselves already are in their own migration (0010) —
-- "no ALTER TABLE" only ever meant no *schema* change to an existing table,
-- never that a fresh table's own rows can't be updated in place.
--
-- entry_stage_role/entry_stage_deadline_at together are the durable "which
-- staged-entry step is in flight, and by when must it resolve" clock: armed
-- (persisted here) before the dynamic subscription for that step's contract
-- is ever requested, and preserved verbatim across a restart — never
-- recomputed from the restart's own clock (common.engine.positional.
-- positional_engine.PositionalMultiLegEngine._advance_entry_stage). At most
-- one stage is ever in flight (entry is strictly sequential), so one row's
-- worth of state per cycle is enough.

CREATE TABLE IF NOT EXISTS strategy_cycle_entry_stage (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id              TEXT    NOT NULL,
    strategy_id             TEXT    NOT NULL,
    execution_mode          TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    cycle_id                TEXT    NOT NULL UNIQUE,
    -- NULL: no staged-entry step currently in flight (entry not yet started,
    -- already complete, or already failed/unwound).
    entry_stage_role        TEXT    CHECK (entry_stage_role IS NULL OR entry_stage_role IN (
                                'SHORT_CALL', 'SHORT_PUT', 'HEDGE_CALL', 'HEDGE_PUT')),
    entry_stage_deadline_at TEXT,
    version                 INTEGER NOT NULL DEFAULT 1,
    created_at              TEXT    NOT NULL,
    updated_at              TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_strategy_cycle_entry_stage_cycle
    ON strategy_cycle_entry_stage (cycle_id);
