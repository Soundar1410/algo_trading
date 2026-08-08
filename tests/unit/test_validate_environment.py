"""scripts.validate_environment: the ``_check_legacy_system`` gate.

Isolated from the rest of ``main()`` — no project root, no writable-dir,
credential or database checks are exercised here. This file exists
specifically to prove the fail-closed fix at this call site: an undetermined
``legacy_system_status()`` result (``launchctl`` unavailable/errored/timed
out, no independently-detected process either) must become its own distinct
problem, never the "OK: not detected" line that only a confirmed absence
earns. Before this fix, ``_check_legacy_system`` collapsed "could not
determine" into the same boolean as "confirmed not loaded" and printed "OK".
"""

from __future__ import annotations

import pytest

from common.process.legacy_guard import LaunchdLabelState, LegacySystemStatus
from scripts import validate_environment as ve

_INACTIVE = LegacySystemStatus(
    launchd_state=LaunchdLabelState.INACTIVE,
    launchd_detail="label is not loaded",
    process_running=False,
    process_detail="no live process found",
)

_ACTIVE = LegacySystemStatus(
    launchd_state=LaunchdLabelState.ACTIVE,
    launchd_detail="label is loaded",
    process_running=False,
    process_detail="no live process found",
)

_UNDETERMINED = LegacySystemStatus(
    launchd_state=LaunchdLabelState.UNKNOWN,
    launchd_detail="launchctl unavailable (FileNotFoundError('no launchctl'))",
    process_running=False,
    process_detail="no live process found",
)


def test_a_confirmed_inactive_legacy_system_prints_ok(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(ve, "legacy_system_status", lambda: _INACTIVE)

    problems: list[str] = []
    ve._check_legacy_system(problems)

    assert problems == []
    assert "OK: legacy Trading_Automation system not detected" in capsys.readouterr().out


def test_a_confirmed_active_legacy_system_is_a_problem_naming_the_unload_command(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(ve, "legacy_system_status", lambda: _ACTIVE)

    problems: list[str] = []
    ve._check_legacy_system(problems)

    assert len(problems) == 1
    assert "appears active" in problems[0]
    assert "launchctl bootout" in problems[0]


def test_an_undetermined_legacy_check_is_its_own_problem_never_printed_as_ok(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """The fail-first case: run against unmodified code and this fails —
    ``problems`` was empty and "OK: not detected" was printed instead,
    because the collapsed boolean made an unresolved launchctl call
    indistinguishable from a confirmed absence."""
    monkeypatch.setattr(ve, "legacy_system_status", lambda: _UNDETERMINED)

    problems: list[str] = []
    ve._check_legacy_system(problems)

    assert len(problems) == 1
    assert "could not be determined" in problems[0]
    # Never the "OK" line a confirmed absence earns, and never told to
    # "unload" something that was never actually confirmed to be there.
    assert "OK: legacy Trading_Automation system not detected" not in capsys.readouterr().out
    assert "unload it first" not in problems[0]
