-- Durable repeated-roll support for the intraday multi-leg engine
-- (strategy-rolling-strangle-otm1 branch, Phase 0/1). Generic to any
-- multi-leg strategy that rolls a threatened leg repeatedly with
-- independent per-role budgets -- nothing here is specific to
-- rolling_strangle_otm1.
--
-- Migration 0009's strategy_baskets carries exactly one global
-- adjustment_count and one pending_replacement_role/_state slot -- correct
-- for straddle_920's single sole-adjustment-per-day lifecycle (0009's own
-- header), structurally unable to represent two independent per-role
-- counters, a shared re-anchored reference spot, or two concurrent
-- in-flight claims (one CE roll and one PE roll at once, spec section 9.3).
-- straddle_920's scalar columns are untouched by this migration and remain
-- the single-adjustment projection they always were.
--
-- Two tables, both additive side tables -- no ALTER TABLE on
-- strategy_baskets (SQLite has no supported `ADD COLUMN IF NOT EXISTS`, so
-- an ALTER TABLE migration is not safely replayable after a partial apply
-- the way MigrationRunner requires; see 0011/0012's identical reasoning).
--
-- (a) strategy_basket_roll_anchor -- mutable, one row per basket, the same
-- "genuinely mutable, upserted in place" shape as 0012's
-- strategy_cycle_entry_stage: the shared reference spot a roll re-anchors
-- to on every claim (spec section 9.2 step 5), armed once at primary entry
-- and updated at every roll claim, never recomputed from a restart's own
-- clock.
--
-- (b) strategy_basket_rolls -- one row per (basket_id, leg_role,
-- roll_sequence), deliberately a hybrid of two existing patterns rather
-- than a copy of either:
--   * like 0010's strategy_cycle_adjustments, `roll_sequence` is a durable,
--     append-only-by-construction identity (UNIQUE (basket_id, leg_role,
--     roll_sequence) below) that also IS the per-role roll counter --
--     MAX(roll_sequence) for a role, no separate counter column needed;
--   * unlike strategy_cycle_adjustments (whose docstring frames it as
--     written once a claim reaches a terminal outcome, with the *mutable*
--     in-flight state living instead on strategy_cycles.pending_adjustment_
--     role/_state -- necessarily a single slot on the parent row, since
--     that engine allows only one in-flight adjustment at a time), this
--     table's own row is the mutable in-flight claim: created at claim time
--     with lifecycle_state = 'CLAIMED' and mutated in place
--     (CLAIMED -> EXIT_SUBMISSION_PENDING -> a terminal AdjustmentLifecycle
--     value) via the same ON CONFLICT ... DO UPDATE upsert
--     append_cycle_adjustment already uses. Putting the mutable claim on a
--     per-(role, roll_sequence) row, instead of a single slot on the parent
--     basket, is exactly what makes two independent, *concurrent* in-flight
--     claims representable (spec section 9.3's both-leg recentre mode) --
--     the one thing the existing single-slot pattern cannot do.
--
-- close_correlation_id/close_intent_id are deliberately nullable and
-- deliberately NOT the "stable identity" naively assumed at first: a target
-- whose adjustment close is definitively rejected/cancelled reconstructs its
-- leg as OPEN with its roll claim FAILED and its budget consumed, and that
-- same leg is later, legitimately, closed again by hard square-off through
-- the ordinary (non-roll) gateway verbs -- a second, unrelated exit-side
-- order_intents row for the same leg_id. Reconciling this claim must
-- therefore key on its own close_intent_id, never on "the" exit row for
-- target_leg_id. Both columns are written atomically with the transition off
-- 'CLAIMED' (never before) in the same repository transaction that reserves
-- the order_intents row -- see repository.reserve_roll_close_intent -- so
-- 'the intent exists but the claim does not know its own identity yet' is
-- not a reachable state.
--
-- lifecycle_state reuses common.engine.multi_leg_models.AdjustmentLifecycle's
-- existing vocabulary verbatim (CLAIMED, EXIT_SUBMISSION_PENDING,
-- EXIT_UNKNOWN, EXIT_CONFIRMED, AWAITING_NEXT_CANDLE, REPLACEMENT_PENDING,
-- REPLACEMENT_FILLED/_FAILED/_EXPIRED, plus FAILED for a definitively
-- rejected/cancelled close) rather than inventing a parallel one -- no CHECK
-- constraint, matching 0009's pending_replacement_state, since the
-- vocabulary is validated by the application enum on load, fail-closed on
-- an unrecognised value, exactly as pending_replacement_role already is
-- (multi_leg_state.py).
--
-- leg_role is TEXT with NO CHECK constraint -- deliberately not a
-- restrictive CE/PE-only (or even CE/PE/GENERIC-only) list. It is validated
-- on load through the full common.engine.multi_leg_models.LegRole enum
-- (CE, PE, GENERIC, SHORT_CALL, SHORT_PUT, HEDGE_CALL, HEDGE_PUT and any
-- future member), fail-closed on an unrecognised value -- the same pattern
-- 0009's strategy_baskets.pending_replacement_role already uses. This is
-- required for this table to be honestly reusable by a future rolling
-- multi-leg strategy: a three-value CE/PE/GENERIC CHECK would need its own
-- migration the moment such a strategy used a positional-shaped role.
--
-- Every statement is CREATE ... IF NOT EXISTS -- no ALTER TABLE -- so this
-- migration is replay-safe on a crash-and-retry, matching every other
-- additive migration in this tree (see MigrationRunner._apply_one).

CREATE TABLE IF NOT EXISTS strategy_basket_roll_anchor (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id          TEXT    NOT NULL,
    strategy_id         TEXT    NOT NULL,
    execution_mode      TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    trading_date        TEXT    NOT NULL,
    basket_id           TEXT    NOT NULL,
    -- The shared reference spot every roll re-anchors to (spec section 9.2
    -- step 5) -- set once at primary entry, updated at every roll claim's
    -- own atomic commit (never at replacement fill time).
    reference_price     REAL,
    -- The decision timestamp of the completed candle that most recently set
    -- reference_price -- distinct from claimed_at (wall-clock write time) on
    -- strategy_basket_rolls, so a restart can tell "which candle" from
    -- "when persisted".
    anchor_candle_ts    TEXT,
    version             INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    UNIQUE (strategy_id, execution_mode, trading_date, basket_id)
);

CREATE INDEX IF NOT EXISTS idx_strategy_basket_roll_anchor_scope
    ON strategy_basket_roll_anchor (strategy_id, execution_mode, trading_date);

CREATE TABLE IF NOT EXISTS strategy_basket_rolls (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id                  TEXT    NOT NULL,
    strategy_id                 TEXT    NOT NULL,
    execution_mode              TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    trading_date                TEXT    NOT NULL,
    basket_id                   TEXT    NOT NULL,
    -- Ties together every target claimed atomically in one roll event (one
    -- row per target role; a single-leg roll has exactly one member, a
    -- both-leg recentre has exactly two). Replacement is permitted for a
    -- role only once every row sharing its claim_group_id has reached
    -- EXIT_CONFIRMED.
    claim_group_id               TEXT    NOT NULL,
    -- Validated through common.engine.multi_leg_models.LegRole on load,
    -- fail-closed -- see header. No CHECK constraint (deliberate: see
    -- header).
    leg_role                     TEXT    NOT NULL,
    -- 1, 2, 3, ... -- this role's own roll count the moment this attempt
    -- was durably claimed. MAX(roll_sequence) for a role, across every row,
    -- is that role's current roll count; no separate counter column.
    roll_sequence                INTEGER NOT NULL,
    -- common.engine.multi_leg_models.AdjustmentLifecycle's vocabulary, plus
    -- 'FAILED' for a definitively rejected/cancelled close (see header). No
    -- CHECK constraint -- validated by the application enum on load,
    -- fail-closed, matching pending_replacement_state's existing pattern.
    lifecycle_state               TEXT    NOT NULL,
    -- The concrete leg instance this claim closes. Always known at claim
    -- time -- never inferred from role alone (0009's own
    -- pending_replacement_role could not say *which* leg, only which role).
    target_leg_id                 TEXT    NOT NULL,
    -- Nullable: NULL while lifecycle_state = 'CLAIMED' (no submission
    -- authorised yet); populated atomically, together, with the transition
    -- to 'EXIT_SUBMISSION_PENDING' -- see header and
    -- repository.reserve_roll_close_intent. Never the leg's *only* exit
    -- intent in general (a later, unrelated square-off close of the same
    -- leg is legitimate) -- this claim's own outcome is reconciled through
    -- close_intent_id alone, never by scanning every exit row for
    -- target_leg_id.
    close_correlation_id          TEXT,
    close_intent_id               INTEGER,
    -- The fresh leg instance opened once every claim_group_id member is
    -- EXIT_CONFIRMED and a strictly-later completed candle before the
    -- cutoff arrives. NULL until REPLACEMENT_PENDING.
    replacement_leg_id            TEXT,
    -- The reference_price this specific claim was made against (a copy of
    -- strategy_basket_roll_anchor.reference_price at claim time, spec
    -- section 9.2 step 5) -- kept per-row so a restart can show the exact
    -- price that triggered *this* roll even after the anchor has since
    -- moved on to a later roll.
    reference_price_at_claim      REAL,
    -- The decision timestamp of the completed candle whose evaluation
    -- produced this claim (spec section 9.2 steps 4-5) -- distinct from
    -- claimed_at (wall-clock write time).
    claim_candle_ts                TEXT    NOT NULL,
    claimed_at                     TEXT    NOT NULL,
    version                        INTEGER NOT NULL DEFAULT 1,
    updated_at                     TEXT    NOT NULL,
    -- Structurally prevents a post-crash retry from reclaiming the same
    -- role's next roll count twice -- the real guarantee, not merely a
    -- convention (0009's own strategy_baskets.version column shows the
    -- limits of a convention-only approach: it is incremented but never
    -- read back in a WHERE clause anywhere in this codebase).
    UNIQUE (basket_id, leg_role, roll_sequence)
);

CREATE INDEX IF NOT EXISTS idx_strategy_basket_rolls_scope
    ON strategy_basket_rolls (strategy_id, execution_mode, trading_date, basket_id);

CREATE INDEX IF NOT EXISTS idx_strategy_basket_rolls_group
    ON strategy_basket_rolls (claim_group_id);

CREATE INDEX IF NOT EXISTS idx_strategy_basket_rolls_target_leg
    ON strategy_basket_rolls (target_leg_id);
