-- Phase 10, reviewed-destructive (see common/persistence/migrations.py's
-- _MANUALLY_REVIEWED_DESTRUCTIVE and _check_destructive_preconditions).
--
-- Widens orders.status's CHECK constraint to add EXPIRED — Dhan's own
-- documented terminal orderStatus value (verified directly against
-- https://dhanhq.co/docs/v2/orders/: TRANSIT, PENDING, REJECTED, CANCELLED,
-- PART_TRADED, TRADED, EXPIRED), distinct from REJECTED and CANCELLED, for
-- a DAY-validity order that never fills before market close.
--
-- SQLite cannot ALTER a CHECK constraint, so this follows SQLite's own
-- documented "other kinds of table schema changes" procedure
-- (https://www.sqlite.org/lang_altertable.html section 7): create the new
-- shape, copy every row across by explicit column list (preserving `id`
-- exactly, so fills.order_id's foreign key stays valid), drop the old
-- table, rename the new one into place, recreate its index.
--
-- Crash safety for this one migration comes from a fresh backup_database()
-- snapshot (required by MigrationRunner whenever orders already has rows)
-- plus PRAGMA foreign_key_check after the batch, not from pure statement
-- replay — see migrations.py's _apply_one docstring for the narrow window
-- this accepts and why. INSERT OR IGNORE and every CREATE using
-- IF NOT EXISTS keep every step *except* the DROP itself safely re-runnable.

PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS orders_new (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id             INTEGER NOT NULL UNIQUE REFERENCES order_intents (id),
    correlation_id        TEXT    NOT NULL UNIQUE,
    runtime_id            TEXT    NOT NULL,
    strategy_id           TEXT    NOT NULL,
    execution_mode        TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    broker_order_id       TEXT,
    status                TEXT    NOT NULL CHECK (status IN (
                              'PENDING', 'SUBMITTED', 'ACKNOWLEDGED', 'PARTIALLY_FILLED',
                              'FILLED', 'REJECTED', 'CANCELLED', 'EXPIRED', 'UNKNOWN')),
    filled_quantity       INTEGER NOT NULL DEFAULT 0,
    average_fill_price    REAL,
    rejection_reason      TEXT,
    submitted_at          TEXT,
    updated_at            TEXT    NOT NULL
);

INSERT OR IGNORE INTO orders_new (
    id, intent_id, correlation_id, runtime_id, strategy_id, execution_mode,
    broker_order_id, status, filled_quantity, average_fill_price,
    rejection_reason, submitted_at, updated_at
)
SELECT
    id, intent_id, correlation_id, runtime_id, strategy_id, execution_mode,
    broker_order_id, status, filled_quantity, average_fill_price,
    rejection_reason, submitted_at, updated_at
FROM orders;

DROP TABLE orders;

ALTER TABLE orders_new RENAME TO orders;

CREATE INDEX IF NOT EXISTS idx_orders_open
    ON orders (strategy_id, execution_mode, status);

PRAGMA foreign_keys = ON;
