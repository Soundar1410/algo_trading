"""common.process.legacy_guard: detecting the still-installed legacy system.

Two kinds of test, deliberately kept apart:

* **Synthetic fixtures** exercise the matching logic in isolation — portable,
  run everywhere including CI, with ``subprocess``/``psutil`` replaced.
* **Real-machine tests** (bottom of this file) check the module's own
  constants against the *actual* installed legacy plist on this Mac — the one
  check whose correctness matters most before any ``algo_trading`` plist is
  ever loaded, per the plan's own reasoning: a guard that compiles, passes
  every synthetic fixture, and still does not recognise the real thing is
  exactly the failure mode this module exists to catch. Both skip (not fail)
  on a machine without that plist path — CI, or a future dev machine.
"""

from __future__ import annotations

import plistlib
import subprocess

import psutil
import pytest

from common.process import legacy_guard
from common.process.legacy_guard import (
    LEGACY_LAUNCHD_LABEL,
    LEGACY_LAUNCHD_PLIST_PATH,
    LaunchdLabelState,
    LegacySystemStatus,
    legacy_system_status,
)


class _FakeCompletedProcess:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


class _FakeProcess:
    def __init__(self, info: dict) -> None:
        self.info = info


class _UnreadableProcess:
    """Mimics a process that died or became inaccessible mid-scan."""

    @property
    def info(self) -> dict:
        raise psutil.NoSuchProcess(999)


def _no_legacy_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(legacy_guard.psutil, "process_iter", lambda *a, **k: [])


def _label_returncode(monkeypatch: pytest.MonkeyPatch, returncode: int) -> None:
    monkeypatch.setattr(
        legacy_guard.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(returncode)
    )


# ======================================================= synthetic: launchd
def test_a_loaded_label_is_detected(monkeypatch: pytest.MonkeyPatch):
    _label_returncode(monkeypatch, 0)
    _no_legacy_processes(monkeypatch)

    status = legacy_system_status()

    assert status.launchd_state is LaunchdLabelState.ACTIVE
    assert status.active is True
    assert status.undetermined is False
    assert LEGACY_LAUNCHD_LABEL in status.launchd_detail


def test_an_unloaded_label_is_not_detected(monkeypatch: pytest.MonkeyPatch):
    _label_returncode(monkeypatch, 1)
    _no_legacy_processes(monkeypatch)

    status = legacy_system_status()

    assert status.launchd_state is LaunchdLabelState.INACTIVE


def test_launchctl_unavailable_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch):
    def _raise(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("no launchctl on this machine")

    monkeypatch.setattr(legacy_guard.subprocess, "run", _raise)
    _no_legacy_processes(monkeypatch)

    status = legacy_system_status()

    assert status.launchd_state is LaunchdLabelState.UNKNOWN
    assert "unavailable" in status.launchd_detail


# =============================================== synthetic: tri-state fail-closed
def test_an_undetermined_launchd_check_with_no_process_refuses(monkeypatch: pytest.MonkeyPatch):
    """The exact defect this fix closes: `launchctl` errors, no process is
    independently found either — the state genuinely cannot be determined,
    and that must refuse (`active`), not be silently treated as absent."""

    def _raise(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="launchctl", timeout=5.0)

    monkeypatch.setattr(legacy_guard.subprocess, "run", _raise)
    _no_legacy_processes(monkeypatch)

    status = legacy_system_status()

    assert status.launchd_state is LaunchdLabelState.UNKNOWN
    assert status.active is True
    assert status.undetermined is True


def test_an_undetermined_launchd_check_is_not_undetermined_if_a_process_is_found(
    monkeypatch: pytest.MonkeyPatch,
):
    """`undetermined` means "no positive evidence either way" — a detected
    process answers the question even while launchd's own signal is unknown,
    so this must still refuse, but as a genuine detection, not an unknown."""

    def _raise(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("no launchctl on this machine")

    monkeypatch.setattr(legacy_guard.subprocess, "run", _raise)
    fake = _FakeProcess(
        {"pid": 7, "exe": "/Volumes/Trading/Trading_Automation/bin/x", "cmdline": []}
    )
    monkeypatch.setattr(legacy_guard.psutil, "process_iter", lambda *a, **k: [fake])

    status = legacy_system_status()

    assert status.launchd_state is LaunchdLabelState.UNKNOWN
    assert status.active is True
    assert status.undetermined is False


# ======================================================= synthetic: process
def test_a_legacy_path_process_is_detected(monkeypatch: pytest.MonkeyPatch):
    _label_returncode(monkeypatch, 1)
    fake = _FakeProcess(
        {
            "pid": 4242,
            "exe": "/Volumes/Trading/Trading_Automation/.venv/bin/python",
            "cmdline": ["/Volumes/Trading/Trading_Automation/.venv/bin/python"],
        }
    )
    monkeypatch.setattr(legacy_guard.psutil, "process_iter", lambda *a, **k: [fake])

    status = legacy_system_status()

    assert status.process_running is True
    assert "4242" in status.process_detail


def test_a_legacy_path_only_in_cmdline_is_also_detected(monkeypatch: pytest.MonkeyPatch):
    """The executable may be a bare `.venv` python; the giveaway is an argv script path."""
    _label_returncode(monkeypatch, 1)
    fake = _FakeProcess(
        {
            "pid": 55,
            "exe": "/Volumes/Trading/Trading_Automation/.venv/bin/python",
            "cmdline": [
                "/Volumes/Trading/Trading_Automation/.venv/bin/python",
                "-m",
                "weekly_strategies",
            ],
        }
    )
    monkeypatch.setattr(legacy_guard.psutil, "process_iter", lambda *a, **k: [fake])

    status = legacy_system_status()

    assert status.process_running is True


def test_a_similarly_named_sibling_directory_does_not_match(monkeypatch: pytest.MonkeyPatch):
    """`/Volumes/Trading/Trading_AutomationX` must not match `Trading_Automation`."""
    _label_returncode(monkeypatch, 1)
    fake = _FakeProcess(
        {"pid": 1, "exe": "/Volumes/Trading/Trading_AutomationX/bin/foo", "cmdline": []}
    )
    monkeypatch.setattr(legacy_guard.psutil, "process_iter", lambda *a, **k: [fake])

    status = legacy_system_status()

    assert status.process_running is False


def test_this_repositorys_own_process_does_not_match(monkeypatch: pytest.MonkeyPatch):
    """The mount-root trap: algo_trading lives on the same /Volumes/Trading mount
    as the legacy system. A naive `/Volumes/Trading/` prefix would flag this
    repository's own processes as the legacy system it is trying to detect."""
    _label_returncode(monkeypatch, 1)
    fake = _FakeProcess(
        {
            "pid": 2,
            "exe": "/Volumes/Trading/algo_trading/.venv/bin/python",
            "cmdline": [
                "/Volumes/Trading/algo_trading/.venv/bin/python",
                "-m",
                "runtimes.intraday_options",
            ],
        }
    )
    monkeypatch.setattr(legacy_guard.psutil, "process_iter", lambda *a, **k: [fake])

    status = legacy_system_status()

    assert status.process_running is False


def test_a_dead_or_inaccessible_process_is_skipped_not_raised(monkeypatch: pytest.MonkeyPatch):
    _label_returncode(monkeypatch, 1)
    monkeypatch.setattr(legacy_guard.psutil, "process_iter", lambda *a, **k: [_UnreadableProcess()])

    status = legacy_system_status()

    assert status.process_running is False


def test_a_clean_machine_reports_no_conflict(monkeypatch: pytest.MonkeyPatch):
    _label_returncode(monkeypatch, 1)
    _no_legacy_processes(monkeypatch)

    status = legacy_system_status()

    assert status.active is False
    assert status.describe() == "not detected"


def test_describe_reports_both_signals_when_both_fire(monkeypatch: pytest.MonkeyPatch):
    _label_returncode(monkeypatch, 0)
    fake = _FakeProcess(
        {"pid": 9, "exe": "/Volumes/Trading/Trading_Automation/bin/x", "cmdline": []}
    )
    monkeypatch.setattr(legacy_guard.psutil, "process_iter", lambda *a, **k: [fake])

    status = legacy_system_status()

    assert "launchd:" in status.describe()
    assert "process:" in status.describe()


# ===================================================== real machine, gated
def _skip_unless_legacy_plist_installed() -> None:
    if not LEGACY_LAUNCHD_PLIST_PATH.is_file():
        pytest.skip(f"legacy plist not installed on this machine ({LEGACY_LAUNCHD_PLIST_PATH})")


def test_the_configured_label_matches_the_real_installed_plist():
    """The check that matters most before any algo_trading plist ever loads.

    Reads the *actual* installed legacy plist on this Mac (not a synthetic
    stand-in) and asserts LEGACY_LAUNCHD_LABEL equals its real ``Label`` byte
    for byte. A future rename or edit of the legacy agent breaks this test
    loudly, rather than leaving the guard silently matching nothing.
    """
    _skip_unless_legacy_plist_installed()
    with LEGACY_LAUNCHD_PLIST_PATH.open("rb") as handle:
        data = plistlib.load(handle)

    assert data["Label"] == LEGACY_LAUNCHD_LABEL


def test_legacy_system_status_runs_cleanly_against_the_real_machine():
    """No mocked launchctl/psutil — proves this executes and returns a
    well-formed result. Deliberately does not assert loaded vs. not-loaded:
    that is this machine's real, mutable operator state (whether the legacy
    agent has been unloaded yet), not something this suite should pin."""
    _skip_unless_legacy_plist_installed()
    status = legacy_system_status()

    assert isinstance(status, LegacySystemStatus)
    assert isinstance(status.active, bool)
    assert status.launchd_detail
    assert status.process_detail
