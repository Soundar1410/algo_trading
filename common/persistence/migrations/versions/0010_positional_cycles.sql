-- Positional multi-leg cycle support (weekly_delta_neutral /
-- strategy-weekly-delta-neutral branch). Closes the D69/limitation-30
-- persistence gap `runtimes/positional_options/__init__.py` documents:
-- `positions`, `strategy_state`, `order_intents`, `strategy_baskets` and
-- `strategy_legs` all key their UNIQUE identity on `trading_date`, so a
-- position held across sessions fragments across unrelated rows. This
-- migration adds a durable, immutable `cycle_id` identity plus a
-- generic-to-any-positional-strategy set of tables — nothing here is
-- specific to `weekly_delta_neutral`.
--
-- `trading_date` is untouched (D69's own candidate direction): `positions`,
-- `order_intents`, `orders`, `fills` and `trade_ledger` keep recording the
-- *event* date exactly as before. What changes is how a *position* row is
-- looked up across days once a `cycle_id` is involved — see
-- `common.execution.repository`'s cycle-aware `_read_position`/
-- `_upsert_position`/`reserve_intent` overloads (optional `cycle_id`
-- parameter; every existing intraday call site, which never passes one, is
-- byte-identical to before this migration).
--
-- `order_intents.basket_id`/`.leg_id` — already present, unused-until-
-- straddle_920 columns whose whole point (migration 0009's own header) is
-- reuse by "the next multi-leg strategy" — are reused again here rather
-- than duplicated: a positional order intent sets `basket_id = cycle_id`
-- and `leg_id = <strategy_cycle_legs.leg_id>`, so `order_intents`/`orders`/
-- `fills` already correlate to a cycle with no new binding table. The
-- existing `leg_order_history(leg_id=...)` repository method already works
-- unchanged for a cycle leg's `leg_id` (it filters on `order_intents.leg_id`
-- alone); `cycle_order_history(cycle_id=...)` below is its `basket_id`-keyed
-- sibling for "every order this cycle has ever placed, across every leg".
--
-- Every statement is `CREATE ... IF NOT EXISTS` — no `ALTER TABLE` — so this
-- migration is replay-safe on a crash-and-retry, matching every other
-- additive migration in this tree (see MigrationRunner._apply_one).

CREATE TABLE IF NOT EXISTS strategy_cycles (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id                TEXT    NOT NULL,
    strategy_id               TEXT    NOT NULL,
    execution_mode            TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    -- The durable, immutable identity a position/basket/leg/adjustment/
    -- incident/reconciliation/dashboard row correlates to, independent of
    -- how many `trading_date`s the cycle spans.
    cycle_id                  TEXT    NOT NULL UNIQUE,
    underlying                TEXT    NOT NULL,
    -- Resolved from the exchange's own instrument master before the first
    -- order (spec section 3.4) and never rolled forward on restart —
    -- persisted here, not recomputed from weekday arithmetic.
    resolved_expiry_date      TEXT    NOT NULL,
    -- ENTERING: sequential staged entry under way (per-leg progress lives on
    --   strategy_cycle_legs — this table holds no duplicate per-leg state).
    -- ACTIVE: the intended four-leg exposure is confirmed open; normal
    --   monitoring/adjustment. An in-flight adjustment is a sub-state of
    --   ACTIVE (see pending_adjustment_state), not a separate top-level value
    --   — exactly one thing decides "what phase is this cycle in".
    -- EXITING: an exit trigger has fired; working towards flat.
    -- COMPLETED: flat, terminal, the intended lifecycle finished normally.
    -- FAILED: entry never completed; every filled leg was unwound; no
    --   exposure remains; terminal.
    -- CRITICAL_UNRESOLVED: unmanageable/unknown exposure was found (by entry,
    --   adjustment, exit or restart reconciliation); blocks new entries;
    --   requires operator/controlled reconciliation; deliberately NOT
    --   terminal — see idx_one_open_cycle below.
    -- ABANDONED: reserved for an explicit future operator action; terminal.
    state                     TEXT    NOT NULL DEFAULT 'ENTERING' CHECK (state IN (
                                  'ENTERING', 'ACTIVE', 'EXITING', 'COMPLETED', 'FAILED',
                                  'CRITICAL_UNRESOLVED', 'ABANDONED')),
    -- Spec section 3.2: at most one primary entry attempt per weekly cycle —
    -- this cycle's own existence *is* that attempt, consumed durably before
    -- the first order (spec section 5.1's pre-effect checkpoint) so a crash
    -- immediately after creation can never retry it.
    entries_consumed          INTEGER NOT NULL DEFAULT 0 CHECK (entries_consumed IN (0, 1)),
    day_blocked_reason        TEXT,
    -- Immutable original credit/risk basis (spec section 3.7/6.1) — captured
    -- once, when the intended four-leg exposure is confirmed, and never
    -- rebased by a later adjustment or config change.
    original_net_credit       REAL,
    original_max_loss         REAL,
    original_wing_width       REAL,
    -- Daily counter: resets at a verified new trading date (lifecycle.py),
    -- never at cycle creation and never alongside adjustments_this_cycle.
    adjustments_today         INTEGER NOT NULL DEFAULT 0,
    adjustments_today_date    TEXT,
    -- Cycle counter: persists for the cycle's entire life, however many
    -- trading dates it spans (spec section 7.1: max 3/cycle).
    adjustments_this_cycle    INTEGER NOT NULL DEFAULT 0,
    last_adjustment_at        TEXT,
    -- Mirrors strategy_baskets.pending_replacement_role/_state exactly (same
    -- AdjustmentLifecycle vocabulary, same claim/submit/reconcile machine) —
    -- reused, not reinvented, for the untested-short-roll adjustment.
    pending_adjustment_role   TEXT,
    pending_adjustment_state  TEXT,
    square_off_state          TEXT    NOT NULL DEFAULT 'PENDING'
                              CHECK (square_off_state IN ('PENDING', 'IN_PROGRESS', 'COMPLETED')),
    -- The trading_date the cycle was created on (the entry Wednesday) —
    -- observability only; never part of this table's identity.
    opened_trading_date       TEXT    NOT NULL,
    version                   INTEGER NOT NULL DEFAULT 1,
    created_at                TEXT    NOT NULL,
    updated_at                TEXT    NOT NULL,
    -- Spec section 9.3: one consumed cycle per resolved expiry, for every
    -- outcome (completed, failed, still in flight) — never only the
    -- currently-open one. A same-expiry retry is therefore structurally
    -- impossible, not merely blocked by application logic.
    UNIQUE (runtime_id, strategy_id, execution_mode, resolved_expiry_date)
);

-- Spec section 9.3: one non-terminal cycle per (runtime, strategy, mode).
-- CRITICAL_UNRESOLVED is deliberately included in "non-terminal" here (it is
-- not in the exclusion list) — unresolved exposure must keep blocking a new
-- cycle, never silently permit one alongside it.
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_cycle
    ON strategy_cycles (runtime_id, strategy_id, execution_mode)
    WHERE state NOT IN ('COMPLETED', 'FAILED', 'ABANDONED');

CREATE INDEX IF NOT EXISTS idx_strategy_cycles_scope
    ON strategy_cycles (runtime_id, strategy_id, execution_mode, state);

CREATE TABLE IF NOT EXISTS strategy_cycle_legs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id            TEXT    NOT NULL,
    strategy_id           TEXT    NOT NULL,
    execution_mode        TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    cycle_id              TEXT    NOT NULL,
    leg_id                TEXT    NOT NULL,
    -- Widened past strategy_legs' own CHECK (CE/PE/GENERIC, migration 0009,
    -- deliberately untouched — see common.engine.multi_leg_models.LegRole's
    -- own docstring) to also accept the four positional-only roles.
    leg_role              TEXT    NOT NULL CHECK (leg_role IN (
                              'CE', 'PE', 'GENERIC',
                              'SHORT_CALL', 'SHORT_PUT', 'HEDGE_CALL', 'HEDGE_PUT')),
    -- Redundant with leg_role (SHORT_CALL/HEDGE_CALL -> CE, SHORT_PUT/
    -- HEDGE_PUT -> PE) but persisted directly so a dashboard read model never
    -- has to re-derive it, and so a row can be sanity-checked against its own
    -- role without importing the mapping.
    option_type           TEXT    CHECK (option_type IS NULL OR option_type IN ('CE', 'PE')),
    leg_sequence          INTEGER NOT NULL,
    is_replacement        INTEGER NOT NULL DEFAULT 0 CHECK (is_replacement IN (0, 1)),
    replaces_leg_id       TEXT,
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
    realized_gross_pnl    REAL,
    -- Same eight-value vocabulary as strategy_legs.state (migration 0009) —
    -- one shared LegState enum, reused verbatim.
    state                 TEXT    NOT NULL CHECK (state IN (
                              'PENDING_CONTRACT', 'PENDING_SUBSCRIPTION', 'PENDING_ORDER',
                              'OPEN', 'CLOSED', 'FAILED', 'EXPIRED',
                              'CLOSE_SUBMISSION_UNKNOWN')),
    version               INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL,
    UNIQUE (cycle_id, leg_id)
);

CREATE INDEX IF NOT EXISTS idx_strategy_cycle_legs_cycle
    ON strategy_cycle_legs (cycle_id, leg_role, leg_sequence);

CREATE INDEX IF NOT EXISTS idx_strategy_cycle_legs_scope
    ON strategy_cycle_legs (strategy_id, execution_mode, state);

-- Append-only adjustment ledger (spec section 7.3): one row per completed
-- claim, whatever its outcome. The mutable in-flight claim itself lives on
-- strategy_cycles.pending_adjustment_role/_state; a row here is written once
-- the claim reaches a terminal AdjustmentLifecycle outcome for this attempt.
CREATE TABLE IF NOT EXISTS strategy_cycle_adjustments (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id                TEXT    NOT NULL,
    strategy_id               TEXT    NOT NULL,
    execution_mode            TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    cycle_id                  TEXT    NOT NULL,
    -- 1, 2, 3 — the adjustments_this_cycle counter's value the moment this
    -- attempt was durably claimed.
    adjustment_sequence       INTEGER NOT NULL,
    trigger_reason            TEXT    NOT NULL,
    target_leg_id             TEXT    NOT NULL,
    replacement_leg_id        TEXT,
    claimed_at                TEXT    NOT NULL,
    pre_adjustment_net_delta  REAL,
    post_adjustment_net_delta REAL,
    realized_pnl              REAL,
    charges                   REAL,
    -- Terminal AdjustmentLifecycle value for this attempt (EXIT_UNKNOWN,
    -- REPLACEMENT_FILLED, REPLACEMENT_FAILED, REPLACEMENT_EXPIRED, ...).
    lifecycle_state           TEXT    NOT NULL,
    created_at                TEXT    NOT NULL,
    UNIQUE (cycle_id, adjustment_sequence)
);

CREATE INDEX IF NOT EXISTS idx_strategy_cycle_adjustments_cycle
    ON strategy_cycle_adjustments (cycle_id, adjustment_sequence);

-- Append-only cycle history: state transitions, entry attempts, exit
-- progress, restart adoption, incidents — the dashboard's Signals & Events
-- equivalent for a cycle. Deliberately generic free-text `event_type`/
-- `detail` rather than a closed enum: this table is an audit trail, not a
-- decision input, so a new event type never needs a migration.
CREATE TABLE IF NOT EXISTS strategy_cycle_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id    TEXT    NOT NULL,
    strategy_id   TEXT    NOT NULL,
    execution_mode TEXT   NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    cycle_id      TEXT    NOT NULL,
    event_type    TEXT    NOT NULL,
    detail        TEXT,
    trading_date  TEXT    NOT NULL,
    occurred_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_strategy_cycle_events_cycle
    ON strategy_cycle_events (cycle_id, occurred_at);

-- The key to cross-day positions (spec review correction 4): resolves a
-- cycle-scoped position lookup to the one `positions` row that security has
-- ever had for this cycle, however many `trading_date`s of fills it has
-- absorbed. Written in the *same* database transaction as the position
-- mutation it binds (common.execution.repository._upsert_position /
-- apply_fill) — never a separate write, so a binding failure rolls back the
-- position write it was guarding, and a position write can never commit
-- without a matching binding.
--
-- Once created for a (cycle_id, security_id) pair, a binding's `position_id`
-- is never rewritten to point at a different position row: cycle-scoped
-- resolution reuses the same `positions` row for the security's entire life
-- inside this cycle (including a close-and-reopen — see
-- `_upsert_position`'s existing "reopening a fully-closed identity from
-- flat" branch, which now fires against the *same* row rather than creating
-- a new one once a binding exists). A write that would rebind an existing
-- pair to a different position_id is a contradictory-binding defect and is
-- refused (see the repository method's own docstring), not repaired.
CREATE TABLE IF NOT EXISTS cycle_position_bindings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id     TEXT    NOT NULL,
    security_id  TEXT    NOT NULL,
    position_id  INTEGER NOT NULL,
    created_at   TEXT    NOT NULL,
    UNIQUE (cycle_id, security_id)
);

CREATE INDEX IF NOT EXISTS idx_cycle_position_bindings_position
    ON cycle_position_bindings (position_id);

-- The quote/Greek snapshot behind one risk-increasing decision (entry or
-- adjustment candidate evaluation), with every input and its source
-- timestamp — spec section 4: "every Greek has a source timestamp and
-- maximum age" and "all four candidate legs use a consistent evaluation
-- snapshot." Write-once, append-only; never updated.
CREATE TABLE IF NOT EXISTS cycle_decision_snapshots (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id               TEXT    NOT NULL,
    strategy_id              TEXT    NOT NULL,
    execution_mode           TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    cycle_id                 TEXT    NOT NULL,
    -- e.g. 'ENTRY_CANDIDATE', 'ADJUSTMENT_CANDIDATE' — free text, same
    -- reasoning as strategy_cycle_events.event_type.
    decision_type            TEXT    NOT NULL,
    leg_role                 TEXT,
    security_id              TEXT    NOT NULL,
    option_type              TEXT    CHECK (option_type IS NULL OR option_type IN ('CE', 'PE')),
    strike                   REAL,
    spot                     REAL,
    bid                      REAL,
    ask                      REAL,
    quote_age_ms             REAL,
    quote_source_timestamp   TEXT,
    delta                    REAL,
    gamma                    REAL,
    theta                    REAL,
    vega                     REAL,
    implied_volatility       REAL,
    -- 'BROKER_CHAIN' | 'MODEL' — common.greeks.models.GreekSource.
    greek_source             TEXT    CHECK (greek_source IS NULL OR
                                             greek_source IN ('BROKER_CHAIN', 'MODEL')),
    greek_source_timestamp   TEXT,
    risk_free_rate           REAL,
    dividend_yield           REAL,
    evaluation_timestamp     TEXT    NOT NULL,
    time_to_expiry_years     REAL,
    created_at               TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cycle_decision_snapshots_cycle
    ON cycle_decision_snapshots (cycle_id, created_at);
