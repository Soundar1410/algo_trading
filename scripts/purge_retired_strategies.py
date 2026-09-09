#!/usr/bin/env python3
"""Permanently delete every row belonging to a retired ``strategy_id``.

    .venv/bin/python -m scripts.purge_retired_strategies <runtime_id> <id> [<id> ...]
    .venv/bin/python -m scripts.purge_retired_strategies <runtime_id> <id> --apply

A "retired" id is one no ``config/strategies/**/*.yaml`` declares any more —
in practice the left-hand side of a rename (see
docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md's two rename events). Its rows stay
in the operational database and keep showing up in the Performance tab's
per-strategy breakdown; this is how an operator removes them for good.

**This is the only destructive script in this repository.** Everything else
that touches the operational database either appends or is read-only. It is
therefore built to refuse far more often than it runs:

1. ``--apply`` is required. Without it nothing is deleted and the plan is
   printed instead.
2. A ``strategy_id`` that any config file still declares is refused. The
   whole point is to delete history for ids nothing claims; deleting a live
   strategy's rows would silently destroy the running evaluation record.
3. A ``strategy_id`` with a non-CLOSED ``positions`` row is refused. Deleting
   the book out from under an open position would leave the broker holding
   something this system no longer knows about.
4. Any **live process in the runtime group** is refused, not merely one
   belonging to an id being purged — a retired id never has a worker, so
   that narrower check would always pass, yet the delete would still take a
   write lock on the file seven live workers are writing to every tick.
   Purging is an end-of-day operation and this gate makes that structural.
   Note that ``ended_at IS NULL`` alone is not the test: this database
   carries stale supervisor rows from sessions that died without shutting
   down cleanly (14 August, 1 September), so an ``ended_at``-only gate would
   refuse forever. The PID is checked with signal 0, which tests existence
   without touching the process.
5. A full snapshot of the database file is written to ``data/backups/``
   before the first DELETE, named ``<runtime>_pre_purge_<utc>.db``. The
   deletion is not recoverable from within the database once applied; this
   file is the only way back, and it is left for the operator to remove.

All deletes run inside one transaction: either every table loses the id's
rows or none does, so a failure halfway cannot leave a half-deleted strategy
whose trades are gone but whose fills remain.

**Orphan sweep.** Deleting by ``strategy_id`` alone is not enough, and the
first production run proved it: ``paper_fill_quotes`` references
``orders(id)`` but carries no ``strategy_id`` of its own, so purging two
retired ids on 9 September 2026 removed its parents and left **46 orphaned
rows** behind — surfaced by the dashboard's own ``PRAGMA foreign_key_check``
as "Foreign-key violations: 46" where the pre-purge snapshot had zero.
SQLite does not enforce foreign keys unless ``PRAGMA foreign_keys`` is on,
which is why the delete succeeded silently. Every run now finishes by
deleting rows whose parent no longer exists, in the same transaction, and
verifies ``foreign_key_check`` is clean before committing. The sweep runs
even when no ``strategy_id`` rows matched, so it doubles as the repair path
for a database already damaged this way.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from common.config.paths import load_paths
from dashboards.data.account import _raw_strategy_files

EXIT_OK = 0
EXIT_REFUSED = 1

#: Every table carrying a ``strategy_id``, deleted in this order. Children
#: before parents so a foreign key, if one is ever added, cannot block the
#: transaction midway.
STRATEGY_SCOPED_TABLES = (
    "cycle_leg_marks",
    "strategy_basket_rolls",
    "strategy_basket_roll_anchor",
    "strategy_cycle_entry_stage",
    "strategy_cycle_margin_snapshots",
    "cycle_decision_snapshots",
    "strategy_cycle_events",
    "strategy_cycle_adjustments",
    "strategy_cycle_legs",
    "strategy_cycles",
    "strategy_legs",
    "strategy_baskets",
    "position_marks",
    "trade_ledger",
    "reconciliation_mismatches",
    "reconciliation_runs",
    "fills",
    "orders",
    "order_intents",
    "positions",
    "signals",
    "strategy_state",
    "notifications",
    "errors",
    "audit_events",
    "runtime_heartbeats",
    "runtime_sessions",
)


def _configured_ids(config_root: Path) -> set[str]:
    return {
        str(data.get("strategy_id"))
        for data in _raw_strategy_files(config_root)
        if data.get("strategy_id")
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _has_strategy_id(conn: sqlite3.Connection, table: str) -> bool:
    return any(r[1] == "strategy_id" for r in conn.execute(f"PRAGMA table_info({table})"))


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID exists. Signal 0 performs the
    permission/existence check the kernel would do for a real signal without
    delivering one. ``PermissionError`` means it exists but is owned by
    someone else — still alive, so still a refusal."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _live_processes(conn: sqlite3.Connection, runtime_id: str) -> list[str]:
    """Refuse while **anything** in this runtime group is still running, not
    only a process belonging to one of the ids being purged.

    The narrower check would pass right now: a retired id by definition has
    no worker any more. But the delete runs against the same database file
    seven live workers are writing to every tick, and a bulk delete of ten
    thousand rows takes a write lock they would then contend with mid-trade.
    Purging is an end-of-day operation; this gate makes that structural
    rather than a thing the operator has to remember.
    """
    problems = []
    rows = conn.execute(
        "SELECT strategy_id, process_role, pid, started_at FROM runtime_sessions "
        "WHERE runtime_id = ? AND ended_at IS NULL",
        (runtime_id,),
    ).fetchall()
    for strategy_id, process_role, pid, started_at in rows:
        if _pid_alive(int(pid)):
            who = strategy_id or process_role
            problems.append(
                f"{who}: a process is still running (pid {pid}, started {started_at}). "
                "Purge after the session has shut down."
            )
    return problems


def _open_positions(conn: sqlite3.Connection, ids: tuple[str, ...]) -> list[str]:
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT strategy_id, status, COUNT(*) FROM positions "
        f"WHERE strategy_id IN ({placeholders}) AND status <> 'CLOSED' "
        f"GROUP BY strategy_id, status",
        ids,
    ).fetchall()
    return [f"{sid}: {n} position(s) in status {status}" for sid, status, n in rows]


def _row_counts(
    conn: sqlite3.Connection, ids: tuple[str, ...]
) -> tuple[dict[str, int], list[str]]:
    """Rows per table for these ids, plus the tables actually present."""
    placeholders = ", ".join("?" for _ in ids)
    counts: dict[str, int] = {}
    tables = []
    for table in STRATEGY_SCOPED_TABLES:
        if not _table_exists(conn, table) or not _has_strategy_id(conn, table):
            continue
        tables.append(table)
        (n,) = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE strategy_id IN ({placeholders})", ids
        ).fetchone()
        if n:
            counts[table] = int(n)
    return counts, tables


def _ledger_summary(conn: sqlite3.Connection, ids: tuple[str, ...]) -> list[tuple[str, int, float]]:
    placeholders = ", ".join("?" for _ in ids)
    return [
        (str(sid), int(n), float(net or 0.0))
        for sid, n, net in conn.execute(
            f"SELECT strategy_id, COUNT(*), "
            f"SUM(gross_pnl - entry_charges - exit_charges) "
            f"FROM trade_ledger WHERE strategy_id IN ({placeholders}) GROUP BY strategy_id",
            ids,
        )
    ]


def _orphan_children(conn: sqlite3.Connection) -> dict[str, int]:
    """Rows whose foreign-key parent no longer exists, per table.

    Derived from ``PRAGMA foreign_key_list`` rather than a hardcoded list, so
    a future migration that adds another child table is covered without
    anyone remembering to update this script — the failure mode here is
    silent orphaning, which nothing else in the system would notice.
    """
    counts: dict[str, int] = {}
    tables = [
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    for table in tables:
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})"):
            parent, child_col, parent_col = fk[2], fk[3], fk[4] or "id"
            if parent not in STRATEGY_SCOPED_TABLES:
                continue
            (n,) = conn.execute(
                f"SELECT COUNT(*) FROM {table} c WHERE c.{child_col} IS NOT NULL "
                f"AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE p.{parent_col} = c.{child_col})"
            ).fetchone()
            if n:
                counts[table] = counts.get(table, 0) + int(n)
    return counts


def _delete_orphan_children(conn: sqlite3.Connection) -> dict[str, int]:
    deleted: dict[str, int] = {}
    tables = [
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    for table in tables:
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})"):
            parent, child_col, parent_col = fk[2], fk[3], fk[4] or "id"
            if parent not in STRATEGY_SCOPED_TABLES:
                continue
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE {child_col} IS NOT NULL AND NOT EXISTS "
                f"(SELECT 1 FROM {parent} p WHERE p.{parent_col} = {table}.{child_col})"
            )
            if cursor.rowcount:
                deleted[table] = deleted.get(table, 0) + cursor.rowcount
    return deleted


def _snapshot(database_path: Path, runtime_id: str) -> Path:
    """A full copy of the database file, taken with SQLite's own backup API so
    it is consistent even against a database another process is reading."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = database_path.parent.parent / "backups" / f"{runtime_id}_pre_purge_{stamp}.db"
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime_id")
    parser.add_argument("strategy_ids", nargs="+")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete. Without it the plan is printed and nothing changes.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="override the database path (tests; defaults to the runtime's own).",
    )
    parser.add_argument(
        "--config-root",
        type=Path,
        default=None,
        help="override the config root (tests; defaults to the resolved one).",
    )
    args = parser.parse_args(argv)

    paths = load_paths()
    database_path = args.database or paths.database_path(args.runtime_id)
    config_root = args.config_root or paths.config_root
    ids = tuple(dict.fromkeys(args.strategy_ids))  # de-duplicate, keep order

    if not database_path.is_file():
        print(f"REFUSED: no database at {database_path}")
        return EXIT_REFUSED

    configured = _configured_ids(config_root)
    still_configured = [sid for sid in ids if sid in configured]
    if still_configured:
        print(
            "REFUSED: these ids are still declared in config and are not retired: "
            + ", ".join(sorted(still_configured))
        )
        return EXIT_REFUSED

    conn = sqlite3.connect(database_path)
    try:
        problems = _live_processes(conn, args.runtime_id) + _open_positions(conn, ids)
        if problems:
            print("REFUSED:")
            for problem in problems:
                print(f"  - {problem}")
            return EXIT_REFUSED

        counts, _tables = _row_counts(conn, ids)
        total = sum(counts.values())
        orphans = _orphan_children(conn)
        print(f"Database: {database_path}")
        print(f"Retired ids: {', '.join(ids)}")
        if not total and not orphans:
            print("Nothing to delete — these ids own no rows, and no orphaned children exist.")
            return EXIT_OK

        if counts:
            print("\nRows to delete:")
            for table, n in sorted(counts.items(), key=lambda kv: -kv[1]):
                print(f"  {table:<38} {n:>8,}")
            print(f"  {'TOTAL':<38} {total:>8,}")

        if orphans:
            print("\nOrphaned child rows to sweep (parent already gone):")
            for table, n in sorted(orphans.items(), key=lambda kv: -kv[1]):
                print(f"  {table:<38} {n:>8,}")

        ledger = _ledger_summary(conn, ids)
        if ledger:
            print("\nClosed trades and net P&L this removes from the record:")
            for sid, n, net in sorted(ledger, key=lambda r: -r[2]):
                print(f"  {sid:<30} {n:>4} trade(s)   {net:>14,.2f}")

        if not args.apply:
            print("\nDry run — nothing was deleted. Re-run with --apply to delete.")
            return EXIT_OK

        snapshot = _snapshot(database_path, args.runtime_id)
        print(f"\nSnapshot written: {snapshot}")

        placeholders = ", ".join("?" for _ in ids)
        deleted: dict[str, int] = {}
        with conn:  # one transaction: strategy rows and orphan sweep together
            for table in counts:
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE strategy_id IN ({placeholders})", ids
                )
                deleted[table] = cursor.rowcount
            # Always after the strategy deletes: this pass removes children
            # whose parents those deletes just took away.
            swept = _delete_orphan_children(conn)
            violations = list(conn.execute("PRAGMA foreign_key_check"))
            if violations:
                raise RuntimeError(
                    f"refusing to commit: {len(violations)} foreign-key violation(s) "
                    f"would remain, e.g. {violations[:3]}"
                )

        print(f"\nDeleted {sum(deleted.values()):,} row(s) across {len(deleted)} table(s).")
        if swept:
            print(
                f"Swept {sum(swept.values()):,} orphaned child row(s): "
                + ", ".join(f"{t}={n}" for t, n in sorted(swept.items()))
            )

        remaining, _ = _row_counts(conn, ids)
        if remaining:
            print("WARNING: rows still present after the delete:", remaining)
            return EXIT_REFUSED
        left = list(conn.execute("PRAGMA foreign_key_check"))
        if left:
            print(f"WARNING: {len(left)} foreign-key violation(s) remain")
            return EXIT_REFUSED
        print("Verified: these ids own no rows, and foreign_key_check is clean.")
        return EXIT_OK
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
