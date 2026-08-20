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

No secret is ever written into a plist or passed as an argument: credentials
reach the runtime only through ``.env``, read by Python from the agent's
``WorkingDirectory``.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
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


def _print_commands(header: str, commands: list[list[str]], *, execute: bool) -> int:
    print(header)
    for command in commands:
        print("    " + " ".join(command))
    if not execute:
        print("\n(dry run — nothing was executed. Re-run with --execute to apply.)")
        return EXIT_OK
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True)
        stream = result.stdout.strip() or result.stderr.strip()
        print(f"  -> rc={result.returncode} {stream}")
        if result.returncode != 0:
            return EXIT_FAILED
    return EXIT_OK


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

    source = _source_dir()
    # The launchd log directory must exist, on the boot volume, *before*
    # bootstrapping: launchd opens StandardOutPath/StandardErrorPath while
    # setting the job up, and a missing directory there fails the job before
    # its wait loop ever runs — the exact class of failure the boot-volume
    # paths exist to remove.
    commands: list[list[str]] = [
        ["/bin/mkdir", "-p", str(LAUNCH_AGENTS_DIR)],
        ["/bin/mkdir", "-p", str(boot_log_root())],
    ]
    for spec in PLIST_SPECS:
        target = LAUNCH_AGENTS_DIR / spec.filename
        commands.append(["/bin/cp", str(source / spec.filename), str(target)])
        # bootout first so a reinstall is idempotent rather than an "already
        # loaded" error; it is expected to fail harmlessly the first time.
        commands.append(["/bin/launchctl", "bootout", f"{_domain()}/{spec.label}"])
        commands.append(["/bin/launchctl", "bootstrap", _domain(), str(target)])
        commands.append(["/bin/launchctl", "enable", f"{_domain()}/{spec.label}"])

    print(
        "\nNote: this installs and loads the agents. It does NOT start trading —\n"
        "auto_start.enabled in config/global.yaml is a separate gate, and the\n"
        "controller exits 0 while it is false.\n"
    )
    return _print_commands("Install commands:", commands, execute=args.execute)


def cmd_status(args: argparse.Namespace) -> int:
    commands = [["/bin/launchctl", "print", f"{_domain()}/{spec.label}"] for spec in PLIST_SPECS]
    commands.append(["/bin/launchctl", "list"])
    return _print_commands("Status commands:", commands, execute=args.execute)


def cmd_logs(args: argparse.Namespace) -> int:
    root = resolve_project_root()
    log_dir = root / "logs" / "launchd"
    print("Log files:")
    for spec in PLIST_SPECS:
        print(f"    {log_dir / f'{spec.short_name}.out.log'}")
        print(f"    {log_dir / f'{spec.short_name}.err.log'}")
    print(f"    {root / 'logs' / 'auto_start.log'}   (the controller's own log)")
    print("\nTail them with:")
    print(f"    tail -f {log_dir}/autostart.err.log")
    return EXIT_OK


def cmd_uninstall(args: argparse.Namespace) -> int:
    commands: list[list[str]] = []
    for spec in PLIST_SPECS:
        commands.append(["/bin/launchctl", "disable", f"{_domain()}/{spec.label}"])
        commands.append(["/bin/launchctl", "bootout", f"{_domain()}/{spec.label}"])
        commands.append(["/bin/rm", "-f", str(LAUNCH_AGENTS_DIR / spec.filename)])
    return _print_commands("Rollback commands:", commands, execute=args.execute)


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
