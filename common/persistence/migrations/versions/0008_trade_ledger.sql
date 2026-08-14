-- Phase 10 dashboard corrective pass. A durable, append-only per-trade
-- record — one row per *realising* fill (a fill that reduces or closes a
-- position), written by ExecutionRepository._upsert_position in the same
-- transaction as the fills/positions/orders writes apply_fill already
-- makes.
--
-- Why this exists: positions holds exactly one row per (strategy, mode,
-- trading_date, security_id) identity, reused across any number of
-- close/reopen cycles within a day. A position that closes and then
-- reopens the same day silently overwrites its own "CLOSED" status back to
-- "OPEN" — the individual round trip's own detail is gone, even though
-- positions.realised_pnl correctly keeps accumulating the day's running
-- total throughout. This table is the missing per-trade record: it is
-- never updated after insert, never reused across a reopen, and is the
-- source of truth for Closed Trades, Performance and Strategy Comparison
-- — not a re-derivation from positions/fills at read time.
--
-- entry_price/opened_at/entry_correlation_id are read off the position row
-- as it stood immediately before this fill, which _upsert_position now
-- resets on every reopen from flat (a bookkeeping-only fix alongside this
-- migration — see that method's docstring). exit_price/closed_at come off
-- the fill itself. gross_pnl is the fill's own realised_delta, already
-- computed for strategy_state's daily accumulator. entry_charges sums the
-- fills recorded under entry_correlation_id; exit_charges is this fill's
-- own charges — exact for a strategy that never scales in (the only one
-- this project runs today); a scaling strategy would need entry charges
-- apportioned across each partial close, a documented precision limit, not
-- a schema gap.

CREATE TABLE IF NOT EXISTS trade_ledger (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id            TEXT    NOT NULL,
    strategy_id           TEXT    NOT NULL,
    execution_mode        TEXT    NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    trading_date          TEXT    NOT NULL,
    instrument            TEXT    NOT NULL,
    security_id           TEXT    NOT NULL,
    -- The direction of the leg that opened, not the fill that closed it —
    -- matches the reader's existing "entry side" convention.
    entry_side            TEXT    NOT NULL CHECK (entry_side IN ('BUY', 'SELL')),
    quantity              INTEGER NOT NULL CHECK (quantity > 0),
    entry_price           REAL    NOT NULL,
    exit_price            REAL    NOT NULL,
    gross_pnl             REAL    NOT NULL,
    entry_charges         REAL    NOT NULL DEFAULT 0.0,
    exit_charges          REAL    NOT NULL DEFAULT 0.0,
    entry_correlation_id  TEXT,
    exit_correlation_id   TEXT    NOT NULL,
    exit_broker_fill_id   TEXT    NOT NULL,
    opened_at             TEXT    NOT NULL,
    closed_at             TEXT    NOT NULL,
    created_at            TEXT    NOT NULL,
    -- Belt and braces alongside fills' own (order_id, broker_fill_id)
    -- idempotency key: apply_fill already refuses to re-process a replayed
    -- broker_fill_id before this code path is ever reached.
    UNIQUE (exit_correlation_id, exit_broker_fill_id)
);

CREATE INDEX IF NOT EXISTS idx_trade_ledger_scope
    ON trade_ledger (runtime_id, strategy_id, execution_mode, trading_date);

CREATE INDEX IF NOT EXISTS idx_trade_ledger_closed_at
    ON trade_ledger (runtime_id, closed_at DESC);
