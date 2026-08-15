#!/usr/bin/env python3
"""Start one strategy — still through its runtime group's supervisor.

    .venv/bin/python -m scripts.start_strategy io_supertrend_fast_v1 \
        [--runtime-id intraday_options] [--config-root config]

Spec section 11 names this ``scripts/start_strategy io_supertrend_fast_v1``.
A thin wrapper, like ``scripts/start_runtime.py`` — the real entrypoint for
each runtime group (resolved via ``scripts._runtimes.resolve_entrypoint``)
already knows how to admit exactly what it should; this script only
translates the spec's positional argument.

``intraday_options`` supports a real ``--strategy-id`` filter (Phase 7 Part
4: still through its supervisor, never a bare worker). ``positional_options``
does not — it drives exactly one strategy per process by design (see
``runtimes/positional_options/worker.py``'s own module docstring), so this
script instead verifies ``strategy_id`` really is that runtime's one enabled
strategy *before* delegating, and refuses with a clear message if not,
rather than silently starting whatever happens to be enabled.
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

    if args.runtime_id == "intraday_options":
        return entrypoint(
            [
                "--runtime-id", args.runtime_id,
                "--strategy-id", args.strategy_id,
                "--config-root", str(args.config_root),
            ]
        )

    # Every other runtime group (positional_options today) admits exactly
    # one strategy per process and has no --strategy-id flag to filter
    # with — verify the requested id really is the one enabled strategy
    # before starting anything, so a stale/wrong strategy_id fails loudly
    # rather than silently starting whichever strategy happens to be
    # enabled.
    from common.config import discover_enabled_strategies, load_settings

    settings = load_settings()
    enabled = discover_enabled_strategies(args.config_root, args.runtime_id, settings=settings)
    enabled_ids = {cfg.strategy.strategy_id for cfg in enabled}
    if args.strategy_id not in enabled_ids:
        print(
            f"{args.strategy_id!r} is not an enabled strategy under "
            f"runtimes/{args.runtime_id}.yaml (enabled: {sorted(enabled_ids) or ['none']})."
        )
        return EXIT_STRATEGY_NOT_FOUND

    return entrypoint(["--runtime-id", args.runtime_id, "--config-root", str(args.config_root)])


if __name__ == "__main__":
    sys.exit(main())
