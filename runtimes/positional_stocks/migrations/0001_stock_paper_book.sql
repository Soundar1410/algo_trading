-- The paper book of wsr1_weekly_stochrsi (spec section 9), in positional_stocks.db
-- only. This file lives in the runtime's OWN migration directory (spec 9, v1.2i):
-- the shared common/persistence/migrations/versions/ must never carry it.
--
-- Conventions:
--   * Every table is prefixed `stock_` and carries `strategy_id`.
--   * Money and prices are TEXT holding exact decimals (the rules compute in
--     Decimal, rounded to the paisa); a REAL would reintroduce the 891.9999...
--     that Decimal exists to avoid.
--   * Weeks are TEXT 'YYYY-Www' (ISO year and week); dates are TEXT 'YYYY-MM-DD'.
--   * Cash is never stored as a running balance: it is capital plus the sum of
--     stock_fills.cash_delta, so it cannot drift from the fills that made it.
--     stock_equity.cash is a weekly snapshot for the report.

-- One row per decided week. STARTED is committed before the week's single
-- transaction; COMPLETED is written inside it. A STARTED row with no
-- COMPLETED is an interrupted run that the next run redoes.
CREATE TABLE IF NOT EXISTS stock_weekly_runs (
    strategy_id   TEXT NOT NULL,
    week_ending   TEXT NOT NULL,
    iso_week      TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('STARTED', 'COMPLETED')),
    fingerprint   TEXT NOT NULL DEFAULT '',
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    PRIMARY KEY (strategy_id, week_ending)
);

-- One row per trade, from its T1 fill to its last share. Tranches and sales
-- are the rows of stock_fills; this row carries what the fills cannot: the
-- sizing, the fixed levels and the rules' bookkeeping.
CREATE TABLE IF NOT EXISTS stock_positions (
    position_id     TEXT PRIMARY KEY,
    strategy_id     TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    sector          TEXT NOT NULL,
    grp             TEXT NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('OPEN', 'HALF_SOLD', 'CLOSED')),
    spacing         TEXT NOT NULL,
    allocation      TEXT NOT NULL,
    t1_amount       TEXT NOT NULL,
    t2_amount       TEXT NOT NULL,
    t3_amount       TEXT NOT NULL,
    event_risk      INTEGER NOT NULL CHECK (event_risk IN (0, 1)),
    p1              TEXT NOT NULL,
    l1              TEXT NOT NULL,
    l2              TEXT NOT NULL,
    stop            TEXT NOT NULL,
    touch_week      TEXT,
    t3_disabled     INTEGER NOT NULL DEFAULT 0 CHECK (t3_disabled IN (0, 1)),
    half_sold_week  TEXT,
    exit_week       TEXT,
    net_pnl         TEXT,
    updated_week    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_positions_state
    ON stock_positions (strategy_id, state);
CREATE INDEX IF NOT EXISTS idx_stock_positions_symbol
    ON stock_positions (strategy_id, symbol);

-- Every order the rules decided. A BUY_T1 carries its sizing, sector and group
-- so an order that stays unfilled keeps holding its capacity across runs
-- (spec 4.12 v1.2g). PENDING -> FILLED or SKIPPED, once, in one transaction.
CREATE TABLE IF NOT EXISTS stock_pending_orders (
    order_id             TEXT PRIMARY KEY,
    strategy_id          TEXT NOT NULL,
    symbol               TEXT NOT NULL,
    action               TEXT NOT NULL
        CHECK (action IN ('BUY_T1', 'BUY_T2', 'BUY_T3', 'SELL_HALF', 'SELL_ALL')),
    decided_week         TEXT NOT NULL,
    execute_on_or_after  TEXT NOT NULL,
    reason               TEXT NOT NULL,
    position_id          TEXT,
    amount               TEXT,
    quantity             INTEGER,
    spacing              TEXT,
    allocation           TEXT,
    t1_amount            TEXT,
    t2_amount            TEXT,
    t3_amount            TEXT,
    event_risk           INTEGER CHECK (event_risk IN (0, 1)),
    sector               TEXT,
    grp                  TEXT,
    state                TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (state IN ('PENDING', 'FILLED', 'SKIPPED')),
    resolved_week        TEXT,
    resolution           TEXT
);
CREATE INDEX IF NOT EXISTS idx_stock_pending_orders_state
    ON stock_pending_orders (strategy_id, state);

-- One row per filled order. UNIQUE(order_id) is the exactly-once guarantee.
CREATE TABLE IF NOT EXISTS stock_fills (
    fill_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id         TEXT NOT NULL,
    order_id            TEXT NOT NULL UNIQUE REFERENCES stock_pending_orders (order_id),
    position_id         TEXT NOT NULL REFERENCES stock_positions (position_id),
    symbol              TEXT NOT NULL,
    action              TEXT NOT NULL
        CHECK (action IN ('BUY_T1', 'BUY_T2', 'BUY_T3', 'SELL_HALF', 'SELL_ALL')),
    session             TEXT NOT NULL,
    price               TEXT NOT NULL,
    shares              INTEGER NOT NULL CHECK (shares > 0),
    fees                TEXT NOT NULL,
    -- Buys only (spec 4.9 v1.2g): whether the fill was at its ISO week's
    -- first trading session. NULL for sells.
    at_week_open        INTEGER CHECK (at_week_open IN (0, 1)),
    cash_delta          TEXT NOT NULL,
    -- Spec 8: the symbol did not trade on the execution session and filled
    -- at a later session's open.
    not_traded_on_execution_session INTEGER NOT NULL DEFAULT 0
        CHECK (not_traded_on_execution_session IN (0, 1)),
    -- Spec 8 v1.2j: the fill session is in an earlier week than the run that
    -- recorded it (its candle was missing at the earlier run).
    late_fill           INTEGER NOT NULL DEFAULT 0 CHECK (late_fill IN (0, 1)),
    recorded_week       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_fills_position
    ON stock_fills (strategy_id, position_id);

-- The weekly mark-to-market and brake state (spec 4.12, 4.13 step 2).
CREATE TABLE IF NOT EXISTS stock_equity (
    strategy_id       TEXT NOT NULL,
    week_ending       TEXT NOT NULL,
    iso_week          TEXT NOT NULL,
    cash              TEXT NOT NULL,
    positions_value   TEXT NOT NULL,
    equity            TEXT NOT NULL,
    peak              TEXT NOT NULL,
    -- Spec 9: (peak - equity) / peak x 100, to 0.01.
    drawdown_pct      TEXT NOT NULL,
    regime            TEXT NOT NULL CHECK (regime IN ('normal', 'red', 'unknown')),
    brake1_until      TEXT,
    brake1_can_fire   INTEGER NOT NULL CHECK (brake1_can_fire IN (0, 1)),
    brake2_fired_on   TEXT,
    entries_blocked   TEXT,
    warnings          TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (strategy_id, week_ending)
);

-- Spec 4.11: a losing exit starts a 26-week cooling-off. The rules derive it
-- from the closed position; this row is the operator-facing record.
CREATE TABLE IF NOT EXISTS stock_cooling_off (
    strategy_id  TEXT NOT NULL,
    position_id  TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    exit_week    TEXT NOT NULL,
    until_week   TEXT NOT NULL,
    net_pnl      TEXT NOT NULL,
    PRIMARY KEY (strategy_id, position_id)
);

-- Spec 6.3 v1.2f: a held symbol removed from universe.csv follows the
-- on_exit of its LAST-SEEN row, which only persistence can remember.
CREATE TABLE IF NOT EXISTS stock_universe_seen (
    strategy_id     TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    isin            TEXT NOT NULL,
    company         TEXT NOT NULL,
    industry        TEXT NOT NULL,
    nifty100        INTEGER NOT NULL CHECK (nifty100 IN (0, 1)),
    grp             TEXT,
    on_exit         TEXT NOT NULL CHECK (on_exit IN ('hold', 'exit')),
    as_of           TEXT,
    last_seen_week  TEXT NOT NULL,
    PRIMARY KEY (strategy_id, symbol)
);

-- Spec 9's funnel record, one row per symbol per decided week. `triggered` is
-- the spec 4.5 TRIGGER at that week's close, computed for every symbol (held
-- ones included): the persisted trigger history. The repeat-signal rule
-- recomputes it from the series; this is the record, not an input.
CREATE TABLE IF NOT EXISTS stock_signals (
    strategy_id  TEXT NOT NULL,
    week_ending  TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    stage        TEXT NOT NULL,
    reason       TEXT NOT NULL,
    rs           REAL,
    flags        TEXT NOT NULL DEFAULT '[]',
    triggered    INTEGER NOT NULL CHECK (triggered IN (0, 1)),
    PRIMARY KEY (strategy_id, week_ending, symbol)
);

-- What step 3 decided for each held position, and why (spec 4.10).
CREATE TABLE IF NOT EXISTS stock_position_reviews (
    strategy_id  TEXT NOT NULL,
    week_ending  TEXT NOT NULL,
    position_id  TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    reason       TEXT NOT NULL,
    order_id     TEXT,
    flags        TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (strategy_id, week_ending, position_id)
);
