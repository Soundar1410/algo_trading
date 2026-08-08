"""Detects whether the legacy ``Trading_Automation`` system is active.

Spec section 12 ("Do not start legacy and new trading systems together") and
section 16 ("Prevent simultaneous execution of the old and new systems") name
this as a LaunchAgent requirement; Phase 8's own gate names it as one of the
six manual tests ("old-system exclusion"). The runbook recorded this as an
open item to settle "before LaunchAgents in Phase 8" (D-notes, Phase 0/3
audit) — this module is that settlement.

The legacy system is not hypothetical. During the Phase 8 audit it was found
**installed and scheduled** on this machine:
``~/Library/LaunchAgents/com.soundarraj.tradingautomation.controller.plist``,
``RunAtLoad`` plus a weekday-09:00 ``StartCalendarInterval``, execing
``/Volumes/Trading/Trading_Automation/common/startup/start_all.sh``. Its
filename (``...controller.plist``) does not match its ``Label``
(``...starttrading``) — ``launchctl`` keys on the label, never the filename,
so any check or any unload must use the label.

Two independent signals are checked, because either alone can miss it:

* **launchd** — the label may be loaded (queued to run) with no process alive
  right now. Caught by asking ``launchctl`` directly, never by grepping its
  unlabelled ``list`` dump.
* **A running process** — the legacy system could have been started by hand,
  bypassing ``launchd`` entirely. Caught by scanning for any live process
  whose executable or command line sits under the legacy project root.

The legacy root is matched as an exact path-component prefix
(``/Volumes/Trading/Trading_Automation``), never the shared mount root
(``/Volumes/Trading``) that this repository (``/Volumes/Trading/algo_trading``)
also lives on — a mount-root prefix would flag this codebase's own processes
as the legacy system it is trying to detect.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import psutil

from common.logging import get_logger

_log = get_logger(__name__)

#: The legacy LaunchAgent's *label* — read directly from the installed plist
#: during the Phase 8 audit. Never the filename; see the module docstring.
LEGACY_LAUNCHD_LABEL = "com.soundarraj.tradingautomation.starttrading"

#: Where that plist lives on disk. The detector itself never reads this file —
#: it only asks ``launchctl`` for the label above. This path exists so this
#: module's own real-machine test can read the *actual* installed plist and
#: assert ``LEGACY_LAUNCHD_LABEL`` still matches its real ``Label``, catching
#: a future rename/edit of the legacy agent that would otherwise leave this
#: guard silently blind.
LEGACY_LAUNCHD_PLIST_PATH = (
    Path.home() / "Library" / "LaunchAgents" / "com.soundarraj.tradingautomation.controller.plist"
)

#: The legacy system's own project root — matched as a path-component prefix,
#: never the shared ``/Volumes/Trading`` mount root. See the module docstring.
LEGACY_PROJECT_ROOT = Path("/Volumes/Trading/Trading_Automation")

#: The exact operator command to stop the legacy LaunchAgent. Keyed on the
#: label, not the filename — surfaced in every refusal message so an operator
#: never has to go look it up.
LEGACY_UNLOAD_COMMAND = f"launchctl bootout gui/$(id -u)/{LEGACY_LAUNCHD_LABEL}"


@dataclass(frozen=True)
class LegacySystemStatus:
    """What was found. Either signal being true means: do not start."""

    launchd_label_loaded: bool
    launchd_detail: str
    process_running: bool
    process_detail: str

    @property
    def active(self) -> bool:
        return self.launchd_label_loaded or self.process_running

    def describe(self) -> str:
        parts = []
        if self.launchd_label_loaded:
            parts.append(f"launchd: {self.launchd_detail}")
        if self.process_running:
            parts.append(f"process: {self.process_detail}")
        return "; ".join(parts) if parts else "not detected"


def legacy_system_status() -> LegacySystemStatus:
    """Check both signals. Read-only: no process is signalled, nothing is unloaded."""
    loaded, launchd_detail = _launchd_label_loaded()
    running, process_detail = _legacy_process_running()
    return LegacySystemStatus(
        launchd_label_loaded=loaded,
        launchd_detail=launchd_detail,
        process_running=running,
        process_detail=process_detail,
    )


def _launchd_label_loaded(label: str = LEGACY_LAUNCHD_LABEL) -> tuple[bool, str]:
    """Ask launchd directly whether ``label`` is loaded (running or not).

    ``launchctl list <label>`` exits 0 when the label is loaded and nonzero
    otherwise — exact, and independent of ``launchctl list``'s unlabelled
    output format, which this deliberately never parses.
    """
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Not macOS, or launchctl hung/unavailable (e.g. most test/CI
        # environments). This function reports what it could find, not a
        # safety decision — callers treat "could not determine" as their own
        # concern; `legacy_system_status()` is only ever consulted before a
        # real start, never silently skipped.
        _log.warning("could not query launchctl for %s: %s", label, exc)
        return False, f"launchctl unavailable ({exc})"

    if result.returncode == 0:
        return True, f"label {label!r} is loaded"
    return False, f"label {label!r} is not loaded"


def _legacy_process_running(root: Path = LEGACY_PROJECT_ROOT) -> tuple[bool, str]:
    """Any live process whose executable or command line sits under ``root``.

    Matched with :meth:`Path.is_relative_to` on path components, not a string
    prefix — ``/Volumes/Trading/Trading_AutomationX`` must not match
    ``/Volumes/Trading/Trading_Automation``. ``root`` defaults to the legacy
    system's own project root, never the shared mount root that this
    repository also lives under.
    """
    for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            info = proc.info
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

        candidates: list[str] = []
        if info.get("exe"):
            candidates.append(info["exe"])
        candidates.extend(arg for arg in (info.get("cmdline") or []) if arg)

        for candidate in candidates:
            try:
                candidate_path = Path(candidate)
            except ValueError:
                continue
            if candidate_path.is_absolute() and candidate_path.is_relative_to(root):
                return True, f"pid {info['pid']} ({candidate})"

    return False, f"no live process found under {root}"
