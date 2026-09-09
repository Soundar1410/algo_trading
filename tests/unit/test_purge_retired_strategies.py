"""Tests for the only destructive script in this repository.

Every gate gets its own test, because the cost of a gate that silently stops
working is the permanent loss of a live strategy's trading record. The happy
path is tested last and asserts both halves: the retired id's rows are gone
from every table, and the surviving strategy's rows are untouched.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from common.config.models import ExecutionMode
from common.execution import ExecutionRepository
from common.persistence import Database, MigrationRunner
from scripts import purge_retired_strategies as purge

RUNTIME_ID = "intraday_options"
RETIRED = "supertrend_buy_1_1p2"
ACTIVE = "st12_supertrend_buy"


@pytest.fixture
def repository(database_path: Path) -> ExecutionRepository:
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    return ExecutionRepository(database)


@pytest.fixture
def config_root(tmp_path: Path) -> Path:
    """A config tree declaring only ``ACTIVE`` — so ``RETIRED`` is retired."""
    strategies = tmp_path / "config" / "strategies"
    strategies.mkdir(parents=True)
    (strategies / f"{ACTIVE}.yaml").write_text(
        f"strategy_id: {ACTIVE}\nruntime_id: {RUNTIME_ID}\n", encoding="utf-8"
    )
    return tmp_path / "config"


def _dead_pid() -> int:
    """A PID that certainly is not running: fork a child and reap it."""
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child exits immediately
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


def _session(
    repository: ExecutionRepository,
    strategy_id: str | None,
    *,
    ended: bool = True,
    pid: int | None = None,
    process_role: str = "worker",
):
    record = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id=strategy_id,
        execution_mode=ExecutionMode.PAPER,
        process_role=process_role,
        pid=pid if pid is not None else _dead_pid(),
    )
    if ended:
        with repository.database.transaction() as conn:
            conn.execute(
                "UPDATE runtime_sessions SET ended_at = ? WHERE id = ?",
                (datetime.now().isoformat(), record.id),
            )
    return record


def _trade(repository: ExecutionRepository, strategy_id: str, *, net: float = 100.0) -> None:
    """One ``trade_ledger`` row, written directly. The lifecycle write path is
    exercised in ``test_dashboard_performance_breakdown.py``; here only the
    presence and later absence of rows matters."""
    with repository.database.transaction() as conn:
        conn.execute(
            "INSERT INTO trade_ledger (runtime_id, strategy_id, execution_mode, instrument, "
            "security_id, trading_date, entry_side, quantity, entry_price, exit_price, "
            "gross_pnl, entry_charges, exit_charges, exit_correlation_id, "
            "exit_broker_fill_id, opened_at, closed_at, created_at) "
            "VALUES (?, ?, 'paper', 'NIFTY', '13', '2026-08-10', 'BUY', 75, 100.0, 110.0, "
            "?, 0.0, 0.0, ?, 'fill-1', ?, ?, ?)",
            (
                RUNTIME_ID,
                strategy_id,
                net,
                f"p_io_{strategy_id[:4]}_20260810_0001",
                "2026-08-10T03:45:00+00:00",
                "2026-08-10T04:00:00+00:00",
                "2026-08-10T04:00:00+00:00",
            ),
        )


def _position(repository: ExecutionRepository, strategy_id: str, *, status: str) -> None:
    with repository.database.transaction() as conn:
        conn.execute(
            "INSERT INTO positions (runtime_id, strategy_id, execution_mode, instrument, "
            "security_id, trading_date, status, quantity, average_price, opened_at, "
            "updated_at) "
            "VALUES (?, ?, 'paper', 'NIFTY', '13', '2026-08-10', ?, 75, 100.0, ?, ?)",
            (
                RUNTIME_ID,
                strategy_id,
                status,
                "2026-08-10T03:45:00+00:00",
                "2026-08-10T03:45:00+00:00",
            ),
        )


def _run(database_path: Path, config_root: Path, *ids: str, apply: bool = False) -> int:
    argv = [
        RUNTIME_ID,
        *(ids or (RETIRED,)),
        "--database",
        str(database_path),
        "--config-root",
        str(config_root),
    ]
    if apply:
        argv.append("--apply")
    return purge.main(argv)


def _orphan_it(database_path: Path, order_id: int) -> None:
    """Delete an ``orders`` row out from under its ``paper_fill_quotes``
    child, over a raw connection with foreign keys left off.

    That is not a contrivance — it is exactly how the damage happened. The
    application's own ``Database`` turns ``PRAGMA foreign_keys`` on, so this
    delete is refused through it; the purge script opened a plain
    ``sqlite3.connect``, where the default is off, and SQLite dropped the
    parent without a word.
    """
    conn = sqlite3.connect(database_path)
    try:
        conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        conn.commit()
    finally:
        conn.close()


def _fk_violations(database_path: Path) -> list[tuple]:
    conn = sqlite3.connect(database_path)
    try:
        return list(conn.execute("PRAGMA foreign_key_check"))
    finally:
        conn.close()


def _paper_fill_quote(repository: ExecutionRepository, order_id: int) -> None:
    """A ``paper_fill_quotes`` row — the table that broke the first real
    purge. It references ``orders(id)`` and carries no ``strategy_id``, so a
    strategy-scoped delete cannot see it."""
    with repository.database.transaction() as conn:
        conn.execute(
            "INSERT INTO paper_fill_quotes (order_id, broker_fill_id, quote_bid, "
            "quote_ask, fill_method) VALUES (?, ?, 100.0, 100.5, 'bid_ask')",
            (order_id, f"fill-{order_id}"),
        )


def _order(repository: ExecutionRepository, strategy_id: str, session_id: int) -> int:
    """One ``orders`` row plus the ``order_intents`` parent it requires.

    Written directly rather than through ``OrderLifecycle`` because these
    tests care only about the foreign-key shape — a real order/intent/fill
    chain is exercised in ``test_dashboard_performance_breakdown.py``.
    """
    unique = abs(hash((strategy_id, session_id))) % 100000
    with repository.database.transaction() as conn:
        intent = conn.execute(
            "INSERT INTO order_intents (correlation_id, correlation_namespace, session_id, "
            "runtime_id, strategy_id, execution_mode, trading_date, sequence_number, "
            "instrument, security_id, side, quantity, order_type, product_type, "
            "risk_decision, created_at) "
            "VALUES (?, 'paper', ?, ?, ?, 'paper', '2026-08-10', ?, 'NIFTY', '13', 'BUY', "
            "75, 'MARKET', 'INTRADAY', 'ALLOW', ?)",
            (
                f"p_io_{strategy_id[:4]}_20260810_{unique:05d}",
                session_id,
                RUNTIME_ID,
                strategy_id,
                unique,
                "2026-08-10T03:45:00+00:00",
            ),
        ).lastrowid
        cursor = conn.execute(
            "INSERT INTO orders (intent_id, correlation_id, runtime_id, strategy_id, "
            "execution_mode, status, updated_at) "
            "VALUES (?, ?, ?, ?, 'paper', 'FILLED', ?)",
            (
                intent,
                f"p_io_{strategy_id[:4]}_20260810_{unique:05d}",
                RUNTIME_ID,
                strategy_id,
                "2026-08-10T03:45:00+00:00",
            ),
        )
        return int(cursor.lastrowid or 0)


def _count(database_path: Path, table: str, strategy_id: str) -> int:
    conn = sqlite3.connect(database_path)
    try:
        (n,) = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE strategy_id = ?", (strategy_id,)
        ).fetchone()
        return int(n)
    finally:
        conn.close()


# ================================================================== refusals
def test_refuses_an_id_that_config_still_declares(
    repository: ExecutionRepository, database_path: Path, config_root: Path, capsys
):
    """The gate that matters most: purging a live strategy would destroy the
    running paper-evaluation record with no way back."""
    _session(repository, ACTIVE)
    _trade(repository, ACTIVE)

    assert _run(database_path, config_root, ACTIVE, apply=True) == purge.EXIT_REFUSED
    assert "still declared in config" in capsys.readouterr().out
    assert _count(database_path, "trade_ledger", ACTIVE) == 1


def test_refuses_while_any_worker_in_the_runtime_group_is_alive(
    repository: ExecutionRepository, database_path: Path, config_root: Path, capsys
):
    """Refuses on *another* strategy's live worker, not just the purged id's.
    A retired id never has a worker, so an id-scoped check would always pass
    while the delete still contended for the write lock."""
    _trade(repository, RETIRED)
    _session(repository, ACTIVE, ended=False, pid=os.getpid())

    assert _run(database_path, config_root, apply=True) == purge.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "still running" in out and ACTIVE in out
    assert _count(database_path, "trade_ledger", RETIRED) == 1


def test_a_stale_unfinished_session_whose_process_died_does_not_block(
    repository: ExecutionRepository, database_path: Path, config_root: Path
):
    """This database really does carry ``ended_at IS NULL`` supervisor rows
    from sessions that crashed (14 August, 1 September). An ``ended_at``-only
    gate would refuse forever, so the PID must actually be probed."""
    _trade(repository, RETIRED)
    _session(repository, None, ended=False, process_role="supervisor")

    assert _run(database_path, config_root, apply=True) == purge.EXIT_OK
    assert _count(database_path, "trade_ledger", RETIRED) == 0


def test_refuses_an_id_holding_a_non_closed_position(
    repository: ExecutionRepository, database_path: Path, config_root: Path, capsys
):
    _session(repository, RETIRED)
    _position(repository, RETIRED, status="OPEN")

    assert _run(database_path, config_root, apply=True) == purge.EXIT_REFUSED
    assert "OPEN" in capsys.readouterr().out
    assert _count(database_path, "positions", RETIRED) == 1


def test_refuses_a_missing_database(tmp_path: Path, config_root: Path, capsys):
    assert _run(tmp_path / "nope.db", config_root) == purge.EXIT_REFUSED
    assert "no database" in capsys.readouterr().out


# =================================================================== dry run
def test_dry_run_reports_the_plan_and_deletes_nothing(
    repository: ExecutionRepository, database_path: Path, config_root: Path, capsys
):
    _session(repository, RETIRED)
    _trade(repository, RETIRED, net=4264.05)

    assert _run(database_path, config_root) == purge.EXIT_OK
    out = capsys.readouterr().out
    assert "trade_ledger" in out
    assert "4,264.05" in out
    assert "nothing was deleted" in out
    assert _count(database_path, "trade_ledger", RETIRED) == 1
    assert _count(database_path, "runtime_sessions", RETIRED) == 1


def test_reports_nothing_to_do_for_an_id_with_no_rows(
    database_path: Path, config_root: Path, repository: ExecutionRepository, capsys
):
    assert _run(database_path, config_root, "never_existed") == purge.EXIT_OK
    assert "own no rows" in capsys.readouterr().out


# ===================================================================== apply
def test_apply_deletes_every_row_for_the_retired_id_and_leaves_the_active_one(
    repository: ExecutionRepository, database_path: Path, config_root: Path
):
    _session(repository, RETIRED)
    _trade(repository, RETIRED)
    _position(repository, RETIRED, status="CLOSED")
    _session(repository, ACTIVE)
    _trade(repository, ACTIVE)
    _position(repository, ACTIVE, status="CLOSED")

    assert _run(database_path, config_root, apply=True) == purge.EXIT_OK

    for table in ("trade_ledger", "positions", "runtime_sessions"):
        assert _count(database_path, table, RETIRED) == 0, table
        assert _count(database_path, table, ACTIVE) == 1, table


def test_apply_writes_a_restorable_snapshot_before_deleting(
    repository: ExecutionRepository, database_path: Path, config_root: Path
):
    """The snapshot is the only way back once the delete lands, so it must
    contain the rows that are about to disappear — not a copy taken after."""
    _session(repository, RETIRED)
    _trade(repository, RETIRED)

    assert _run(database_path, config_root, apply=True) == purge.EXIT_OK

    backups = sorted((database_path.parent.parent / "backups").glob("*_pre_purge_*.db"))
    assert len(backups) == 1
    assert _count(backups[0], "trade_ledger", RETIRED) == 1
    assert _count(database_path, "trade_ledger", RETIRED) == 0


def test_apply_purges_several_ids_at_once(
    repository: ExecutionRepository, database_path: Path, config_root: Path
):
    second = "ema_cross_9_21_buy"
    for strategy_id in (RETIRED, second):
        _session(repository, strategy_id)
        _trade(repository, strategy_id)

    assert _run(database_path, config_root, RETIRED, second, apply=True) == purge.EXIT_OK

    assert _count(database_path, "trade_ledger", RETIRED) == 0
    assert _count(database_path, "trade_ledger", second) == 0


def test_every_listed_table_carries_a_strategy_id_in_the_real_schema(
    repository: ExecutionRepository, database_path: Path
):
    """Guards the table list against schema drift. A table renamed or dropped
    by a later migration would otherwise sit in ``STRATEGY_SCOPED_TABLES``
    doing nothing, and a *new* strategy-scoped table would be missed — the
    second half of this test catches that."""
    conn = sqlite3.connect(database_path)
    try:
        present = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        scoped = {
            table
            for table in present
            if any(c[1] == "strategy_id" for c in conn.execute(f"PRAGMA table_info({table})"))
        }
    finally:
        conn.close()

    listed = set(purge.STRATEGY_SCOPED_TABLES)
    assert listed - present == set(), "listed table no longer exists in the schema"
    assert scoped - listed == set(), "a strategy-scoped table is missing from the purge list"


def test_snapshot_name_is_unique_per_run(
    repository: ExecutionRepository, database_path: Path, config_root: Path
):
    """Two purges in the same second must not overwrite each other's only
    route back."""
    first = purge._snapshot(database_path, RUNTIME_ID)
    second = purge._snapshot(database_path, RUNTIME_ID)
    assert first != second
    assert first.exists() and second.exists()


def test_pid_alive_reports_this_process_and_not_a_reaped_child():
    assert purge._pid_alive(os.getpid()) is True
    assert purge._pid_alive(_dead_pid()) is False


def test_dead_pid_helper_does_not_return_a_recycled_live_pid():
    """Sanity check on the fixture itself: a reaped child's PID must still be
    free at the moment the gate probes it, or the refusal tests would pass
    for the wrong reason."""
    pid = _dead_pid()
    assert not purge._pid_alive(pid)


# ============================================== foreign-key orphan sweep
def test_purging_leaves_no_orphaned_foreign_key_children(
    repository: ExecutionRepository, database_path: Path, config_root: Path
):
    """The defect the first production run hit. ``paper_fill_quotes`` points
    at ``orders(id)`` and has no ``strategy_id``, so deleting the retired
    strategy's orders orphaned 46 of its rows — invisible, because SQLite
    does not enforce foreign keys unless ``PRAGMA foreign_keys`` is on, and
    only surfaced later by the dashboard's own ``foreign_key_check``."""
    session = _session(repository, RETIRED)
    retired_order = _order(repository, RETIRED, session.id)
    _paper_fill_quote(repository, retired_order)

    assert _fk_violations(database_path) == []
    assert _run(database_path, config_root, apply=True) == purge.EXIT_OK

    assert _fk_violations(database_path) == [], "purge left dangling children behind"
    assert _count(database_path, "orders", RETIRED) == 0


def test_the_sweep_spares_a_surviving_strategys_children(
    repository: ExecutionRepository, database_path: Path, config_root: Path
):
    """The sweep deletes only rows whose parent is genuinely gone. An active
    strategy's quotes must be untouched."""
    retired_session = _session(repository, RETIRED)
    _paper_fill_quote(repository, _order(repository, RETIRED, retired_session.id))
    active_session = _session(repository, ACTIVE)
    active_order = _order(repository, ACTIVE, active_session.id)
    _paper_fill_quote(repository, active_order)

    assert _run(database_path, config_root, apply=True) == purge.EXIT_OK

    conn = sqlite3.connect(database_path)
    try:
        (surviving,) = conn.execute(
            "SELECT COUNT(*) FROM paper_fill_quotes WHERE order_id = ?", (active_order,)
        ).fetchone()
    finally:
        conn.close()
    assert surviving == 1
    assert _fk_violations(database_path) == []


def test_the_sweep_repairs_a_database_already_orphaned(
    repository: ExecutionRepository, database_path: Path, config_root: Path
):
    """The sweep runs even when no ``strategy_id`` rows match, so re-running
    the script is the repair path for a database damaged by the version that
    lacked it — which is exactly how the live database was fixed."""
    session = _session(repository, RETIRED)
    order_id = _order(repository, RETIRED, session.id)
    _paper_fill_quote(repository, order_id)
    _orphan_it(database_path, order_id)

    assert len(_fk_violations(database_path)) == 1
    assert _run(database_path, config_root, apply=True) == purge.EXIT_OK
    assert _fk_violations(database_path) == []


def test_the_dry_run_reports_orphans_without_touching_them(
    repository: ExecutionRepository, database_path: Path, config_root: Path, capsys
):
    session = _session(repository, RETIRED)
    order_id = _order(repository, RETIRED, session.id)
    _paper_fill_quote(repository, order_id)
    _orphan_it(database_path, order_id)

    assert _run(database_path, config_root) == purge.EXIT_OK
    assert "paper_fill_quotes" in capsys.readouterr().out
    assert len(_fk_violations(database_path)) == 1, "a dry run must change nothing"
