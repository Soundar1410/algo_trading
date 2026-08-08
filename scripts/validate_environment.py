#!/usr/bin/env python3
"""Read-only startup preflight — spec section 13's "before market use" checklist.

    .venv/bin/python -m scripts.validate_environment [--runtime-id intraday_options]

Every check here is read-only: no broker, no feed, no write connection, no
market order. A failure here means "do not start the supervisor yet", not
"something has already gone wrong" — this is meant to run *before*
``scripts/start_runtime``, not after.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from common.config import Settings, load_settings
from common.config.paths import ProjectPaths, ProjectRootError, load_paths
from common.config.secrets import read_secret
from common.persistence import DatabaseError, connect_readonly
from common.process import legacy_system_status
from common.utils.timeutils import now_ist

EXIT_OK = 0
EXIT_PROBLEMS = 1

#: Below this, a full trading day risks a write failing mid-session on logs or the DB.
_MIN_FREE_DISK_GB = 1.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--runtime-id",
        default="intraday_options",
        help="Which runtime group's database to check, if one exists yet.",
    )
    args = parser.parse_args(argv)

    problems: list[str] = []
    warnings: list[str] = []
    settings = load_settings()

    print("Project root")
    try:
        paths = load_paths(settings=settings)
    except ProjectRootError as exc:
        # Nothing below this depends on a root that failed to resolve — the
        # mounted-drive check spec 2673 asks for first, exactly because
        # everything else is derived from it (common/config/paths.py).
        print(f"  FAIL: {exc}")
        return EXIT_PROBLEMS
    print(f"  OK: {paths.project_root}")
    _check_path_drift(settings, paths.project_root, warnings)

    print("Writable directories")
    _check_writable_dirs(paths, problems)

    print("Disk space")
    _check_disk_space(paths.project_root, problems, warnings)

    print("System time")
    print(f"  UTC : {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.gmtime())}")
    print(f"  IST : {now_ist().isoformat()}")
    print("  (no network time source consulted here — confirm against a trusted clock by hand)")

    print("Legacy system")
    _check_legacy_system(problems)

    print("Credentials")
    _check_credentials(settings, problems, warnings)

    print(f"Database ({args.runtime_id})")
    _check_database(paths.database_path(args.runtime_id), problems)

    print()
    for warning in warnings:
        print(f"WARNING: {warning}")
    for problem in problems:
        print(f"PROBLEM: {problem}")

    if problems:
        print(f"\n{len(problems)} problem(s) found. Do not start the supervisor yet.")
        return EXIT_PROBLEMS
    print(f"\nAll checks passed{' with warnings' if warnings else ''}.")
    return EXIT_OK


def _check_path_drift(settings: Settings, resolved_root: Path, warnings: list[str]) -> None:
    """Spec 2674: "detect path changes after moving the repository."

    Only meaningful when ``PROJECT_ROOT`` is actually configured — with
    nothing configured, ``load_paths`` already falls back to wherever this
    code is running from, so there is nothing to drift against.
    """
    if not settings.project_root:
        return

    # Private, and imported anyway: this script lives in the same codebase as
    # common.config.paths, not outside it, and duplicating the walk-to-
    # pyproject.toml logic (as common.process.locks does, for a different
    # reason — staying dependency-free) would just be a second copy to keep
    # in step with the first.
    from common.config.paths import _discover_root_from_source

    source_root = _discover_root_from_source()
    if source_root is not None and source_root != resolved_root:
        warnings.append(
            f"PROJECT_ROOT ({resolved_root}) does not match where this code actually "
            f"lives on disk ({source_root}) — check for a moved or duplicated checkout."
        )


def _check_writable_dirs(paths: ProjectPaths, problems: list[str]) -> None:
    try:
        paths.ensure_writable_dirs()
    except OSError as exc:
        problems.append(f"could not create the platform's data directories: {exc}")
        return

    for name, directory in (
        ("data", paths.data_root),
        ("logs", paths.log_root),
        ("operational", paths.operational_root),
        ("cache", paths.cache_root),
        ("pid", paths.pid_root),
        ("locks", paths.lock_root),
    ):
        if os.access(directory, os.W_OK):
            print(f"  OK: {name} ({directory})")
        else:
            problems.append(f"{name} directory is not writable: {directory}")


def _check_disk_space(project_root: Path, problems: list[str], warnings: list[str]) -> None:
    usage = shutil.disk_usage(project_root)
    free_gb = usage.free / 1_000_000_000
    message = f"{free_gb:.1f} GB free of {usage.total / 1_000_000_000:.1f} GB"
    if free_gb < _MIN_FREE_DISK_GB:
        problems.append(f"low disk space: {message} (below {_MIN_FREE_DISK_GB:.1f} GB)")
    else:
        print(f"  OK: {message}")


def _check_legacy_system(problems: list[str]) -> None:
    """Spec section 12/16: never run the legacy and new systems together.

    Fail-closed: an undetermined ``launchctl`` check (unavailable, errored,
    timed out) is reported as its own problem, distinct from a confirmed
    detection — never printed as "OK: not detected", and never told to
    "unload it first" when nothing was actually confirmed to unload.
    """
    status = legacy_system_status()
    if status.undetermined:
        problems.append(
            f"the legacy Trading_Automation system's state could not be determined "
            f"({status.describe()}) — a check that cannot be verified is treated as "
            "active, not as absent; resolve why launchctl could not be queried, then "
            "retry"
        )
    elif status.active:
        problems.append(
            f"the legacy Trading_Automation system appears active ({status.describe()}) "
            "— unload it first: launchctl bootout gui/$(id -u)/"
            "com.soundarraj.tradingautomation.starttrading"
        )
    else:
        print("  OK: legacy Trading_Automation system not detected")


def _check_credentials(settings: Settings, problems: list[str], warnings: list[str]) -> None:
    client_id = read_secret(settings.dhan_client_id)
    pin = read_secret(settings.dhan_pin)
    totp_secret = read_secret(settings.dhan_totp_secret)

    if not client_id:
        problems.append("DHAN_CLIENT_ID is not set — authentication cannot proceed at all.")
    else:
        print("  OK: DHAN_CLIENT_ID is set")

    if not (pin and totp_secret):
        warnings.append(
            "DHAN_PIN/DHAN_TOTP_SECRET are not both set — a fresh token cannot be "
            "generated; a cached or manually supplied one is still usable."
        )
    else:
        print("  OK: DHAN_PIN and DHAN_TOTP_SECRET are set")

    if settings.has_telegram_credentials():
        print("  OK: Telegram credentials are set")
    else:
        warnings.append(
            "Telegram credentials are not set — notifications stay on the null "
            "channel; the runtime still starts and trades normally."
        )


def _check_database(database_path: Path, problems: list[str]) -> None:
    if not database_path.is_file():
        print(f"  OK: not created yet ({database_path}) — fine before a first run")
        return

    try:
        conn = connect_readonly(database_path)
    except DatabaseError as exc:
        problems.append(f"cannot open {database_path} read-only: {exc}")
        return

    try:
        integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
        bad = [row[0] for row in integrity_rows if row[0] != "ok"]
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()

    if bad:
        problems.append(f"{database_path} failed integrity_check: {bad}")
    elif fk_violations:
        problems.append(f"{database_path} has {len(fk_violations)} foreign-key violation(s)")
    else:
        print(f"  OK: {database_path} (integrity check clean)")


if __name__ == "__main__":
    sys.exit(main())
