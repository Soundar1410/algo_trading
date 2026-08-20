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
    #: How the ``/bin/sh`` wrapper bounds its wait for the project volume.
    #: ``"session_deadline"`` stops at today's configured wall-clock deadline;
    #: ``"elapsed"`` stops after a fixed duration from invocation. See
    #: :func:`_wait_for_project_command` for why the trading controller cannot
    #: use an elapsed budget.
    wait_policy: str = "session_deadline"

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
        # Deliberately NOT the trading controller's 09:00-15:15 boundary. The
        # dashboard is read-only, has no calendar trigger and no session, and
        # is meant to work at weekends and on holidays — a wrapper that gave up
        # at 15:15 would refuse to start a Saturday dashboard at all, and one
        # that gave up at *any* wall-clock time would be applying a trading
        # rule to something that does not trade. An elapsed budget is the right
        # shape here precisely because there is no trigger to lose: the only
        # thing waiting longer costs is an idle shell, and the only thing
        # giving up costs is a re-login.
        wait_policy="elapsed",
    ),
)


#: Absolute, on the boot volume, present before any external volume mounts.
#: These are what make the wait loop possible at all.
SHELL_BIN = "/bin/sh"
CAFFEINATE_BIN = "/usr/bin/caffeinate"
DATE_BIN = "/bin/date"
SLEEP_BIN = "/bin/sleep"

#: How long the dashboard's wrapper waits for the volume, in seconds. Twelve
#: hours: it has no calendar trigger, so giving up early means "no dashboard
#: until the next login" for no benefit. See the spec's own comment.
DASHBOARD_WAIT_SECONDS = 12 * 3600


def boot_log_root() -> Path:
    """Where launchd writes stdout/stderr — on the **boot volume**.

    This is not a stylistic choice. ``launchd`` opens ``StandardOutPath`` and
    ``StandardErrorPath``, and chdirs to ``WorkingDirectory``, *before* it
    executes ``ProgramArguments[0]``. Pointing any of them at
    ``/Volumes/Trading`` means launchd can fail to prepare the job while the
    volume is still unmounted — so the carefully-written wait loop never runs,
    and the delayed-mount recovery this whole design rests on does not
    actually exist. Every prerequisite launchd needs must therefore be
    somewhere that is always present.

    Resolved absolutely at generation time: a committed plist must never
    contain ``~`` or an unexpanded environment variable, neither of which
    launchd expands.
    """
    return Path.home() / "Library" / "Logs" / "algo_trading" / "launchd"


def boot_working_directory() -> Path:
    """A ``WorkingDirectory`` that exists before any volume mounts.

    The project root cannot be used for the reason above. The wrapper ``cd``s
    to the real project root itself, once it exists, so ``.env`` resolution
    and every relative path inside the application behave exactly as before.
    """
    return Path.home()


def session_deadline_hhmm(config_root: Path | None = None) -> str:
    """Today's configured session deadline as a zero-padded ``HHMM`` string.

    The wrapper compares against this as an **absolute wall-clock boundary**,
    never as a duration from its own invocation. The difference is not
    academic: ``RunAtLoad`` fires whenever the Mac is switched on, so a 07:00
    login with the volume unavailable would, under an elapsed
    ``deadline - startup`` budget, expire at 13:15 — and while that shell is
    still waiting, ``launchd`` will not start a second copy for the 09:00
    ``StartCalendarInterval`` event. The day would be silently lost, by the
    very mechanism written to save it.
    """
    if config_root is None:
        config_root = Path(__file__).resolve().parents[2] / "config"
    try:
        from common.config import load_auto_start_config

        deadline = parse_hhmm(load_auto_start_config(config_root).session_deadline_time)
    except Exception:  # generation must not require a loadable config
        deadline = parse_hhmm("15:15")
    return f"{deadline.hour:02d}{deadline.minute:02d}"


def _wait_for_project_command(
    argv: list[str],
    *,
    python_bin: Path,
    project_root: Path,
    short_name: str,
    wait_policy: str,
    deadline_hhmm: str,
    elapsed_seconds: int,
) -> str:
    """A ``/bin/sh`` script that waits for the project, ``cd``s in, then ``exec``s.

    Only boot-volume executables are used before the volume appears:
    ``/bin/sh`` itself, ``/bin/date`` and ``/bin/sleep``. Every path is an
    absolute one this generator produced; nothing is interpolated from the
    environment and no secret appears anywhere — the LaunchAgent passes
    credentials by not passing them, leaving ``.env`` to be read by Python from
    the project root it ``cd``s to below.

    **The ``session_deadline`` policy compares an absolute wall-clock time**,
    not elapsed seconds. ``[ "1$(date +%H%M)" -ge 1<deadline> ]`` looks odd and
    is deliberate: the ``1`` prefix turns ``0700`` into ``10700``, so ``test``
    never sees a leading zero it might read as octal, while the ordering of the
    original times is preserved exactly.

    The ``elapsed`` policy is for jobs with no calendar trigger and no session
    — see the dashboard spec's comment for why an absolute trading boundary
    would be wrong there.
    """
    quoted = " ".join(_shell_quote(token) for token in argv)
    target = _shell_quote(str(python_bin))
    root = _shell_quote(str(project_root))

    if wait_policy == "session_deadline":
        guard = (
            f'if [ "1$({DATE_BIN} +%H%M)" -ge 1{deadline_hhmm} ]; then '
            f'echo "{short_name}: {python_bin} did not appear before the '
            f'{deadline_hhmm[:2]}:{deadline_hhmm[2:]} session deadline; '
            f'is /Volumes/Trading mounted?" >&2; exit 75; fi; '
        )
        preamble = ""
    elif wait_policy == "elapsed":
        attempts = max(elapsed_seconds // MOUNT_POLL_SECONDS, 1)
        guard = (
            f"n=$((n+1)); "
            f'if [ "$n" -gt {attempts} ]; then '
            f'echo "{short_name}: {python_bin} did not appear within '
            f'{elapsed_seconds}s; is /Volumes/Trading mounted? '
            f'Log in again to retry." >&2; exit 75; fi; '
        )
        preamble = "n=0; "
    else:  # pragma: no cover - guarded by the spec table and its test
        raise ValueError(f"unknown wait_policy {wait_policy!r}")

    return (
        f"{preamble}"
        f"while [ ! -x {target} ]; do "
        f"{guard}"
        f"{SLEEP_BIN} {MOUNT_POLL_SECONDS}; "
        f"done; "
        # WorkingDirectory is on the boot volume (launchd must chdir there
        # before this shell runs at all), so the real project root is entered
        # here instead — .env and every relative path resolve from it.
        f'cd {root} || {{ echo "{short_name}: cannot enter {project_root}" >&2; exit 75; }}; '
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
    spec: PlistSpec,
    *,
    python_bin: Path,
    project_root: Path,
    deadline_hhmm: str,
) -> list[str]:
    args = [str(python_bin), *spec.module_args]
    if spec.prevent_sleep:
        args = [CAFFEINATE_BIN, "-i", "-s", *args]
    return [
        SHELL_BIN,
        "-c",
        _wait_for_project_command(
            args,
            python_bin=python_bin,
            project_root=project_root,
            short_name=spec.short_name,
            wait_policy=spec.wait_policy,
            deadline_hhmm=deadline_hhmm,
            elapsed_seconds=DASHBOARD_WAIT_SECONDS,
        ),
    ]


def build_plist(
    spec: PlistSpec,
    paths: ProjectPaths,
    *,
    launchd_log_root: Path | None = None,
    working_directory: Path | None = None,
    deadline_hhmm: str | None = None,
) -> dict[str, object]:
    """One plist's content as a plain dict, ready for :func:`plistlib.dumps`.

    Every path launchd itself must prepare — ``WorkingDirectory``,
    ``StandardOutPath``, ``StandardErrorPath`` — lives on the **boot volume**.
    launchd resolves all three *before* it runs ``ProgramArguments[0]``, so
    putting any of them under ``/Volumes/Trading`` would let job setup fail
    while the volume is still unmounted, and the wait loop below would never
    get the chance to run. ``PROJECT_ROOT`` still points at the real project:
    it is an environment variable, not something launchd has to open.
    """
    python_bin = paths.project_root / ".venv" / "bin" / "python"
    log_dir = launchd_log_root if launchd_log_root is not None else boot_log_root()
    work_dir = working_directory if working_directory is not None else boot_working_directory()
    deadline = deadline_hhmm if deadline_hhmm is not None else session_deadline_hhmm()

    document: dict[str, object] = {
        "Label": spec.label,
        "ProgramArguments": _program_arguments(
            spec,
            python_bin=python_bin,
            project_root=paths.project_root,
            deadline_hhmm=deadline,
        ),
        "WorkingDirectory": str(work_dir),
        "EnvironmentVariables": {"PROJECT_ROOT": str(paths.project_root)},
        "StandardOutPath": str(log_dir / f"{spec.short_name}.out.log"),
        "StandardErrorPath": str(log_dir / f"{spec.short_name}.err.log"),
        "RunAtLoad": spec.run_at_load,
        "KeepAlive": spec.keep_alive,
    }
    if spec.weekday_hour_minute is not None:
        document["StartCalendarInterval"] = [
            {"Weekday": weekday, "Hour": hour, "Minute": minute}
            for weekday, hour, minute in spec.weekday_hour_minute
        ]
    return document


def generate_all(
    root: Path | None = None,
    *,
    launchd_log_root: Path | None = None,
    working_directory: Path | None = None,
    deadline_hhmm: str | None = None,
) -> dict[str, bytes]:
    """Every plist's filename mapped to its serialised XML content."""
    project_root = root if root is not None else resolve_project_root()
    paths = ProjectPaths(project_root=project_root)
    return {
        spec.filename: plistlib.dumps(
            build_plist(
                spec,
                paths,
                launchd_log_root=launchd_log_root,
                working_directory=working_directory,
                deadline_hhmm=deadline_hhmm,
            ),
            fmt=plistlib.FMT_XML,
        )
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
