-- Operator action audit trail (Phase 7 Part 4).
--
-- Spec section 11, "Command-line operations": "Live-impacting commands must
-- require explicit confirmation and log an audit event." Nothing in this
-- repository had an audit sink before this migration — `record_error`
-- (0001) is an error log for things that go wrong on their own, not a
-- record of what an operator deliberately asked for.
--
-- Purely additive: no existing table is touched, so a database created by
-- 0001/0002/0003 upgrades in place. Every CREATE uses IF NOT EXISTS because
-- the runner rejects anything else — safety comes from replay-safety rather
-- than transactional atomicity, which sqlite3.executescript() cannot
-- provide (deviation D6).
--
-- **No column here can hold a secret.** `actor` is an operating-system
-- username, never a credential; `detail` is operator-authored or derived
-- from already-redacted values, the same discipline `notifications.message`
-- (0001) and `auth_events.detail` (0002) already follow.
CREATE TABLE IF NOT EXISTS audit_events (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id            TEXT    NOT NULL,
    strategy_id           TEXT,
    execution_mode        TEXT    CHECK (execution_mode IS NULL
                                         OR execution_mode IN ('paper', 'live')),
    -- What operator action this records — a closed vocabulary, not a
    -- free-text log line, so a query can answer "how many stops this week"
    -- without parsing prose.
    action                TEXT    NOT NULL CHECK (action IN (
                              'stop_runtime',
                              'stop_strategy',
                              'square_off_requested',
                              'square_off_completed'
                          )),
    -- The operating-system user who ran the command. Never a secret.
    actor                 TEXT    NOT NULL,
    detail                TEXT,
    occurred_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_recent
    ON audit_events (runtime_id, occurred_at DESC);
