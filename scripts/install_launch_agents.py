#!/usr/bin/env python3
"""Install, inspect and remove this platform's LaunchAgents.

    .venv/bin/python -m scripts.install_launch_agents validate
    .venv/bin/python -m scripts.install_launch_agents install   [--execute]
    .venv/bin/python -m scripts.install_launch_agents status
    .venv/bin/python -m scripts.install_launch_agents logs
    .venv/bin/python -m scripts.install_launch_agents uninstall [--execute]

**Dry-run by default.** Without ``--execute`` this prints the exact commands
and changes nothing. That default is the point: loading a LaunchAgent is the
step that turns committed files into a Mac that trades by itself at 09:00, and
it should be a decision an operator makes deliberately, having read what it is
about to do — not a side effect of running a script called "install".

Two refusals, both fail-closed and both checked before anything actionable is
printed:

* the legacy ``Trading_Automation`` LaunchAgent is loaded, or its state cannot
  be determined. Two systems trading one account is the failure this project's
  whole legacy-guard exists to prevent, and the manual ``bootout`` command is
  printed instead;
* the Mac's timezone does not match the configured trading timezone, so
  ``StartCalendarInterval``'s 09:00 is not the 09:00 anyone intended.

No secret is ever written into a plist or passed as an argument. Credentials
reach the runtime only through ``.env``, which Python reads from the **project
root** — note that this is no longer the plist's ``WorkingDirectory``. That is
now the operator's boot-volume home, because ``launchd`` must chdir there
before it can run the wrapper at all, and the project may still be unmounted
at that moment. The wrapper itself ``cd``s to ``PROJECT_ROOT`` once the volume
appears and before it ``exec``s, so ``.env`` resolution is unchanged from the
application's point of view.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from common.config import load_auto_start_config
from common.config.paths import resolve_project_root
from common.process import legacy_system_status
from orchestration.auto_start.gate import system_timezone_matches, system_timezone_name
from orchestration.launchd.generate_plists import PLIST_SPECS, boot_log_root

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_FAILED = 1

LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LEGACY_BOOTOUT = "launchctl bootout gui/$UID/com.soundarraj.tradingautomation.starttrading"


def _source_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "orchestration" / "launchd"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _preconditions(config_root: Path) -> list[str]:
    """Everything that must be true before an install may even be described."""
    problems: list[str] = []

    legacy = legacy_system_status()
    if legacy.active:
        detail = "state could not be determined" if legacy.undetermined else legacy.describe()
        problems.append(
            f"legacy Trading_Automation is present ({detail}). Two systems must never "
            f"trade one account. Clear it manually first:\n    {LEGACY_BOOTOUT}"
        )

    try:
        cfg = load_auto_start_config(config_root)
    except Exception as exc:
        problems.append(f"auto_start config could not be read: {exc}")
        return problems

    if cfg.require_system_timezone_match and not system_timezone_matches(cfg.timezone):
        problems.append(
            f"this Mac's timezone is {system_timezone_name() or 'unknown'}, but auto_start "
            f"expects {cfg.timezone}. launchd fires StartCalendarInterval in the Mac's local "
            "zone, so the 09:00 trigger would not be at the configured start time."
        )
    return problems


#: ``launchctl`` return codes that authoritatively mean "this service is not
#: loaded". 3 is ``ESRCH`` ("No such process"); 113 is launchd's own "Could
#: not find specified service". Nothing else may be read as harmless — a
#: permission failure, a malformed domain or an I/O error must all fail
#: closed, because "we could not tell" is not "it was not there".
NOT_LOADED_RETURN_CODES = (3, 113)

#: Message fragments carrying the same authoritative meaning, checked as well
#: as the return code because launchctl's codes have moved between releases.
NOT_LOADED_MESSAGES = (
    "No such process",
    "Could not find specified service",
    "Operation now in progress",  # bootout of a service already on its way out
)


@dataclass(frozen=True)
class Step:
    """One command, and what a non-zero result from it is allowed to mean."""

    command: list[str]
    #: When true, a return code in :data:`NOT_LOADED_RETURN_CODES` (or a
    #: matching message) is an expected outcome rather than a failure. Every
    #: *other* non-zero result still fails closed.
    tolerate_not_loaded: bool = False
    #: Human note appended when the plan is printed.
    note: str = ""

    def rendered(self) -> str:
        text = "    " + " ".join(self.command)
        return f"{text}    # {self.note}" if self.note else text


def _is_not_loaded(result: subprocess.CompletedProcess[str]) -> bool:
    """Whether ``result`` authoritatively says the service was not loaded."""
    if result.returncode in NOT_LOADED_RETURN_CODES:
        return True
    blob = f"{result.stdout} {result.stderr}"
    return any(fragment in blob for fragment in NOT_LOADED_MESSAGES)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True)


def label_is_loaded(label: str) -> bool | None:
    """``True``/``False`` if launchd could tell us, ``None`` if it could not.

    ``None`` is deliberately distinct from ``False``: an unreadable state must
    not be silently treated as "not installed", which would turn an unexpected
    failure into a skipped bootout and a duplicate load.
    """
    result = _run(["/bin/launchctl", "print", f"{_domain()}/{label}"])
    if result.returncode == 0:
        return True
    if _is_not_loaded(result):
        return False
    return None


def _execute(steps: list[Step]) -> int:
    """Run each step in order, stopping at the first genuine failure."""
    for step in steps:
        result = _run(step.command)
        stream = (result.stdout.strip() or result.stderr.strip()).splitlines()
        detail = stream[0] if stream else ""
        if result.returncode == 0:
            print(f"  -> ok: {' '.join(step.command)}")
            continue
        if step.tolerate_not_loaded and _is_not_loaded(result):
            # The expected first-install case: booting out a label that was
            # never loaded. Harmless, and specifically *not* a reason to stop
            # before bootstrap — the bug this tolerance exists to fix.
            print(f"  -> not loaded (expected): {' '.join(step.command)}")
            continue
        print(f"  -> FAILED rc={result.returncode}: {' '.join(step.command)}")
        if detail:
            print(f"     {detail}")
        return EXIT_FAILED
    return EXIT_OK


def _print_plan(header: str, steps: list[Step], *, execute: bool) -> int:
    print(header)
    for step in steps:
        print(step.rendered())
    if not execute:
        print("\n(dry run — nothing was executed. Re-run with --execute to apply.)")
        return EXIT_OK
    print()
    return _execute(steps)


def cmd_validate(args: argparse.Namespace) -> int:
    """Structural check of the generated plists. Read-only, always safe."""
    source = _source_dir()
    failures = 0
    for spec in PLIST_SPECS:
        path = source / spec.filename
        if not path.is_file():
            print(f"MISSING  {path}")
            failures += 1
            continue
        try:
            with path.open("rb") as handle:
                document = plistlib.load(handle)
        except Exception as exc:
            print(f"INVALID  {path}: {exc}")
            failures += 1
            continue
        if shutil.which("plutil"):
            result = subprocess.run(
                ["/usr/bin/plutil", "-lint", str(path)], capture_output=True, text=True
            )
            if result.returncode != 0:
                print(f"PLUTIL   {path}: {result.stdout.strip()}")
                failures += 1
                continue
        print(f"OK       {path.name}  ({document['Label']})")
    print(f"\n{len(PLIST_SPECS) - failures}/{len(PLIST_SPECS)} plist(s) valid.")
    return EXIT_OK if failures == 0 else EXIT_FAILED


def install_steps(*, source: Path, loaded: dict[str, bool | None] | None = None) -> list[Step]:
    """The deterministic install lifecycle, in order.

    Per label, after the directories exist and the plist is copied:

    1. **bootout** — only when the label is known to be loaded, and otherwise
       attempted with the authoritative not-loaded result tolerated. This is
       the defect being fixed: on a genuine first install, ``bootout`` of an
       unloaded label returns non-zero, and treating that as fatal stopped the
       run *before* ``bootstrap``.
    2. **enable** — before bootstrap, not after. ``uninstall`` runs
       ``disable``, and a disabled label cannot be bootstrapped; re-enabling
       first is what makes reinstall-after-uninstall work.
    3. **bootstrap** — load it.

    ``loaded`` lets the caller supply an already-probed state; a label whose
    state could not be determined (``None``) is still booted out, because
    skipping it on an unreadable answer risks a duplicate load.
    """
    loaded = loaded or {}
    steps: list[Step] = [
        Step(["/bin/mkdir", "-p", str(LAUNCH_AGENTS_DIR)]),
        # launchd opens StandardOutPath/StandardErrorPath while setting the job
        # up. A missing directory there fails the job before its wait loop ever
        # runs — the exact class of failure the boot-volume paths exist to
        # remove, so the directory must exist before any bootstrap.
        Step(["/bin/mkdir", "-p", str(boot_log_root())]),
    ]
    for spec in PLIST_SPECS:
        target = LAUNCH_AGENTS_DIR / spec.filename
        service = f"{_domain()}/{spec.label}"
        steps.append(Step(["/bin/cp", str(source / spec.filename), str(target)]))
        if loaded.get(spec.label) is not False:
            steps.append(
                Step(
                    ["/bin/launchctl", "bootout", service],
                    tolerate_not_loaded=True,
                    note="only if loaded; 'not found' is expected on a first install",
                )
            )
        steps.append(
            Step(
                ["/bin/launchctl", "enable", service],
                note="before bootstrap, so reinstall after uninstall works",
            )
        )
        steps.append(Step(["/bin/launchctl", "bootstrap", _domain(), str(target)]))
    return steps


def cmd_install(args: argparse.Namespace) -> int:
    problems = _preconditions(args.config_root)
    if problems:
        print("Refusing to install:")
        for problem in problems:
            print(f"  - {problem}")
        return EXIT_REFUSED

    if cmd_validate(args) != EXIT_OK:
        print("Refusing to install: the generated plists did not validate.")
        return EXIT_REFUSED

    # Probe only when actually executing: a dry run must not run launchctl at
    # all, so it prints the full plan with the bootout marked conditional.
    loaded: dict[str, bool | None] = {}
    if args.execute:
        for spec in PLIST_SPECS:
            loaded[spec.label] = label_is_loaded(spec.label)

    print(
        "\nNote: this installs and loads the agents. It does NOT start trading —\n"
        "auto_start.enabled in config/global.yaml is a separate gate, and the\n"
        "controller exits 0 while it is false.\n"
    )
    return _print_plan(
        "Install commands:",
        install_steps(source=_source_dir(), loaded=loaded),
        execute=args.execute,
    )


def cmd_status(args: argparse.Namespace) -> int:
    steps = [
        Step(
            ["/bin/launchctl", "print", f"{_domain()}/{spec.label}"],
            tolerate_not_loaded=True,
            note="'not found' simply means this agent is not installed",
        )
        for spec in PLIST_SPECS
    ]
    steps.append(Step(["/bin/launchctl", "list"]))
    return _print_plan("Status commands:", steps, execute=args.execute)


def cmd_logs(args: argparse.Namespace) -> int:
    """Where to look. Read-only, and it runs nothing."""
    print("launchd streams (boot volume — readable even when the mount is what failed):")
    for spec in PLIST_SPECS:
        print(f"    {boot_log_root() / f'{spec.short_name}.out.log'}")
        print(f"    {boot_log_root() / f'{spec.short_name}.err.log'}")

    print("\nApplication log (written by the controller, after the volume is up):")
    try:
        project_log = resolve_project_root() / "logs" / "auto_start.log"
    except Exception as exc:  # an unmounted volume must not crash a read-only command
        print(f"    (project root unavailable: {exc})")
    else:
        print(f"    {project_log}")

    print("\nTail them with:")
    print(f"    tail -f {boot_log_root() / 'autostart.err.log'}")
    return EXIT_OK


def uninstall_steps(*, loaded: dict[str, bool | None] | None = None) -> list[Step]:
    """Rollback, ordered so a partially-installed agent still comes out.

    Every launchctl step tolerates the authoritative not-loaded result, and
    the plist removal comes **last and unconditionally** — a stale plist left
    behind merely because ``bootout`` said "not loaded" would silently
    reinstall itself at the next login.
    """
    loaded = loaded or {}
    steps: list[Step] = []
    for spec in PLIST_SPECS:
        service = f"{_domain()}/{spec.label}"
        steps.append(
            Step(
                ["/bin/launchctl", "disable", service],
                tolerate_not_loaded=True,
                note="'not found' is expected if it was never installed",
            )
        )
        steps.append(
            Step(
                ["/bin/launchctl", "bootout", service],
                tolerate_not_loaded=True,
                note="'not found' is expected if it is not loaded",
            )
        )
        steps.append(
            Step(
                ["/bin/rm", "-f", str(LAUNCH_AGENTS_DIR / spec.filename)],
                note="unconditional: never leave a stale plist behind",
            )
        )
    return steps


def cmd_uninstall(args: argparse.Namespace) -> int:
    return _print_plan("Rollback commands:", uninstall_steps(), execute=args.execute)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config-root", type=Path, default=Path("config"))
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually run the commands. Without this, they are only printed.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, handler in (
        ("validate", cmd_validate),
        ("install", cmd_install),
        ("status", cmd_status),
        ("logs", cmd_logs),
        ("uninstall", cmd_uninstall),
    ):
        sub = subparsers.add_parser(name, help=handler.__doc__ or name)
        sub.set_defaults(handler=handler)

    args = parser.parse_args(argv)
    handler = args.handler
    return int(handler(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
