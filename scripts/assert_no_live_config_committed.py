#!/usr/bin/env python3
"""Read-only guard — refuses to let a live-enabling value slip into the
committed configuration tree (CLAUDE.md: "every committed config must keep
all live gates fail-closed... no `mode: live` in committed YAML").

    .venv/bin/python -m scripts.assert_no_live_config_committed [config_root]

Every gate this checks is independently enforced at load/runtime too
(``common.config.models.effective_live_gate``, ``ResolvedConfig``'s own
validators) — this script exists so a violation is caught at commit/CI time,
before it ever reaches a running worker, and so a reviewer does not have to
hold the whole gate chain in their head to notice a stray ``mode: live`` in a
diff. It reads raw YAML directly rather than the full pydantic-validated
``ResolvedConfig`` path deliberately: ``discover_enabled_strategies`` only
ever returns *enabled* strategies, which would silently skip exactly the
case of a disabled-but-``mode: live`` strategy file — still forbidden by the
rule above regardless of ``enabled``.

Read-only: opens no database, no broker, no network. A malformed or
unreadable YAML file is reported as a problem, never silently skipped —
"cannot verify this file is safe" is treated the same as "this file is
unsafe", the same fail-closed convention ``validate_environment.py``'s own
legacy-system check uses.
"""

from __future__ import annotations

import sys
from pathlib import Path

from common.config.loader import _read_yaml
from common.config.paths import load_paths

EXIT_OK = 0
EXIT_PROBLEMS = 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    config_root = Path(argv[0]) if argv else load_paths().config_root

    problems: list[str] = []
    problems.extend(_check_global(config_root))
    problems.extend(_check_runtimes(config_root))
    problems.extend(_check_strategies(config_root))

    if problems:
        print(f"{len(problems)} live-config problem(s) found under {config_root}:")
        for problem in problems:
            print(f"  PROBLEM: {problem}")
        return EXIT_PROBLEMS

    print(f"OK: no live-enabling value is committed under {config_root}.")
    return EXIT_OK


def _check_global(config_root: Path) -> list[str]:
    path = config_root / "global.yaml"
    if not path.is_file():
        return [f"{path}: not found — cannot verify the global live gate is closed"]
    try:
        raw = _read_yaml(path)
    except Exception as exc:
        return [f"{path}: could not be read ({exc}) — treated as unsafe"]

    problems: list[str] = []
    global_section = raw.get("global", {}) or {}
    if global_section.get("live_trading_enabled") is True:
        problems.append(f"{path}: global.live_trading_enabled is true")

    runtime_defaults = raw.get("runtime_defaults", {}) or {}
    if runtime_defaults.get("live_execution_allowed") is True:
        problems.append(f"{path}: runtime_defaults.live_execution_allowed is true")

    strategy_defaults = raw.get("strategy_defaults", {}) or {}
    if strategy_defaults.get("mode") == "live":
        problems.append(f"{path}: strategy_defaults.mode is 'live'")
    if strategy_defaults.get("live_approved") is True:
        problems.append(f"{path}: strategy_defaults.live_approved is true")

    return problems


def _check_runtimes(config_root: Path) -> list[str]:
    runtimes_dir = config_root / "runtimes"
    if not runtimes_dir.is_dir():
        return []
    problems: list[str] = []
    for path in sorted(runtimes_dir.glob("*.yaml")):
        try:
            raw = _read_yaml(path)
        except Exception as exc:
            problems.append(f"{path}: could not be read ({exc}) — treated as unsafe")
            continue
        if raw.get("live_execution_allowed") is True:
            problems.append(f"{path}: live_execution_allowed is true")
    return problems


def _check_strategies(config_root: Path) -> list[str]:
    strategies_dir = config_root / "strategies"
    if not strategies_dir.is_dir():
        return []
    problems: list[str] = []
    for path in sorted(strategies_dir.glob("*.yaml")):
        try:
            raw = _read_yaml(path)
        except Exception as exc:
            problems.append(f"{path}: could not be read ({exc}) — treated as unsafe")
            continue
        # Checked regardless of `enabled` — a disabled strategy still must
        # not declare `mode: live` in committed YAML (CLAUDE.md, verbatim).
        if raw.get("mode") == "live":
            problems.append(f"{path}: mode is 'live'")
        if raw.get("live_approved") is True:
            problems.append(f"{path}: live_approved is true")
    return problems


if __name__ == "__main__":
    sys.exit(main())
