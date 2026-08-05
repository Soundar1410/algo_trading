-- Paper fill provenance: the submission-time quote (Phase 4 Part 5).
--
-- Spec section 6 lists what a paper fill must record: the signal reference
-- price, the submission-time quote, the simulated fill price, the slippage
-- amount, the latency, and the fallback method used when bid/ask was
-- unavailable. Migration 0001 gave `fills` every one of those except the quote
-- itself, so a fill could be audited back to a *price* but not to the book that
-- produced it — which is exactly the question Part 5's model invites, since its
-- whole claim is that it prices against a real bid and ask.
--
-- **Why a side table and not new `fills` columns.** The migration runner requires
-- every statement to be a no-op on replay (see versions/README.md rule 2),
-- because sqlite3.executescript() commits implicitly and a migration therefore
-- cannot be applied and recorded in one transaction (deviation D6). SQLite has no
-- `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so widening `fills` would fail on
-- the second run — and the second run is the whole safety mechanism.
--
-- One row per fill, written in the same transaction as the fill itself, and only
-- when the broker actually supplied quote detail: a live adapter that cannot
-- supply one leaves no misleading all-NULL row behind.

CREATE TABLE IF NOT EXISTS paper_fill_quotes (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id              INTEGER NOT NULL REFERENCES orders (id),
    -- Matches fills.broker_fill_id, and the UNIQUE below mirrors that table's own
    -- idempotency key: replaying a fill event must not add a second quote row.
    broker_fill_id        TEXT    NOT NULL,
    -- NULL on either side means the book had no resting order there, which is
    -- why the fill fell back to last price. Distinct from "we did not look".
    quote_bid             REAL,
    quote_ask             REAL,
    -- Age of that quote when the fill was priced. NULL when the quote's exchange
    -- timestamp was ahead of our clock, which is ordinary on a replayed tape.
    quote_age_ms          REAL,
    -- Whether the simulated submission latency actually selected a later quote,
    -- or the fill used the submission quote because no later one existed yet.
    -- Deviation D48: on a live feed this is 0 for market orders, and recording it
    -- per fill is what keeps that an observation rather than a claim.
    latency_applied       INTEGER CHECK (latency_applied IN (0, 1)),
    -- Duplicated from fills.fill_method so this table reads on its own: 'bid_ask',
    -- 'ltp_fallback' or 'limit'.
    fill_method           TEXT,
    UNIQUE (order_id, broker_fill_id)
);

CREATE INDEX IF NOT EXISTS idx_paper_fill_quotes_order
    ON paper_fill_quotes (order_id);
