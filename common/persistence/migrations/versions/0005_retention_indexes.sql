-- Timestamp-leading indexes for common.retention.database.purge_old_rows
-- (Phase 7 Part 5 addendum).
--
-- Every existing index that touches these five tables' timestamp columns
-- (idx_runtime_heartbeats_recent, idx_errors_recent, idx_feed_events_recent,
-- idx_auth_events_recent) leads with `runtime_id` — correct and deliberate
-- for the reads that actually use them: common/health/snapshot.py filters
-- `WHERE runtime_id = ? [AND strategy_id = ?] ...` throughout, an equality
-- lookup only the leading column can serve. `notifications` has no index at
-- all. None of the five gives `purge_old_rows`'s
-- `WHERE <col> < ? ORDER BY <col> LIMIT ?` — a range scan with no runtime_id
-- predicate — anything to seek on, so it fell back to a full table scan
-- plus a temp-B-tree sort every run, confirmed via EXPLAIN QUERY PLAN
-- (SCAN ... USE TEMP B-TREE FOR ORDER BY, on all five).
--
-- This migration adds one new, single-column index per table, leading with
-- (and containing only) the timestamp column purge_old_rows filters and
-- sorts on. It does not touch, reorder or drop any existing index — the
-- runner rejects DROP INDEX outright (_reject_destructive), and reordering
-- runtime_id out of the existing composite indexes would have degraded the
-- snapshot reads above instead. The new indexes exist for exactly one
-- consumer: a purge run can now seek to the oldest rows and take LIMIT of
-- them directly, in a plan that reads only the rows it deletes rather than
-- the whole table.
--
-- Purely additive: no existing table or index is touched, so a database
-- created by 0001-0004 upgrades in place.
CREATE INDEX IF NOT EXISTS idx_runtime_heartbeats_beat_at ON runtime_heartbeats (beat_at);
CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications (created_at);
CREATE INDEX IF NOT EXISTS idx_errors_occurred_at ON errors (occurred_at);
CREATE INDEX IF NOT EXISTS idx_feed_events_occurred_at ON feed_events (occurred_at);
CREATE INDEX IF NOT EXISTS idx_auth_events_occurred_at ON auth_events (occurred_at);
