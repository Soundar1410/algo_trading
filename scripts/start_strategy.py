#!/usr/bin/env python3
"""Start one strategy — still through its runtime group's supervisor.

    .venv/bin/python -m scripts.start_strategy io_supertrend_fast_v1 \
        [--runtime-id intraday_options] [--config-root config]

Spec section 11 names this ``scripts/start_strategy io_supertrend_fast_v1``.
A thin wrapper, like ``scripts/start_runtime.py`` — the real entrypoint for
each runtime group (resolved via ``scripts._runtimes.resolve_entrypoint``)
already knows how to admit exactly what it should; this script only
translates the spec's positional argument into that runtime's own
``--strategy-id`` filter.

Every runtime group supports the same real ``--strategy-id`` filter (Phase
7 Part 4 added it for ``intraday_options``; Phase 5 of the gap-closing
session generalized ``positional_options`` to the same shared-hub/
multi-worker shape and gave it the identical flag) — still through that
runtime's own supervisor, never a bare worker spawned outside one. This
script therefore stays generic across every registered runtime rather than
special-casing any one of them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts._runtimes import resolve_entrypoint

EXIT_STRATEGY_NOT_FOUND = 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("strategy_id", help="Which strategy to start.")
    parser.add_argument(
        "--runtime-id",
        default="intraday_options",
        help="Which runtime group this strategy runs in.",
    )
    parser.add_argument(
        "--config-root", type=Path, default=Path("config"), help="Root of the config/ tree."
    )
    args = parser.parse_args(argv)

    try:
        entrypoint = resolve_entrypoint(args.runtime_id)
    except KeyError as exc:
        print(str(exc))
        return EXIT_STRATEGY_NOT_FOUND

    return entrypoint(
        [
            "--runtime-id", args.runtime_id,
            "--strategy-id", args.strategy_id,
            "--config-root", str(args.config_root),
        ]
    )


if __name__ == "__main__":
    sys.exit(main())
