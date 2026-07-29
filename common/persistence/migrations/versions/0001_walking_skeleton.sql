-- Walking-skeleton tables (spec section 11.5, "Tables by delivery stage").
--
-- Exactly the ten tables the first vertical slice needs. Deliberately absent:
-- strategy_registry, order_events, position_events and risk_events (the
-- paper-foundation expansion), and reconciliation_runs / reconciliation_mismatches
-- (controlled-live only). The spec is explicit that live-only tables must not
-- become a prerequisite for paper forward testing.
--
-- Two invariants run through every trading table:
--
--   * strategy_id  — every record belongs to one strategy instance, so a query
--                    can never silently blend two strategies' P&L.
--   * execution_mode with a CHECK constraint — paper and live records live in
--                    the same group database and must be impossible to mix by
--                    accident. The CHECK means a typo ('Paper', 'PAPER') is a
--                    write failure, not a row that quietly escapes both filters.
--
-- Every CREATE uses IF NOT EXISTS: the runner replays an unrecorded migration
-- after a crash rather than relying on transactional atomicity, which
-- sqlite3.executescript() cannot provide (see MigrationRunner._apply_one).

-- --------------------------------------------------------------- sessions
CREATE TABLE IF NOT EXISTS runtime_sessions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id            TEXT    NOT NULL,
    strategy_id           TEXT,
    execution_mode        TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    process_role          TEXT    NOT NULL CHECK (process_role IN ('supervisor', 'worker')),
    pid                   INTEGER NOT NULL,
    config_fingerprint    TEXT,
    started_at            TEXT    NOT NULL,
    -- NULL means "still running or died without shutting down cleanly". Restart
    -- recovery looks for exactly that: the previous incomplete session.
    ended_at              TEXT,
    shutdown_reason       TEXT
);

CREATE INDEX IF NOT EXISTS idx_runtime_sessions_open
    ON runtime_sessions (runtime_id, strategy_id, ended_at);

CREATE TABLE IF NOT EXISTS runtime_heartbeats (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            INTEGER NOT NULL REFERENCES runtime_sessions (id),
    runtime_id            TEXT    NOT NULL,
    strategy_id           TEXT,
    health_state          TEXT    NOT NULL,
    last_tick_at          TEXT,
    queue_depth           INTEGER,
    dropped_events        INTEGER NOT NULL DEFAULT 0,
    beat_at               TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runtime_heartbeats_recent
    ON runtime_heartbeats (runtime_id, strategy_id, beat_at DESC);

-- ---------------------------------------------------------------- signals
CREATE TABLE IF NOT EXISTS signals (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            INTEGER NOT NULL REFERENCES runtime_sessions (id),
    runtime_id            TEXT    NOT NULL,
    strategy_id           TEXT    NOT NULL,
    execution_mode        TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    trading_date          TEXT    NOT NULL,
    instrument            TEXT    NOT NULL,
    security_id           TEXT    NOT NULL,
    side                  TEXT    NOT NULL CHECK (side IN ('BUY', 'SELL')),
    -- The exact candle that produced this signal (spec: "Record the exact candle
    -- snapshot used to produce a signal"). Kept as columns rather than a blob so
    -- a mismatch is greppable in SQL during an incident.
    candle_open           REAL    NOT NULL,
    candle_high           REAL    NOT NULL,
    candle_low            REAL    NOT NULL,
    candle_close          REAL    NOT NULL,
    candle_start_at       TEXT    NOT NULL,
    candle_end_at         TEXT    NOT NULL,
    reference_price       REAL    NOT NULL,
    -- Four separate clocks, per the spec's candle observability rules.
    tick_at               TEXT,
    received_at           TEXT,
    evaluated_at          TEXT    NOT NULL,
    reason                TEXT,
    config_fingerprint    TEXT,
    -- One signal per strategy per candle. This is what makes "one completed
    -- candle creates one order, not two" an enforced property rather than a
    -- hopeful one: a duplicate candle delivery cannot produce a second signal.
    UNIQUE (strategy_id, execution_mode, instrument, candle_end_at)
);

-- ----------------------------------------------------------- order intents
CREATE TABLE IF NOT EXISTS order_intents (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Globally unique across paper and live. The mode-specific namespace is
    -- carried in the value itself (p_ / l_) and repeated in the column below,
    -- so neither a query nor a human can confuse the two.
    correlation_id        TEXT    NOT NULL UNIQUE,
    correlation_namespace TEXT    NOT NULL CHECK (correlation_namespace IN ('paper', 'live')),
    session_id            INTEGER NOT NULL REFERENCES runtime_sessions (id),
    signal_id             INTEGER REFERENCES signals (id),
    runtime_id            TEXT    NOT NULL,
    strategy_id           TEXT    NOT NULL,
    execution_mode        TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    trading_date          TEXT    NOT NULL,
    sequence_number       INTEGER NOT NULL,
    instrument            TEXT    NOT NULL,
    security_id           TEXT    NOT NULL,
    side                  TEXT    NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity              INTEGER NOT NULL CHECK (quantity > 0),
    order_type            TEXT    NOT NULL CHECK (order_type IN ('MARKET', 'LIMIT')),
    limit_price           REAL,
    trigger_price         REAL,
    product_type          TEXT    NOT NULL,
    basket_id             TEXT,
    leg_id                TEXT,
    config_fingerprint    TEXT,
    risk_decision         TEXT    NOT NULL,
    risk_reason           TEXT,
    -- Set in the same transaction as the insert, before the broker is called.
    -- A submitted-but-unacknowledged intent is what restart recovery must
    -- reconcile; without this flag a crash mid-submission is indistinguishable
    -- from a crash before it.
    submission_reserved   INTEGER NOT NULL DEFAULT 0 CHECK (submission_reserved IN (0, 1)),
    created_at            TEXT    NOT NULL,
    UNIQUE (strategy_id, execution_mode, trading_date, sequence_number)
);

CREATE INDEX IF NOT EXISTS idx_order_intents_strategy_mode
    ON order_intents (strategy_id, execution_mode, trading_date);

-- ----------------------------------------------------------------- orders
CREATE TABLE IF NOT EXISTS orders (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id             INTEGER NOT NULL UNIQUE REFERENCES order_intents (id),
    correlation_id        TEXT    NOT NULL UNIQUE,
    runtime_id            TEXT    NOT NULL,
    strategy_id           TEXT    NOT NULL,
    execution_mode        TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    broker_order_id       TEXT,
    status                TEXT    NOT NULL CHECK (status IN (
                              'PENDING', 'SUBMITTED', 'ACKNOWLEDGED', 'PARTIALLY_FILLED',
                              'FILLED', 'REJECTED', 'CANCELLED', 'UNKNOWN')),
    filled_quantity       INTEGER NOT NULL DEFAULT 0,
    average_fill_price    REAL,
    rejection_reason      TEXT,
    submitted_at          TEXT,
    updated_at            TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_open
    ON orders (strategy_id, execution_mode, status);

-- ------------------------------------------------------------------ fills
CREATE TABLE IF NOT EXISTS fills (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id              INTEGER NOT NULL REFERENCES orders (id),
    correlation_id        TEXT    NOT NULL,
    runtime_id            TEXT    NOT NULL,
    strategy_id           TEXT    NOT NULL,
    execution_mode        TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    -- Broker-side identity of this fill. UNIQUE with order_id is what makes
    -- fill processing idempotent: replaying the same broker event cannot
    -- double-count quantity into the position.
    broker_fill_id        TEXT    NOT NULL,
    quantity              INTEGER NOT NULL CHECK (quantity > 0),
    price                 REAL    NOT NULL,
    -- Paper-fill provenance (spec section 6, "Record"). Without these a paper
    -- P&L number cannot be audited back to the quote that produced it.
    reference_price       REAL,
    slippage_amount       REAL,
    latency_ms            INTEGER,
    fill_method           TEXT,
    charges               REAL    NOT NULL DEFAULT 0.0,
    filled_at             TEXT    NOT NULL,
    UNIQUE (order_id, broker_fill_id)
);

-- -------------------------------------------------------------- positions
CREATE TABLE IF NOT EXISTS positions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id            TEXT    NOT NULL,
    strategy_id           TEXT    NOT NULL,
    execution_mode        TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    trading_date          TEXT    NOT NULL,
    instrument            TEXT    NOT NULL,
    security_id           TEXT    NOT NULL,
    quantity              INTEGER NOT NULL,
    average_price         REAL    NOT NULL,
    entry_correlation_id  TEXT,
    -- Exit state that must survive a restart (spec section 12).
    stop_price            REAL,
    target_price          REAL,
    highest_favourable    REAL,
    lowest_favourable     REAL,
    realised_pnl          REAL    NOT NULL DEFAULT 0.0,
    charges               REAL    NOT NULL DEFAULT 0.0,
    status                TEXT    NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
    opened_at             TEXT    NOT NULL,
    closed_at             TEXT,
    updated_at            TEXT    NOT NULL,
    -- One open position per strategy/mode/instrument/day. State must never leak
    -- between days or contracts, so the trading date is part of the identity.
    UNIQUE (strategy_id, execution_mode, trading_date, security_id)
);

CREATE INDEX IF NOT EXISTS idx_positions_open
    ON positions (strategy_id, execution_mode, status);

-- --------------------------------------------------------- strategy state
CREATE TABLE IF NOT EXISTS strategy_state (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id            TEXT    NOT NULL,
    strategy_id           TEXT    NOT NULL,
    execution_mode        TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    trading_date          TEXT    NOT NULL,
    -- Version the payload so a restart against a newer build detects an
    -- incompatible shape instead of silently misreading restored state.
    state_version         INTEGER NOT NULL DEFAULT 1,
    last_candle_end_at    TEXT,
    re_entry_count        INTEGER NOT NULL DEFAULT 0,
    daily_realised_pnl    REAL    NOT NULL DEFAULT 0.0,
    entries_blocked       INTEGER NOT NULL DEFAULT 0 CHECK (entries_blocked IN (0, 1)),
    square_off_state      TEXT    NOT NULL DEFAULT 'PENDING',
    square_off_attempts   INTEGER NOT NULL DEFAULT 0,
    payload               TEXT,
    updated_at            TEXT    NOT NULL,
    UNIQUE (strategy_id, execution_mode, trading_date)
);

-- ---------------------------------------------------- notifications/errors
CREATE TABLE IF NOT EXISTS notifications (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id            TEXT    NOT NULL,
    strategy_id           TEXT,
    execution_mode        TEXT    CHECK (execution_mode IS NULL
                                         OR execution_mode IN ('paper', 'live')),
    channel               TEXT    NOT NULL,
    event_type            TEXT    NOT NULL,
    -- Message bodies are redacted before they reach here; no secrets in SQLite.
    message               TEXT    NOT NULL,
    delivered             INTEGER NOT NULL DEFAULT 0 CHECK (delivered IN (0, 1)),
    failure_reason        TEXT,
    created_at            TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS errors (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id            TEXT    NOT NULL,
    strategy_id           TEXT,
    execution_mode        TEXT    CHECK (execution_mode IS NULL
                                         OR execution_mode IN ('paper', 'live')),
    severity              TEXT    NOT NULL,
    component             TEXT    NOT NULL,
    message               TEXT    NOT NULL,
    context               TEXT,
    occurred_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_errors_recent
    ON errors (runtime_id, occurred_at DESC);
