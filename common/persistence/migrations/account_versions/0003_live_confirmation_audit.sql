-- Append-only operator evidence for issuing and revoking a live confirmation.
-- This does not enable live trading: all independent YAML gates must still be
-- explicitly approved, and preflight verifies the exact current fingerprint.

CREATE TABLE IF NOT EXISTS live_confirmation_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_id     INTEGER,
    action              TEXT NOT NULL CHECK (action IN ('ISSUED', 'REVOKED')),
    account_key         TEXT NOT NULL,
    runtime_id          TEXT NOT NULL,
    strategy_id         TEXT NOT NULL,
    config_fingerprint  TEXT NOT NULL,
    actor               TEXT NOT NULL,
    reason              TEXT NOT NULL,
    occurred_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_live_confirmation_events_recent
    ON live_confirmation_events (account_key, runtime_id, strategy_id, occurred_at);
