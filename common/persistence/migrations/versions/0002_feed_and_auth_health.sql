-- Feed, authentication and option-chain observability (Phase 2).
--
-- Purely additive: no existing table is touched, so a database created by 0001
-- upgrades in place. Every CREATE uses IF NOT EXISTS because the runner rejects
-- anything else — safety comes from replay-safety rather than transactional
-- atomicity, which sqlite3.executescript() cannot provide (deviation D6).
--
-- **No table here stores a secret.** Not an access token, not a PIN, not a TOTP,
-- not a client id. auth_events records that authentication happened, its outcome
-- and its cost in requests — which is exactly what an audit needs and no more.
-- The token itself lives only in data/cache/token_cache.json (mode 0600,
-- gitignored) and in memory.
--
-- These are diagnostic tables. They are written on state *changes* and on
-- failures, never per tick or per candle: the spec is explicit that high-rate
-- events belong in counters and heartbeats, not in rows (section "Do not send
-- every tick, candle or heartbeat").

-- ------------------------------------------------------------ authentication
CREATE TABLE IF NOT EXISTS auth_events (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id            TEXT    NOT NULL,
    -- What happened, not who it happened to.
    event                 TEXT    NOT NULL CHECK (event IN (
                              'token_reused_from_env',
                              'token_reused_from_cache',
                              'token_generated',
                              'token_refreshed',
                              'validation_succeeded',
                              'validation_failed',
                              'credentials_rejected',
                              'rate_limited',
                              'cooldown_suppressed',
                              'transient_failure'
                          )),
    -- 'environment' | 'cache' | 'generated' — matches TokenOutcome.source.
    token_source          TEXT,
    -- Dhan's stated expiry, which is not a secret. NULL when unknown.
    token_expiry          TEXT,
    -- HTTP requests this event cost. The column exists so "a wrong PIN costs
    -- exactly one request" is auditable after the fact rather than only tested.
    requests_made         INTEGER NOT NULL DEFAULT 0,
    -- Redacted human-readable reason. Written through the logging redactor's
    -- rules; never a raw broker response.
    detail                TEXT,
    occurred_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_events_recent
    ON auth_events (runtime_id, occurred_at DESC);

-- --------------------------------------------------------------------- feed
CREATE TABLE IF NOT EXISTS feed_events (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id            TEXT    NOT NULL,
    event                 TEXT    NOT NULL CHECK (event IN (
                              'connected',
                              'disconnected',
                              'reconnect_attempted',
                              'reconnect_exhausted',
                              'resubscribed',
                              'degraded',
                              'recovered',
                              'stale_instrument'
                          )),
    -- Dhan's server-disconnection code (805-809) when it supplied one. 807 is
    -- access-token expiry, which is handled by refreshing rather than retrying,
    -- so recording the code is what makes that decision reviewable.
    reason_code           INTEGER,
    reason                TEXT,
    -- Consecutive attempt number for a reconnect, so a bounded-backoff sequence
    -- can be reconstructed from the table.
    attempt               INTEGER,
    downtime_seconds      REAL,
    expected_subscriptions INTEGER,
    active_subscriptions  INTEGER,
    -- Bars discarded because they were open across this gap. Non-zero here is
    -- the audit trail for a candle that deliberately does not exist.
    gap_candles_discarded INTEGER NOT NULL DEFAULT 0,
    security_id           TEXT,
    occurred_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feed_events_recent
    ON feed_events (runtime_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_feed_events_reason
    ON feed_events (runtime_id, event, reason_code);

-- ------------------------------------------------------------- option chain
-- Snapshot *metadata* only. The spec is explicit (line 2111): keep what audit
-- and freshness need, and avoid storing every full chain indefinitely — a chain
-- is a few hundred strikes, and at one snapshot every three seconds that is
-- gigabytes per session for data the WebSocket feed already supplies.
CREATE TABLE IF NOT EXISTS option_chain_snapshots (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id            TEXT    NOT NULL,
    underlying_security_id INTEGER NOT NULL,
    underlying_segment    TEXT    NOT NULL,
    expiry                TEXT    NOT NULL,
    -- Broker-stated snapshot time when present, else receive time. Stored
    -- separately from received_at so a future broker timestamp does not
    -- retroactively change what freshness meant.
    snapshot_at           TEXT    NOT NULL,
    received_at           TEXT    NOT NULL,
    -- Age at the moment a consumer used it, which is what actually gates a
    -- delta-dependent decision.
    age_seconds           REAL,
    strike_count          INTEGER,
    -- 1 when this row records a snapshot that was served despite being stale,
    -- i.e. a fetch failure fell back to cache. Those are the rows to look at
    -- when a delta decision is later questioned.
    served_stale          INTEGER NOT NULL DEFAULT 0 CHECK (served_stale IN (0, 1)),
    -- 'api' when this snapshot came from a real call, 'cache' when deduplicated.
    source                TEXT    NOT NULL CHECK (source IN ('api', 'cache')),
    occurred_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_option_chain_snapshots_key
    ON option_chain_snapshots (underlying_security_id, expiry, occurred_at DESC);
