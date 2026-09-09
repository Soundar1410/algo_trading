-- Live mark-to-market for the open legs of a positional cycle (dashboard
-- "Positional Options" page). The multi-leg counterpart of migration 0014's
-- single-leg `position_marks`, scoped to `strategy_cycle_legs`.
--
-- The number this table carries is not new -- it is the number the exit
-- ladder already runs on. `LegInstance.unrealised_pnl`
-- (common/engine/multi_leg_models.py) is maintained on every tick by
-- `leg.update_price()`, summed by `Cycle.unrealised_gross_pnl()`, and turned
-- into the `PnlSnapshot` that decides the 55% profit target and the
-- 1.25/1.50/1.75 credit-multiple stops
-- (strategies/positional_options/weekly_delta_neutral/risk.py). It simply
-- never left the worker's memory: `strategy_cycle_legs` has `entry_price`,
-- `exit_price` and `realized_gross_pnl` but no mark columns, and
-- `persist_cycle_leg` runs only on leg state transitions, never periodically.
-- So a read-only dashboard could show what a leg cost and what it eventually
-- made, but never what it was worth right now -- which for a multi-day
-- position held to expiry is most of what an operator wants to know.
--
-- One new additive table, no `ALTER TABLE`: SQLite has no `ADD COLUMN IF NOT
-- EXISTS`, so an `ALTER TABLE` migration is not safely replayable after a
-- partial apply the way `MigrationRunner` requires -- the same reasoning
-- 0011/0012/0013/0014 already give for choosing a side table over widening
-- an existing one.
--
-- "Latest mark only, overwritten on every write, never appended" -- the same
-- convention 0014 states for `position_marks` and the account-shared
-- `live_position_mtm`, for the identical double-counting reason: summing
-- this table must always yield the current picture of a cycle, never an
-- accumulation of every mark ever taken.
--
-- Written once per **evaluation** (`evaluation_interval_seconds`, 5s today),
-- not once per tick. Ticks arrive far faster than that and would make this a
-- write-amplification problem for no added fidelity; the evaluation is also
-- exactly the moment the engine has just re-priced every leg and asked the
-- strategy whether to exit, so the persisted mark matches the one the
-- decision was made on rather than trailing it.
--
-- `as_of` lets a reader decide for itself how much to trust a mark. Unlike
-- the intraday `position_marks`, an old mark here is **ordinary**: a
-- positional cycle is held for days, so overnight and weekend marks are
-- legitimately hours old and that is not a fault. The dashboard shows the
-- age rather than judging it.
--
-- No FOREIGN KEY to `strategy_cycle_legs`, deliberately. On 9 September 2026
-- a purge of retired strategy ids silently orphaned 46 `paper_fill_quotes`
-- rows precisely because that table hangs off a parent id while carrying no
-- `strategy_id` of its own, so a strategy-scoped delete could not see it.
-- Every column needed to scope this table to a strategy is present here, so
-- `scripts/purge_retired_strategies.py` removes it through its ordinary
-- strategy-scoped path with no special case and no cascade to remember.
CREATE TABLE IF NOT EXISTS cycle_leg_marks (
    runtime_id        TEXT NOT NULL,
    strategy_id       TEXT NOT NULL,
    execution_mode    TEXT NOT NULL CHECK (execution_mode IN ('paper', 'live')),
    cycle_id          TEXT NOT NULL,
    leg_id            TEXT NOT NULL,
    -- The last price the engine saw for this leg's contract.
    last_price        REAL NOT NULL,
    -- Gross open P&L at that price, sign-corrected for side: a SELL leg
    -- profits as the option cheapens. Stored rather than recomputed at read
    -- time so the dashboard never re-derives a risk number with its own
    -- copy of the formula.
    unrealised_pnl    REAL NOT NULL,
    -- Running excursion extremes, the multi-leg counterpart of
    -- `positions.highest_favourable`/`lowest_favourable`.
    max_favorable_pnl REAL NOT NULL,
    max_adverse_pnl   REAL NOT NULL,
    as_of             TEXT NOT NULL,
    PRIMARY KEY (runtime_id, strategy_id, execution_mode, cycle_id, leg_id)
);

CREATE INDEX IF NOT EXISTS idx_cycle_leg_marks_cycle
    ON cycle_leg_marks (runtime_id, strategy_id, execution_mode, cycle_id);
