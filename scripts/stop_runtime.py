#!/usr/bin/env python3
"""Stop a running supervisor — verify real ownership of its PID file, then signal it.

    .venv/bin/python -m scripts.stop_runtime [--runtime-id intraday_options]

Never guesses. A PID file naming a live process that is *not* the process
that wrote it (PID reuse) is refused, not signalled — signalling the wrong
process is exactly the incident Phase 7 Part 4's PID hardening
(``common.process.locks``) exists to prevent. Sends ``SIGTERM`` only; the
supervisor's own ``shutdown_signals`` handler turns that into an orderly
shutdown. Writes nothing except one row to the audit trail — no direct write
to ``positions``, ``orders``, or any other trading table.
"""

from __future__ import annotations

import argparse
import sys

from common.process import supervisor_lock
from scripts._operator_common import load_project, open_audit_repository, terminate_verified_owner

EXIT_OK = 0
EXIT_NOT_RUNNING = 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--runtime-id", default="intraday_options", help="Which runtime group to stop."
    )
    args = parser.parse_args(argv)

    _, paths = load_project()
    lock = supervisor_lock(
        runtime_id=args.runtime_id, lock_dir=paths.lock_root, pid_dir=paths.pid_root
    )
    repository = open_audit_repository(paths.database_path(args.runtime_id))

    stopped, message = terminate_verified_owner(
        lock,
        repository=repository,
        runtime_id=args.runtime_id,
        strategy_id=None,
        action="stop_runtime",
    )
    print(message)
    return EXIT_OK if stopped else EXIT_NOT_RUNNING


if __name__ == "__main__":
    sys.exit(main())
