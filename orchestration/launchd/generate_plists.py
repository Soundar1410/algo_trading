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

Two agents, not five
--------------------
``positional_options`` is a **real runtime** with its own supervisor and its
own composition root — an earlier version of this docstring called it a
placeholder, which stopped being true when it gained one. It does not have its
own plist, and does not need one: ``com.soundarraj.algotrading.autostart``
starts *every* enabled runtime through
:func:`scripts._runtimes.resolve_runtime`, so a third runtime group is one
registry entry and no new LaunchAgent.

That single controller replaced the previous ``auth`` @08:45 +
``intraday_options`` @09:00 pair. Two independent agents cannot express "do
not start a runtime until authentication has been validated" — they can only
be scheduled apart and hoped about. :mod:`orchestration.auto_start` makes the
ordering structural instead.

The delayed-volume problem
--------------------------
This project, its ``.venv`` and its logs all live on ``/Volumes/Trading``,
which at login routinely mounts *after* ``launchd`` has already fired the job.
A plist whose ``ProgramArguments[0]`` is the venv interpreter simply fails at
that moment, and with ``KeepAlive=false`` nothing tries again. So the program
is ``/bin/sh`` — on the boot volume, always executable — running a generated
wait loop that ``exec``s the real interpreter once it appears. The loop carries
only absolute paths and no secrets, and its budget is derived from the
committed auto-start schedule rather than an arbitrary constant; Python
re-reads the *configured* deadline the moment it can import anything.

Nothing here loads or unloads anything — this only writes files to disk.
Loading is a separate, explicit operator step; see
``scripts/install_launch_agents.py`` and the runbook.
"""

from __future__ import annotations

import argparse
import plistlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

from common.config.paths import ProjectPaths, resolve_project_root
from common.utils.timeutils import parse_hhmm

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
    #: never a global ``pmset`` change. Only the trading controller needs it,
    #: and it needs it for the *whole session*: the controller supervises its
    #: runtime children rather than spawning and exiting, precisely so this
    #: protection lasts as long as the trading does. The dashboard has no
    #: runtime-hours requirement of its own.
    prevent_sleep: bool = False
    #: Run under a ``/bin/sh`` loop that waits for the project's ``.venv`` to
    #: appear before ``exec``-ing it. Required for anything on the mounted
    #: volume, which is everything.
    wait_for_project: bool = True

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

#: How often the boot-volume wait loop re-checks for the interpreter. Long
#: enough never to spin, short enough that a mount at 09:06 costs seconds.
MOUNT_POLL_SECONDS = 15


PLIST_SPECS: tuple[PlistSpec, ...] = (
    PlistSpec(
        short_name="autostart",
        module_args=["-m", "orchestration.auto_start"],
        # RunAtLoad covers the late login and the wake-from-sleep: a Mac
        # switched on at 09:10 gets its start here, because the 09:00 calendar
        # trigger has already been and gone. A login *before* 09:00 also fires
        # this, and the controller's own gate exits 0 and defers to the
        # calendar trigger rather than trading early.
        run_at_load=True,
        # launchd must not restart this. Bounded retry lives inside the
        # controller (until the session deadline) and inside
        # supervised_launch (per runtime); an unbounded KeepAlive on top of
        # both would defeat every deadline either of them enforces.
        keep_alive=False,
        weekday_hour_minute=[(day, 9, 0) for day in _WEEKDAYS],
        prevent_sleep=True,
    ),
    PlistSpec(
        short_name="dashboard",
        module_args=["-m", "scripts.start_dashboard"],
        # The dashboard has exactly one owner, and it is this agent — never the
        # trading controller. Load-triggered only, with no calendar entry, so
        # it is available independently of trading, weekends included.
        run_at_load=True,
        keep_alive=False,
        weekday_hour_minute=None,
    ),
)


#: Absolute, on the boot volume, present before any external volume mounts.
#: Both are what make the wait loop possible at all.
SHELL_BIN = "/bin/sh"
CAFFEINATE_BIN = "/usr/bin/caffeinate"


def mount_wait_seconds(config_root: Path | None = None) -> int:
    """How long the boot-volume loop waits for the project to appear.

    Derived from the committed auto-start schedule rather than picked: the
    budget is ``session_deadline_time - startup_time``, so a volume that mounts
    at 09:06 — or at 13:00 — still gets a start, and one that never mounts
    stops at the same boundary the Python-side deadline would have stopped at.

    A fixed five-minute cap was the obvious thing to write and would have been
    wrong: ``StartCalendarInterval`` fires once a day, so a mount at 09:06 with
    a 09:00+5min budget means no trading at all that day, with nothing left to
    retry it.

    This is a *floor for the boot-volume phase only*. The moment the project is
    importable, :mod:`orchestration.auto_start` re-reads the real configured
    deadline and re-validates the window; the shell never gets to be the
    authority on when trading may start.
    """
    if config_root is None:
        config_root = Path(__file__).resolve().parents[2] / "config"
    root = config_root
    try:
        from common.config import load_auto_start_config

        cfg = load_auto_start_config(root)
        start = parse_hhmm(cfg.startup_time)
        deadline = parse_hhmm(cfg.session_deadline_time)
    except Exception:
        return 6 * 3600
    span = (
        deadline.hour * 3600 + deadline.minute * 60
    ) - (start.hour * 3600 + start.minute * 60)
    return max(span, MOUNT_POLL_SECONDS)


def _wait_for_project_command(argv: list[str], *, python_bin: Path, budget_seconds: int) -> str:
    """A ``/bin/sh`` script that waits for ``python_bin``, then ``exec``s ``argv``.

    Every token is an absolute path this generator produced; nothing is
    interpolated from the environment and no secret appears anywhere — the
    LaunchAgent passes credentials by not passing them, leaving ``.env`` to be
    read by Python from the working directory.
    """
    attempts = max(budget_seconds // MOUNT_POLL_SECONDS, 1)
    quoted = " ".join(_shell_quote(token) for token in argv)
    target = _shell_quote(str(python_bin))
    return (
        f"n=0; "
        f"while [ ! -x {target} ]; do "
        f"n=$((n+1)); "
        f'if [ "$n" -gt {attempts} ]; then '
        f'echo "autostart: {python_bin} never appeared before the session deadline '
        f'({budget_seconds}s); is /Volumes/Trading mounted?" >&2; exit 75; '
        f"fi; "
        f"sleep {MOUNT_POLL_SECONDS}; "
        f"done; "
        f"exec {quoted}"
    )


def _shell_quote(token: str) -> str:
    """Single-quote a token for ``/bin/sh``.

    Refuses rather than escapes anything containing a single quote: every path
    here is generated from the project root, so a quote in one means something
    is wrong upstream, and quietly escaping it would hide that.
    """
    if "'" in token:
        raise ValueError(f"refusing to build a shell command containing a quote: {token!r}")
    return f"'{token}'"


def _program_arguments(
    spec: PlistSpec, *, python_bin: Path, budget_seconds: int
) -> list[str]:
    args = [str(python_bin), *spec.module_args]
    if spec.prevent_sleep:
        args = [CAFFEINATE_BIN, "-i", "-s", *args]
    if not spec.wait_for_project:
        return args
    return [
        SHELL_BIN,
        "-c",
        _wait_for_project_command(args, python_bin=python_bin, budget_seconds=budget_seconds),
    ]


def build_plist(spec: PlistSpec, paths: ProjectPaths) -> dict[str, object]:
    """One plist's content as a plain dict, ready for :func:`plistlib.dumps`."""
    python_bin = paths.project_root / ".venv" / "bin" / "python"
    launchd_log_dir = paths.log_root / "launchd"

    document: dict[str, object] = {
        "Label": spec.label,
        "ProgramArguments": _program_arguments(
            spec, python_bin=python_bin, budget_seconds=mount_wait_seconds()
        ),
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
