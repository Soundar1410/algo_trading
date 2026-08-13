-- Phase 10 hardening: an account-wide, restart-safe entry latch and emergency
-- exit request.  This is separate from strategy_state because account risk is
-- shared across runtime processes and must never be cleared by restarting one
-- worker.  It is scoped to trading_date so a previous day's realised loss does
-- not silently disable the next session.

CREATE TABLE IF NOT EXISTS live_account_controls (
    account_key               TEXT    NOT NULL,
    trading_date              TEXT    NOT NULL,
    entries_blocked           INTEGER NOT NULL DEFAULT 0
                                      CHECK (entries_blocked IN (0, 1)),
    emergency_exit_requested  INTEGER NOT NULL DEFAULT 0
                                      CHECK (emergency_exit_requested IN (0, 1)),
    reason                    TEXT,
    updated_at                TEXT    NOT NULL,
    PRIMARY KEY (account_key, trading_date)
);

CREATE INDEX IF NOT EXISTS idx_live_account_controls_active
    ON live_account_controls (account_key, trading_date, entries_blocked,
                              emergency_exit_requested);
