-- Margin-decision snapshots for the positional multi-leg engine (spec
-- section 3.7/6.3; request item 2 of the strategy-weekly-delta-neutral
-- gap-closing task). Generic to any positional strategy — nothing here is
-- specific to weekly_delta_neutral.
--
-- Correction from independent review: do NOT widen strategy_cycles with an
-- ALTER TABLE. SQLite has no supported `ADD COLUMN IF NOT EXISTS`, so an
-- ALTER TABLE migration is not safely replayable after a partial apply the
-- way `MigrationRunner` requires every other migration in this tree to be
-- (see this file's own CREATE ... IF NOT EXISTS statements, and 0010's own
-- header comment making the same choice for the same reason). A dedicated,
-- additive side table sidesteps this entirely and is replay-safe by
-- construction.
--
-- Write-once, append-only, exactly like cycle_decision_snapshots (0010): one
-- row per accepted basket-margin estimate. In practice this is written once
-- per cycle (the entry decision, spec section 5.1's pre-effect checkpoint),
-- but the table does not assume that — a future positional strategy that
-- re-estimates margin at another decision point may add rows freely.

CREATE TABLE IF NOT EXISTS strategy_cycle_margin_snapshots (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id             TEXT    NOT NULL,
    strategy_id            TEXT    NOT NULL,
    execution_mode         TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    cycle_id               TEXT    NOT NULL,
    -- e.g. 'ENTRY_CANDIDATE' — free text, same reasoning as
    -- strategy_cycle_events.event_type / cycle_decision_snapshots.decision_type:
    -- an audit trail, not a decision input, so a new decision point never
    -- needs a migration.
    decision_type          TEXT    NOT NULL,
    estimated_margin       REAL    NOT NULL,
    -- 'dhan_margin_calculator_summed_legs' in production; 'conservative_model_v1'
    -- only ever from an explicitly-injected offline/test fallback — see
    -- common.margin's own module docstring. Free text, not a CHECK-constrained
    -- enum, so a new source never needs a migration either.
    source                 TEXT    NOT NULL,
    estimated_at           TEXT    NOT NULL,
    allocated_capital      REAL    NOT NULL,
    utilization_percent    REAL    NOT NULL,
    -- The entry decision/correlation identity this snapshot was accepted
    -- alongside — ties a margin snapshot back to the same intended entry
    -- cycle_decision_snapshots rows (if any) and the leg correlation IDs
    -- persisted at the same pre-effect checkpoint.
    correlation_id         TEXT,
    created_at             TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_strategy_cycle_margin_snapshots_cycle
    ON strategy_cycle_margin_snapshots (cycle_id, created_at);
