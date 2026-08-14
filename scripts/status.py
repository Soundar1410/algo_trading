#!/usr/bin/env python3
"""Read-only operator status — prints one runtime group's health snapshot.

    .venv/bin/python -m scripts.status [--runtime-id intraday_options] \
        [--trading-date YYYY-MM-DD] [--json]

Opens the operational database read-only (:func:`common.persistence.
connect_readonly`) and builds the same :class:`~common.health.snapshot.
HealthSnapshot` the dashboard's Master and System Health pages read
(:func:`common.health.read_snapshot`) — the whole point of Phase 7 Part 1's
snapshot layer is that no consumer, this script included, writes its own SQL
against these tables.

**Read-only, structurally.** Opens exactly one connection
(``connect_readonly``, which SQLite itself refuses writes against), imports
no broker and no feed. Enforced by ``tests/unit/test_scripts_are_read_only.py``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict

from common.config import load_paths, load_settings
from common.health import HealthSnapshot, read_snapshot
from common.persistence import DatabaseError, connect_readonly
from common.utils.timeutils import local_date_in, now_ist

EXIT_OK = 0
EXIT_NO_DATABASE = 1
EXIT_NOT_READY = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--runtime-id", default="intraday_options", help="Which runtime group to report."
    )
    parser.add_argument(
        "--trading-date", default=None, help="ISO date. Defaults to today (IST)."
    )
    parser.add_argument(
        "--json", action="store_true", help="Machine-readable output instead of the summary."
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    paths = load_paths(settings=settings)
    database_path = paths.database_path(args.runtime_id)
    trading_date = args.trading_date or local_date_in(now_ist()).isoformat()

    if not database_path.is_file():
        print(f"No database yet at {database_path}. Start the supervisor first.")
        return EXIT_NO_DATABASE

    try:
        conn = connect_readonly(database_path)
    except DatabaseError as exc:
        print(f"Cannot open {database_path} read-only: {exc}")
        return EXIT_NO_DATABASE

    try:
        snapshot = read_snapshot(conn, runtime_id=args.runtime_id, trading_date=trading_date)
    except sqlite3.Error as exc:
        print(f"Database not ready ({type(exc).__name__}): {exc}")
        return EXIT_NOT_READY
    finally:
        conn.close()

    if args.json:
        print(json.dumps(asdict(snapshot), indent=2, default=str))
    else:
        _print_human(snapshot)
    return EXIT_OK


def _print_human(snapshot: HealthSnapshot) -> None:
    print(f"Runtime      : {snapshot.runtime_id} ({snapshot.trading_date})")
    print(f"Generated at : {snapshot.generated_at}")

    group = snapshot.group
    if group is None:
        print("Process      : no heartbeat recorded yet")
    else:
        print(
            f"Process      : {group.health_state} pid={group.pid} "
            f"heartbeat_age={group.heartbeat_age_seconds}"
        )

    auth = snapshot.auth
    print(
        f"Auth         : {auth.event or 'none'} source={auth.token_source} "
        f"expiry={auth.token_expiry}"
    )

    md = snapshot.market_data
    print(
        f"Market data  : {md.last_event or 'none'} reconnects={md.reconnect_count} "
        f"stale={md.stale_instrument_count} subs_match={md.subscriptions_match}"
    )

    broker = snapshot.broker
    print(f"Broker       : {'healthy' if broker.healthy else f'unhealthy ({broker.last_error})'}")

    db = snapshot.database
    integrity = "ok" if db.integrity_ok else f"PROBLEMS: {'; '.join(db.integrity_problems)}"
    print(f"Database     : {integrity} journal={db.journal_mode} migration={db.migration_version}")

    print(f"Positions    : open={snapshot.open_positions} orders_today={snapshot.orders_today}")
    print(
        f"Realised P&L : paper={snapshot.realised_pnl_paper:.2f} "
        f"live={snapshot.realised_pnl_live:.2f}"
    )

    if snapshot.strategies:
        print("Strategies:")
        for strategy in snapshot.strategies:
            print(
                f"  {strategy.strategy_id:<16} {strategy.health_state:<12} "
                f"mode={strategy.execution_mode} pid={strategy.pid} "
                f"open_positions={strategy.open_positions} "
                f"square_off={strategy.square_off_state}"
            )
    else:
        print("Strategies   : none recorded")

    if snapshot.recent_errors:
        print("Recent errors:")
        for error in snapshot.recent_errors:
            print(f"  - {error.occurred_at} — {error.message}")


if __name__ == "__main__":
    sys.exit(main())
