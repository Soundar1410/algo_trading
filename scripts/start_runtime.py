#!/usr/bin/env python3
"""Start a runtime group — a thin wrapper over the real entrypoint.

    .venv/bin/python -m scripts.start_runtime [intraday_options] [--config-root config]

Spec section 11 names this ``scripts/start_runtime intraday_options``; each
runtime group's own real entrypoint (``python -m runtimes.<runtime_id>``)
takes the same argument as ``--runtime-id`` instead. This script is nothing
but that translation plus resolving *which* entrypoint to call
(``scripts._runtimes.resolve_entrypoint`` — one registry, so a third
runtime group is one new entry there, not a new branch here) — every other
line of behaviour (authentication, live-gate admission, spawning workers
under a supervisor) lives in each runtime's own ``__main__.py`` and is not
duplicated here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts._runtimes import resolve_entrypoint

EXIT_UNKNOWN_RUNTIME = 7


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "runtime_id", nargs="?", default="intraday_options", help="Which runtime group to start."
    )
    parser.add_argument(
        "--config-root", type=Path, default=Path("config"), help="Root of the config/ tree."
    )
    args = parser.parse_args(argv)

    try:
        entrypoint = resolve_entrypoint(args.runtime_id)
    except KeyError as exc:
        print(str(exc))
        return EXIT_UNKNOWN_RUNTIME

    return entrypoint(["--runtime-id", args.runtime_id, "--config-root", str(args.config_root)])


if __name__ == "__main__":
    sys.exit(main())
