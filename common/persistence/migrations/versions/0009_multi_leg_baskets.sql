-- straddle_920 / generic multi-leg support (Phase: strategy-straddle-920).
--
-- Two new, generic tables — not specific to any one strategy — for the
-- durable basket/leg lifecycle a multi-leg strategy (MultiLegEngine) needs
-- and that neither `positions` nor the append-only `trade_ledger` (0008) can
-- represent on their own:
--
-- * `positions` is one row per (strategy, mode, date, security_id), reused
--   across a same-day reopen — it has no notion of "basket" or "which leg
--   instance", and cannot hold a leg that has no fill yet.
-- * `trade_ledger` is append-only and only ever gains a row once a leg's
--   closing fill lands — it cannot represent a leg still PENDING_* or one
--   that FAILED/EXPIRED before ever filling.
--
-- `strategy_baskets` / `strategy_legs` are the missing *current-lifecycle*
-- projection: mutable, upserted on every state transition, one row per
-- basket and one row per leg *instance* respectively. Closing and reopening
-- the same security the same day is a new leg_id and therefore a new
-- `strategy_legs` row — the existing leg's row is never overwritten, which
-- is what makes this an append-only history of leg *instances* even though
-- each individual instance's own row is mutated in place as that one leg
-- moves through PENDING_* -> OPEN -> CLOSED/FAILED/EXPIRED.
--
-- basket_id/leg_id deliberately reuse the identity space `order_intents`
-- already carries (`order_intents.basket_id`/`.leg_id`, added ahead of any
-- consumer) — so a leg's full lifecycle is traceable end to end via the same
-- two identifiers across order_intents -> orders -> fills -> strategy_legs,
-- and a closed leg's row in the append-only `trade_ledger` (0008) is reached
-- from here via `trade_ledger.exit_correlation_id -> orders.correlation_id
-- -> orders.intent_id -> order_intents.id -> order_intents.basket_id/leg_id`
-- rather than by widening `trade_ledger`'s own schema — `trade_ledger`
-- remains the sole append-only historical authority for completed trades;
-- these two tables represent only current lifecycle and relationships, never
-- a second history.
--
-- Both tables are pure `CREATE TABLE IF NOT EXISTS` — no `ALTER TABLE`, so
-- this migration is trivially replay-safe on a crash-and-retry, matching
-- every other additive migration in this tree.

CREATE TABLE IF NOT EXISTS strategy_baskets (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id                TEXT    NOT NULL,
    strategy_id               TEXT    NOT NULL,
    execution_mode            TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    trading_date              TEXT    NOT NULL,
    basket_id                 TEXT    NOT NULL,
    -- OPEN: at least one leg is pending or open, or the basket has not yet
    -- been formally closed for the day. CLOSED: every leg is terminal
    -- (CLOSED/FAILED/EXPIRED) and no further action is permitted this day.
    lifecycle_state           TEXT    NOT NULL DEFAULT 'OPEN'
                              CHECK (lifecycle_state IN ('OPEN', 'CLOSED')),
    -- Spec section 9.2: the day's one primary-entry attempt, consumed before
    -- filter evaluation so a restart can never re-attempt it.
    entries_consumed          INTEGER NOT NULL DEFAULT 0 CHECK (entries_consumed IN (0, 1)),
    day_blocked_reason        TEXT,
    -- Spec section 12.2: at most one leg-doubling adjustment per day.
    adjustment_count          INTEGER NOT NULL DEFAULT 0,
    pending_replacement_role  TEXT,
    pending_replacement_state TEXT,
    -- Spec section 13.4: the original two-leg opening premium sum, captured
    -- once and never rebased by a later adjustment/replacement.
    original_combined_basis   REAL,
    square_off_state          TEXT    NOT NULL DEFAULT 'PENDING'
                              CHECK (square_off_state IN ('PENDING', 'IN_PROGRESS', 'COMPLETED')),
    -- Optimistic-concurrency guard: incremented on every update.
    version                   INTEGER NOT NULL DEFAULT 1,
    created_at                TEXT    NOT NULL,
    updated_at                TEXT    NOT NULL,
    UNIQUE (strategy_id, execution_mode, trading_date, basket_id)
);

CREATE INDEX IF NOT EXISTS idx_strategy_baskets_scope
    ON strategy_baskets (strategy_id, execution_mode, trading_date);

CREATE TABLE IF NOT EXISTS strategy_legs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id            TEXT    NOT NULL,
    strategy_id           TEXT    NOT NULL,
    execution_mode        TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    trading_date          TEXT    NOT NULL,
    basket_id             TEXT    NOT NULL,
    leg_id                TEXT    NOT NULL,
    leg_role              TEXT    NOT NULL CHECK (leg_role IN ('CE', 'PE', 'GENERIC')),
    -- 1 = the original leg for this role; 2+ = successive replacements.
    leg_sequence          INTEGER NOT NULL,
    is_replacement        INTEGER NOT NULL DEFAULT 0 CHECK (is_replacement IN (0, 1)),
    replaces_leg_id        TEXT,
    security_id           TEXT,
    symbol                TEXT,
    strike                REAL,
    expiry                TEXT,
    lot_size              INTEGER,
    side                  TEXT    CHECK (side IS NULL OR side IN ('BUY', 'SELL')),
    quantity              INTEGER,
    entry_price           REAL,
    entry_time            TEXT,
    entry_correlation_id  TEXT,
    exit_price            REAL,
    exit_time             TEXT,
    exit_reason           TEXT,
    exit_correlation_id   TEXT,
    -- Gross (pre-charge) realised P&L for this leg once CLOSED — the same
    -- gross figure the strategy's own risk formulas use; charges/net P&L are
    -- reporting-only and are read from trade_ledger/positions, not stored here.
    realized_gross_pnl    REAL,
    -- CLOSE_SUBMISSION_UNKNOWN: a close was submitted for this leg and its
    -- outcome could not be established (the gateway raised rather than
    -- confirming a fill). Deliberately distinct from both OPEN (nothing
    -- retries the close automatically) and CLOSED (no fill is confirmed) —
    -- see MultiLegEngine._close_leg_safely. Added in the same migration file
    -- (not a new one) because 0009 has never been applied to any real/
    -- operational database — only disposable test databases — and this
    -- branch remains unmerged and under active review.
    state                 TEXT    NOT NULL CHECK (state IN (
                              'PENDING_CONTRACT', 'PENDING_SUBSCRIPTION', 'PENDING_ORDER',
                              'OPEN', 'CLOSED', 'FAILED', 'EXPIRED',
                              'CLOSE_SUBMISSION_UNKNOWN')),
    version               INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL,
    UNIQUE (strategy_id, execution_mode, trading_date, leg_id)
);

CREATE INDEX IF NOT EXISTS idx_strategy_legs_scope
    ON strategy_legs (strategy_id, execution_mode, trading_date, state);

CREATE INDEX IF NOT EXISTS idx_strategy_legs_basket
    ON strategy_legs (basket_id, leg_sequence);
