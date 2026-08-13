-- Phase 10. The account-scoped shared database
-- (data/operational/dhan_account_shared.db, ProjectPaths
-- .account_shared_database_path). Applied by the *same* MigrationRunner
-- machinery as every runtime group's own database, pointed at this
-- separate versions_dir and this separate file — see
-- common.persistence.account_shared. Purely additive, ordinary migration
-- (no destructive-migration exception needed here; that is 0006 in the
-- per-runtime-group versions_dir).
--
-- Every table below carries account_key: a stable, non-secret identity
-- derived (HMAC, not a bare hash) from the authenticated Dhan account, not
-- an operator-typed alias — see common.broker.live_preflight
-- .derive_account_key. The raw Dhan client ID is never persisted here or
-- anywhere else in this database.

CREATE TABLE IF NOT EXISTS live_account_state_provenance (
    account_key           TEXT PRIMARY KEY,
    reconciliation_status TEXT NOT NULL CHECK (reconciliation_status IN
                             ('never_reconciled', 'reconciled', 'stale', 'failed')),
    last_reconciled_at    TEXT,
    established_at        TEXT NOT NULL
);

-- Account-wide, cross-process live-order call limiter (spec section 14).
-- Fixed-window: one row per (account_key, call_class, window). SQLite's own
-- single-writer transaction serialisation (WAL mode) is the cross-process
-- guarantee for the increment — no filelock needed around the reservation
-- itself, only around cross-runtime-group startup coordination elsewhere.
CREATE TABLE IF NOT EXISTS live_order_rate_windows (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key    TEXT    NOT NULL,
    call_class     TEXT    NOT NULL CHECK (call_class IN ('new_order', 'modify', 'cancel', 'read')),
    window_start   TEXT    NOT NULL,
    window_seconds INTEGER NOT NULL,
    count          INTEGER NOT NULL DEFAULT 0,
    UNIQUE (account_key, call_class, window_start, window_seconds)
);

-- The reserve-before-submit risk gate. A row is created in one atomic
-- transaction *before* any broker call (common.risk.account_reservations),
-- so two workers' check-and-reserve transactions cannot both pass a
-- capacity check for exposure neither has actually placed yet. UNKNOWN is
-- reachable from several states and has no transition to RELEASED except
-- via RECONCILED — enforced in Python, not by this CHECK, because the
-- legal state graph is directional, not just a closed vocabulary.
CREATE TABLE IF NOT EXISTS live_risk_reservations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key        TEXT    NOT NULL,
    runtime_id         TEXT    NOT NULL,
    strategy_id        TEXT    NOT NULL,
    correlation_id     TEXT    NOT NULL,
    trading_date       TEXT    NOT NULL,
    state              TEXT    NOT NULL CHECK (state IN (
                          'RESERVED', 'SUBMITTED', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'FILLED',
                          'REJECTED', 'CANCELLED', 'EXPIRED', 'UNKNOWN', 'RECONCILED', 'RELEASED')),
    projected_capital  REAL    NOT NULL,
    projected_legs     INTEGER NOT NULL DEFAULT 1,
    residual_quantity  INTEGER NOT NULL,
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    UNIQUE (account_key, correlation_id)
);

CREATE INDEX IF NOT EXISTS idx_live_risk_reservations_active
    ON live_risk_reservations (account_key, state);

-- Idempotent realised-P&L events, append-only. Safe to SUM(): a replayed
-- event (worker restart re-processing the same fill) is a no-op INSERT
-- conflict on idempotency_key, never a double-count.
CREATE TABLE IF NOT EXISTS live_realised_pnl_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key         TEXT    NOT NULL,
    runtime_id          TEXT    NOT NULL,
    strategy_id         TEXT    NOT NULL,
    trading_date        TEXT    NOT NULL,
    -- e.g. a closing fill's broker_fill_id.
    idempotency_key     TEXT    NOT NULL,
    realised_pnl_delta  REAL    NOT NULL,
    recorded_at         TEXT    NOT NULL,
    UNIQUE (account_key, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_live_realised_pnl_events_day
    ON live_realised_pnl_events (account_key, trading_date);

-- Current open-position state (upserted, not appended) — mirrors the local
-- per-runtime-group `positions` table's own current-state convention
-- (0001_walking_skeleton.sql), for the same reason: "how much are we
-- exposed by right now" must never be an accumulation of history.
CREATE TABLE IF NOT EXISTS live_open_positions (
    account_key       TEXT    NOT NULL,
    runtime_id        TEXT    NOT NULL,
    strategy_id       TEXT    NOT NULL,
    security_id       TEXT    NOT NULL,
    quantity          INTEGER NOT NULL,
    average_price     REAL    NOT NULL,
    deployed_capital  REAL    NOT NULL,
    opened_at         TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    PRIMARY KEY (account_key, runtime_id, strategy_id, security_id)
);

-- Latest mark-to-market only, overwritten on every mark (never appended) —
-- the fix for the double-counting hazard of an additive MTM ledger. Summing
-- this table always reflects the current picture, never an accumulation of
-- repeated snapshots.
CREATE TABLE IF NOT EXISTS live_position_mtm (
    account_key     TEXT NOT NULL,
    runtime_id      TEXT NOT NULL,
    strategy_id     TEXT NOT NULL,
    security_id     TEXT NOT NULL,
    unrealised_pnl  REAL NOT NULL,
    as_of           TEXT NOT NULL,
    PRIMARY KEY (account_key, runtime_id, strategy_id, security_id)
);

-- Explicit live-confirmation token / local approval file (spec section 10,
-- "Explicit live confirmation token or local approval file"), bound to all
-- five of account, runtime, strategy, config fingerprint and expiry — any
-- one of them changing invalidates the confirmation.
CREATE TABLE IF NOT EXISTS live_confirmations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key         TEXT NOT NULL,
    runtime_id          TEXT NOT NULL,
    strategy_id         TEXT NOT NULL,
    config_fingerprint  TEXT NOT NULL,
    confirmed_by        TEXT NOT NULL,
    confirmed_at        TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    revoked_at          TEXT,
    UNIQUE (account_key, runtime_id, strategy_id, config_fingerprint)
);

-- Timestamped, per-(account, runtime, strategy, config-fingerprint) live
-- preflight results (spec's own live-safety checklist). A worker re-runs
-- preflight and writes a fresh row here inside its own process — never
-- trusts a value merely passed in from a parent/supervisor process.
CREATE TABLE IF NOT EXISTS live_preflight_results (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key               TEXT NOT NULL,
    runtime_id                TEXT NOT NULL,
    strategy_id               TEXT NOT NULL,
    config_fingerprint        TEXT NOT NULL,
    checked_at                TEXT NOT NULL,
    static_ip_result          TEXT NOT NULL CHECK (static_ip_result IN ('passed', 'blocked')),
    account_identity_result   TEXT NOT NULL CHECK (account_identity_result IN ('passed', 'blocked')),
    shared_db_health_result   TEXT NOT NULL CHECK (shared_db_health_result IN ('passed', 'blocked')),
    token_result              TEXT NOT NULL CHECK (token_result IN ('passed', 'blocked')),
    connectivity_result       TEXT NOT NULL CHECK (connectivity_result IN ('passed', 'blocked')),
    confirmation_result       TEXT NOT NULL CHECK (confirmation_result IN ('passed', 'blocked')),
    overall_result            TEXT NOT NULL CHECK (overall_result IN ('passed', 'blocked')),
    detail                    TEXT
);

CREATE INDEX IF NOT EXISTS idx_live_preflight_results_recent
    ON live_preflight_results (account_key, runtime_id, strategy_id, checked_at);
