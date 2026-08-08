"""Generates this platform's ``launchd`` property-list files.

    .venv/bin/python -m orchestration.launchd.generate_plists [--check]

Spec section 12 requires "absolute paths only" and "the correct ``.venv``
interpreter" in every plist. Both are things a hand-typed file drifts on
silently the day the repository moves or the interpreter is rebuilt — so
nothing here is hand-typed. This script is the one place that knows the
project root, the ``.venv`` interpreter path and the log directory; the
committed ``.plist`` files under this same directory are its output, never
edited directly. ``--check`` (used by ``tests/unit/test_launchd_plists.py``)
regenerates in memory and fails if the committed files would change — the
drift guard.

Per spec section 12's own list, five plists were named; only three have a
real entrypoint to point at (Phase 8's own audit — see the runbook). No
plist is written for ``positional_options`` (placeholder package, no
supervisor: runbook D56) or ``intraday_stocks`` (does not exist yet: runbook
D34). Writing a plist that points at nothing would fail on its first load,
which is a worse failure mode than a documented, deliberate absence.

Nothing here loads or unloads anything — this only writes files to disk.
Loading is a separate, manual operator step (Phase 8 decision: author and
validate, do not load until Phase 9 has a real strategy).
"""

from __future__ import annotations

import argparse
import plistlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

from common.config.paths import ProjectPaths, resolve_project_root

#: Every LaunchAgent this platform installs shares this label prefix — kept
#: distinct from the legacy system's own ``com.soundarraj.tradingautomation.*``
#: labels (see ``common.process.legacy_guard``) so the two are never
#: ambiguous to ``launchctl`` or to a human reading `launchctl list`.
LABEL_PREFIX = "com.soundarraj.algotrading"


@dataclass(frozen=True)
class PlistSpec:
    """One LaunchAgent, before any path is resolved."""

    short_name: str
    #: Program arguments *after* the interpreter — this module prepends the
    #: resolved ``.venv`` python itself, so specs never repeat it.
    module_args: list[str]
    run_at_load: bool
    keep_alive: bool
    #: Weekday/Hour/Minute triples, local time — ``None`` means load-triggered
    #: only, never on a schedule.
    weekday_hour_minute: list[tuple[int, int, int]] | None = field(default=None)
    #: Wrapped in ``caffeinate -i -s`` — spec section 13's "prevent system
    #: sleep during runtime hours" — scoped to this process's own lifetime,
    #: never a global ``pmset`` change. Only the trading runtime needs it: the
    #: auth bootstrap is a few seconds, and the dashboard has no runtime-hours
    #: requirement of its own.
    prevent_sleep: bool = False

    @property
    def label(self) -> str:
        return f"{LABEL_PREFIX}.{self.short_name}"

    @property
    def filename(self) -> str:
        return f"{self.label}.plist"


#: Weekdays 1 (Mon) - 5 (Fri), matching the legacy agent's own convention
#: (`~/Library/LaunchAgents/com.soundarraj.tradingautomation.controller.plist`)
#: for the one property that must stay human-comparable against it: schedule.
_WEEKDAYS = (1, 2, 3, 4, 5)

PLIST_SPECS: tuple[PlistSpec, ...] = (
    PlistSpec(
        short_name="auth",
        module_args=["-m", "scripts.auth_bootstrap"],
        run_at_load=True,
        keep_alive=False,
        weekday_hour_minute=[(day, 8, 45) for day in _WEEKDAYS],
    ),
    PlistSpec(
        short_name="intraday_options",
        module_args=[
            "-m",
            "orchestration.process_control.supervised_launch",
            "--runtime-id",
            "intraday_options",
        ],
        run_at_load=True,
        keep_alive=False,
        weekday_hour_minute=[(day, 9, 0) for day in _WEEKDAYS],
        prevent_sleep=True,
    ),
    PlistSpec(
        short_name="dashboard",
        module_args=["-m", "scripts.start_dashboard"],
        run_at_load=True,
        keep_alive=False,
        weekday_hour_minute=None,
    ),
)


def _program_arguments(spec: PlistSpec, *, python_bin: Path) -> list[str]:
    args = [str(python_bin), *spec.module_args]
    if spec.prevent_sleep:
        args = ["/usr/bin/caffeinate", "-i", "-s", *args]
    return args


def build_plist(spec: PlistSpec, paths: ProjectPaths) -> dict[str, object]:
    """One plist's content as a plain dict, ready for :func:`plistlib.dumps`."""
    python_bin = paths.project_root / ".venv" / "bin" / "python"
    launchd_log_dir = paths.log_root / "launchd"

    document: dict[str, object] = {
        "Label": spec.label,
        "ProgramArguments": _program_arguments(spec, python_bin=python_bin),
        # Absolute, explicit working directory — required so `.env` (loaded
        # relative to CWD by pydantic-settings, see common/config/settings.py)
        # resolves correctly. launchd starts every agent at "/" otherwise.
        "WorkingDirectory": str(paths.project_root),
        "EnvironmentVariables": {"PROJECT_ROOT": str(paths.project_root)},
        "StandardOutPath": str(launchd_log_dir / f"{spec.short_name}.out.log"),
        "StandardErrorPath": str(launchd_log_dir / f"{spec.short_name}.err.log"),
        "RunAtLoad": spec.run_at_load,
        "KeepAlive": spec.keep_alive,
    }
    if spec.weekday_hour_minute is not None:
        document["StartCalendarInterval"] = [
            {"Weekday": weekday, "Hour": hour, "Minute": minute}
            for weekday, hour, minute in spec.weekday_hour_minute
        ]
    return document


def generate_all(root: Path | None = None) -> dict[str, bytes]:
    """Every plist's filename mapped to its serialised XML content."""
    project_root = root if root is not None else resolve_project_root()
    paths = ProjectPaths(project_root=project_root)
    return {
        spec.filename: plistlib.dumps(build_plist(spec, paths), fmt=plistlib.FMT_XML)
        for spec in PLIST_SPECS
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; fail if the committed files differ from a fresh generation.",
    )
    args = parser.parse_args(argv)

    output_dir = Path(__file__).resolve().parent
    generated = generate_all()

    if args.check:
        drifted = [
            name
            for name, content in generated.items()
            if not (output_dir / name).is_file() or (output_dir / name).read_bytes() != content
        ]
        if drifted:
            print("Drifted from the generator (re-run without --check to fix):")
            for name in drifted:
                print(f"  {name}")
            return 1
        print(f"{len(generated)} plist(s) match the generator.")
        return 0

    for name, content in generated.items():
        (output_dir / name).write_bytes(content)
        print(f"wrote {output_dir / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
