"""Phase 5 verification item 4: mode separation, proven against real state.

Runs a real mixed-mode group (one paper strategy, one blocked live strategy)
to completion, then queries the resulting SQLite file directly — the same
pattern as ``test_walking_skeleton.py``'s duplicate-refusal gate and
``test_supervisor.py``'s database assertions.

Deliberately not simplified to "assert zero `execution_mode='live'` rows".
That version passes under the exact failure this test exists to catch: if the
blocked live strategy were ever silently rerouted to paper, its rows would
land with ``execution_mode='paper'`` and a mode-keyed count would still read
zero. Every negative assertion here is keyed on ``strategy_id`` instead.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from common.config.models import ExecutionMode
from common.market_data import RecordedFeedAdapter, load_tick_tape
from common.notifications import RecordingNotifier
from common.persistence import Database
from runtimes.intraday_options.supervisor import IntradayOptionsSupervisor, SupervisorConfig
from runtimes.intraday_options.worker import WorkerConfig

RUNTIME_ID = "intraday_options"
SECURITY_ID = "99926000"
TRADING_DATE = "2026-07-29"
PAPER_STRATEGY_ID = "skelfix"
LIVE_STRATEGY_ID = "livestrat"

#: Tables where a row naming the blocked live strategy is the *required*
#: behaviour, not a leak: recording that it was blocked. Every other table
#: with a strategy_id column must show zero rows for it.
_BLOCK_RECORD_TABLES = {"errors", "notifications"}


@pytest.fixture
def supervisor_config(runtime_dirs: dict[str, Path], database_path: Path) -> SupervisorConfig:
    return SupervisorConfig(
        runtime_id=RUNTIME_ID,
        database_path=database_path,
        lock_dir=runtime_dirs["lock_dir"],
        pid_dir=runtime_dirs["pid_dir"],
        log_dir=runtime_dirs["log_dir"],
    )


def _worker(config: SupervisorConfig, strategy_id: str, **overrides) -> WorkerConfig:
    return WorkerConfig(
        runtime_id=RUNTIME_ID,
        strategy_id=strategy_id,
        security_id=SECURITY_ID,
        instrument="NIFTY",
        database_path=config.database_path,
        lock_dir=config.lock_dir,
        pid_dir=config.pid_dir,
        log_dir=config.log_dir,
        trading_date=TRADING_DATE,
        **overrides,
    )


def _tables_with_column(conn: sqlite3.Connection, column: str) -> list[str]:
    """Every real table carrying ``column``, discovered from the schema.

    Enumerated rather than hand-listed: a table added in a later phase that
    carries ``strategy_id`` is swept automatically instead of silently
    escaping this test the way a hardcoded list would let it.
    """
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"
        ).fetchall()
    ]
    return [
        table
        for table in tables
        if column in {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    ]


def test_mode_separation_survives_a_mixed_paper_and_live_run(
    supervisor_config, tick_tape_path, database_path
):
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    notifier = RecordingNotifier()
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter, notifier)
    supervisor.add_worker(_worker(supervisor_config, PAPER_STRATEGY_ID))
    supervisor.add_worker(
        _worker(supervisor_config, LIVE_STRATEGY_ID, execution_mode=ExecutionMode.LIVE)
    )

    result = supervisor.run()
    assert result.workers_started == 1
    assert result.worker_exit_codes == {PAPER_STRATEGY_ID: 0}

    conn = Database(database_path).connect()

    # (a) Positive control. Without this, every "no live rows" assertion below
    # is satisfied just as well by a run that crashed on startup and wrote
    # nothing at all.
    for table in ("signals", "orders", "fills", "positions"):
        count = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE strategy_id = ? AND execution_mode = 'paper'",
            (PAPER_STRATEGY_ID,),
        ).fetchone()[0]
        assert count > 0, (
            f"{table} has no paper rows for {PAPER_STRATEGY_ID} — positive control failed"
        )

    # (b) + (c) Every table carrying strategy_id, swept from the schema, shows
    # zero rows for the live strategy — keyed on strategy_id, not
    # execution_mode. See the module docstring for why the mode-keyed version
    # of this assertion would be worthless.
    swept_tables = _tables_with_column(conn, "strategy_id")
    assert len(swept_tables) >= 9, "fewer tables than expected — did discovery break?"
    for table in swept_tables:
        if table in _BLOCK_RECORD_TABLES:
            continue
        count = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE strategy_id = ?", (LIVE_STRATEGY_ID,)
        ).fetchone()[0]
        assert count == 0, f"{table} has a row for the blocked live strategy {LIVE_STRATEGY_ID!r}"

    # runtime_heartbeats carries strategy_id but no execution_mode — a
    # mode-keyed sweep would miss it entirely; the strategy_id-keyed sweep
    # above catches it because it iterates every table with the column,
    # regardless of whether execution_mode is also present. Assert it
    # explicitly too, since missing it would be easy to not notice.
    assert "runtime_heartbeats" in swept_tables
    assert "execution_mode" not in {
        row[1] for row in conn.execute("PRAGMA table_info(runtime_heartbeats)").fetchall()
    }

    # paper_fill_quotes carries neither strategy_id nor execution_mode; it is
    # reached transitively via orders.id. orders already holds zero rows for
    # the live strategy (swept above), so no paper_fill_quotes row can join to
    # one — the FK on order_id makes it structurally impossible. Asserted
    # explicitly rather than only trusted.
    joined = conn.execute(
        """
        SELECT COUNT(*) FROM paper_fill_quotes q
        JOIN orders o ON o.id = q.order_id
        WHERE o.strategy_id = ?
        """,
        (LIVE_STRATEGY_ID,),
    ).fetchone()[0]
    assert joined == 0

    # (d) The block itself is recorded — a carve-out from (b)/(c), not a leak.
    # Silence would be its own defect: an operator reading only the database
    # must be able to see the strategy was deliberately blocked, not silently
    # missing.
    errors = conn.execute(
        "SELECT message, execution_mode FROM errors WHERE strategy_id = ?",
        (LIVE_STRATEGY_ID,),
    ).fetchall()
    assert len(errors) == 1
    assert errors[0]["execution_mode"] == "live"
    assert "live gate blocks it" in errors[0]["message"]
    assert "NOT rerouted to paper" in errors[0]["message"]

    # A human not looking at the database also learns about the block: the
    # errors row above is the persisted record, this is the delivered one.
    # This event is a success — record_notification (Phase 7 Part 2's real
    # production caller, SafeNotifier's on_failure hook) only fires when
    # *delivery* fails, and this RecordingNotifier always succeeds. See
    # tests/integration/test_execution_persistence.py for that path exercised
    # end-to-end, and common/notifications/base.py's module docstring for why
    # the callback exists at all.
    blocked_events = [e for e in notifier.events if e.event_type == "live_strategy_blocked"]
    assert len(blocked_events) == 1
    assert blocked_events[0].strategy_id == LIVE_STRATEGY_ID
    assert blocked_events[0].execution_mode is ExecutionMode.LIVE
