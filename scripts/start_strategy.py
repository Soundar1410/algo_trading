#!/usr/bin/env python3
"""Start one strategy — still through its runtime group's supervisor.

    .venv/bin/python -m scripts.start_strategy io_supertrend_fast_v1 \
        [--runtime-id intraday_options] [--config-root config]

Spec section 11 names this ``scripts/start_strategy io_supertrend_fast_v1``.
A thin wrapper, like ``scripts/start_runtime.py`` — the real entrypoint
(``runtimes/intraday_options/__main__.py``) already accepts ``--strategy-id``
to admit only one strategy (Phase 7 Part 4), and per-strategy start never
spawns a bare worker outside a supervisor; this script only translates the
spec's positional argument into that flag.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from runtimes.intraday_options.__main__ import main as run_supervisor


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

    return run_supervisor(
        [
            "--runtime-id",
            args.runtime_id,
            "--strategy-id",
            args.strategy_id,
            "--config-root",
            str(args.config_root),
        ]
    )


if __name__ == "__main__":
    sys.exit(main())
