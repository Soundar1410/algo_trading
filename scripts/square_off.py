#!/usr/bin/env python3
"""Ask a running worker to square off — never a direct write to ``positions``.

    .venv/bin/python -m scripts.square_off --strategy-id io_vwap_straddle_v1 \
        --confirm [--runtime-id intraday_options] [--reason "..."]

Writes a request file the running worker itself polls and executes through
its own square-off path (``runtimes/intraday_options/worker.py``'s
``_maybe_square_off`` on the fixture path, the ported engine's
``request_square_off`` on the engine path — both via
``common/process/square_off_requests.py``). This script never opens the
strategy's database for a write to ``positions``; the only table it writes
is the audit trail.

**Requires ``--confirm``.** A square-off is a live-impacting command (spec
section 11): without the flag, nothing is written and nothing is asked of
any running worker.
"""

from __future__ import annotations

import argparse
import sys

from common.process import square_off_request_path, write_square_off_request
from scripts._operator_common import current_actor, load_project, open_audit_repository

EXIT_OK = 0
EXIT_NOT_CONFIRMED = 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--runtime-id",
        default="intraday_options",
        help="Which runtime group this strategy runs in.",
    )
    parser.add_argument("--strategy-id", required=True, help="Which strategy to square off.")
    parser.add_argument(
        "--reason",
        default="operator requested via scripts/square_off",
        help="Recorded in the audit trail and on the request file.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required. Without it, nothing is written and no worker is asked to act.",
    )
    args = parser.parse_args(argv)

    if not args.confirm:
        print("Refusing without --confirm: this asks a live worker to close its open position(s).")
        print("Re-run with --confirm to proceed.")
        return EXIT_NOT_CONFIRMED

    _, paths = load_project()
    actor = current_actor()
    request_path = square_off_request_path(paths.runtime_root, args.runtime_id, args.strategy_id)
    request = write_square_off_request(request_path, requested_by=actor, reason=args.reason)

    repository = open_audit_repository(paths.database_path(args.runtime_id))
    repository.record_audit_event(
        runtime_id=args.runtime_id,
        action="square_off_requested",
        actor=actor,
        strategy_id=args.strategy_id,
        detail=f"requested at {request.requested_at}: {request.reason}",
    )

    print(f"Square-off requested for strategy {args.strategy_id!r} in {args.runtime_id!r}.")
    print(f"  request file : {request_path}")
    print(
        "  The running worker will complete it on its own square-off path and record "
        "a square_off_completed audit event when it does. If nothing is running, the "
        "request stays pending until a worker for this strategy next starts."
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
