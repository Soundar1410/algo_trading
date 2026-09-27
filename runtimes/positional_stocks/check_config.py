"""Read-only configuration check to run after ANY config edit (D129).

    python -m runtimes.positional_stocks.check_config [--config-root config]

Why it exists: ``common/config/loader.py`` loads **every** file under
``config/strategies/**`` whenever ``intraday_options`` or
``positional_options`` starts, so a syntax error in
``config/strategies/positional_stocks/wsr1_weekly_stochrsi.yaml`` would make
both paper runtimes refuse to start the next morning. This command catches
that before a commit.

It checks, and changes nothing:

1. every runtime in ``config/runtimes/*.yaml``, through the shared loader's
   ``discover_strategies`` — exactly what each supervisor does at start;
2. the two positional_stocks files for duplicate keys (D126), then the strict
   ``RunConfig`` binding (D119);
3. ``scripts.assert_no_live_config_committed``.

Prints ``OK`` or each problem on its own line. Exit 0 or 1.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
from collections.abc import Sequence
from pathlib import Path

#: The runtimes a broken strategy file would stop at their next start.
PAPER_RUNTIMES = ("intraday_options", "positional_options")


def check(config_root: Path) -> list[str]:
    """Every problem found under ``config_root``; empty means OK."""
    from common.config import Settings, discover_strategies
    from scripts import assert_no_live_config_committed

    from .run_config import RunConfig, RunRefused

    problems: list[str] = []
    runtimes = sorted(p.stem for p in (config_root / "runtimes").glob("*.yaml"))
    if not runtimes:
        problems.append(f"{config_root / 'runtimes'}: no runtime files found")
    for runtime_id in runtimes:
        try:
            discover_strategies(config_root, runtime_id, settings=Settings())
        except Exception as exc:  # anything the shared loader would refuse on
            detail = " ".join(str(exc).split())
            problems.append(
                f"runtime {runtime_id}: the shared config loader refuses: {detail} — "
                f"{' and '.join(PAPER_RUNTIMES)} would refuse to start"
            )
    try:
        RunConfig.from_config(config_root)
    except RunRefused as exc:
        problems.append(f"positional_stocks: {exc}")
    guard_output = io.StringIO()
    with contextlib.redirect_stdout(guard_output):
        guard = assert_no_live_config_committed.main([str(config_root)])
    if guard != assert_no_live_config_committed.EXIT_OK:
        lines = [line.strip() for line in guard_output.getvalue().splitlines() if line.strip()]
        problems.append("live-config guard: " + "; ".join(lines))
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m runtimes.positional_stocks.check_config",
        description="Read-only check of config/ after an edit (D129). Changes nothing.",
    )
    parser.add_argument("--config-root", type=Path, default=Path("config"))
    args = parser.parse_args(argv)
    problems = check(args.config_root)
    if not problems:
        print(
            f"OK: {args.config_root} loads for every runtime, binds for positional_stocks, "
            "and commits no live-enabling value."
        )
        return 0
    for problem in problems:
        print(f"PROBLEM: {problem}")
    print(f"\n{len(problems)} problem(s). Do not commit this configuration.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
