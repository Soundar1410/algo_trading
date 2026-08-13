-- Phase 10. Deferred controlled-live tables (spec section 5,
-- "Deferred controlled-live tables": reconciliation_runs,
-- reconciliation_mismatches). Purely additive — no destructive statements,
-- unlike 0006. Scoped to one runtime group's own database: reconciliation
-- compares this group's local orders/positions against the broker, so it
-- belongs where that local state already lives.

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id                TEXT    NOT NULL,
    -- NULL means a whole-runtime-group run rather than one strategy.
    strategy_id               TEXT,
    execution_mode            TEXT    NOT NULL CHECK (execution_mode = 'live'),
    trigger_source            TEXT    NOT NULL CHECK (trigger_source IN
                                 ('startup', 'scheduled', 'manual', 'mode_transition')),
    started_at                TEXT    NOT NULL,
    completed_at              TEXT,
    status                    TEXT    NOT NULL CHECK (status IN
                                 ('running', 'completed', 'failed')),
    critical_mismatch_count   INTEGER NOT NULL DEFAULT 0,
    entries_blocked           INTEGER NOT NULL DEFAULT 0 CHECK (entries_blocked IN (0, 1)),
    broker_orders_fetched     INTEGER NOT NULL DEFAULT 0,
    broker_trades_fetched     INTEGER NOT NULL DEFAULT 0,
    broker_positions_fetched  INTEGER NOT NULL DEFAULT 0,
    error_message             TEXT
);

CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_recent
    ON reconciliation_runs (runtime_id, strategy_id, started_at);

CREATE TABLE IF NOT EXISTS reconciliation_mismatches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL REFERENCES reconciliation_runs (id),
    runtime_id          TEXT    NOT NULL,
    strategy_id         TEXT    NOT NULL,
    execution_mode      TEXT    NOT NULL CHECK (execution_mode = 'live'),
    -- Full taxonomy, spec section 9 ("Future controlled-live mismatch
    -- classifications").
    category            TEXT    NOT NULL CHECK (category IN (
                            'MATCHED', 'LOCAL_ONLY', 'BROKER_ONLY', 'QUANTITY_MISMATCH',
                            'SIDE_MISMATCH', 'PRODUCT_MISMATCH', 'PRICE_MISMATCH',
                            'LOCAL_OPEN_BROKER_CLOSED', 'LOCAL_CLOSED_BROKER_OPEN',
                            'UNKNOWN_ORDER', 'DUPLICATE_CORRELATION')),
    is_critical         INTEGER NOT NULL CHECK (is_critical IN (0, 1)),
    correlation_id      TEXT,
    broker_order_id     TEXT,
    security_id         TEXT,
    local_quantity      INTEGER,
    broker_quantity     INTEGER,
    local_side          TEXT,
    broker_side         TEXT,
    local_price         REAL,
    broker_price        REAL,
    detail              TEXT,
    -- Closed vocabulary of the narrow permitted automated actions (spec
    -- section 10). Enforced in Python (common.reconciliation.policies), not
    -- by this CHECK alone — NULL until resolved.
    resolution_action   TEXT CHECK (resolution_action IS NULL OR resolution_action IN (
                            'adopt_broker_order', 'update_traded_quantity',
                            'mark_rejected', 'mark_closed')),
    resolution_reason   TEXT,
    resolved_at         TEXT,
    detected_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reconciliation_mismatches_open
    ON reconciliation_mismatches (runtime_id, strategy_id, resolved_at);

CREATE INDEX IF NOT EXISTS idx_reconciliation_mismatches_run
    ON reconciliation_mismatches (run_id);
