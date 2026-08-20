"""scripts.install_launch_agents: dry-run by default, and refuses when unsafe.

Nothing in this file runs launchctl. The tests assert what the script *would*
do, which is exactly the property the script itself is built around: loading a
LaunchAgent turns committed files into a Mac that trades by itself, so it must
be an explicit decision, not a side effect of running something called
"install".
"""

from __future__ import annotations

from pathlib import Path

import pytest

import scripts.install_launch_agents as ila

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture(autouse=True)
def _never_run_a_command(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Any subprocess this script starts is recorded, never executed.

    plutil is the one exception the validate path legitimately shells out to,
    so it is stubbed to succeed rather than blocked.
    """
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(command, **kwargs):
        calls.append(list(command))
        return _Result()

    monkeypatch.setattr(ila.subprocess, "run", _fake_run)
    return calls


@pytest.fixture(autouse=True)
def _no_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Clear:
        active = False
        undetermined = False

        def describe(self) -> str:
            return "not detected"

    monkeypatch.setattr(ila, "legacy_system_status", lambda: _Clear())


def _args(**overrides):
    import argparse

    namespace = argparse.Namespace(
        config_root=REPO_CONFIG, execute=False, handler=None
    )
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


# ------------------------------------------------------------------- dry run
def test_install_executes_nothing_without_the_execute_flag(
    capsys: pytest.CaptureFixture, _never_run_a_command
):
    assert ila.cmd_install(_args()) == ila.EXIT_OK
    output = capsys.readouterr().out

    assert "dry run" in output
    assert "launchctl bootstrap" in output
    launchctl_calls = [c for c in _never_run_a_command if "launchctl" in " ".join(c)]
    assert launchctl_calls == [], "nothing may be loaded without --execute"


def test_uninstall_executes_nothing_without_the_execute_flag(
    capsys: pytest.CaptureFixture, _never_run_a_command
):
    assert ila.cmd_uninstall(_args()) == ila.EXIT_OK
    output = capsys.readouterr().out

    assert "bootout" in output
    assert "dry run" in output
    assert [c for c in _never_run_a_command if "launchctl" in " ".join(c)] == []


def test_the_installer_creates_the_boot_volume_launchd_log_directory(
    capsys: pytest.CaptureFixture,
):
    """launchd opens StandardOutPath/StandardErrorPath while setting the job
    up. A missing directory there fails the job before its wait loop runs —
    the exact failure the boot-volume paths exist to remove."""
    from orchestration.launchd.generate_plists import boot_log_root

    ila.cmd_install(_args())
    output = capsys.readouterr().out

    log_root = boot_log_root()
    assert f"/bin/mkdir -p {log_root}" in output
    assert not str(log_root).startswith("/Volumes/")
    # It must be created before anything is bootstrapped.
    assert output.index(f"mkdir -p {log_root}") < output.index("launchctl bootstrap")


def test_the_installer_creates_the_log_directory_for_real_when_executed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The mkdir is a real command in the list, not just printed prose."""
    from orchestration.launchd.generate_plists import boot_log_root

    executed: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def _record(command, **kwargs):
        executed.append(list(command))
        return _Result()

    monkeypatch.setattr(ila.subprocess, "run", _record)
    ila.cmd_install(_args(execute=True))

    mkdirs = [c for c in executed if c[:2] == ["/bin/mkdir", "-p"]]
    assert [str(boot_log_root())] in [c[2:] for c in mkdirs]


def test_the_printed_commands_cover_every_committed_agent(capsys: pytest.CaptureFixture):
    ila.cmd_install(_args())
    output = capsys.readouterr().out
    for spec in ila.PLIST_SPECS:
        assert spec.label in output


def test_rollback_disables_boots_out_and_removes(capsys: pytest.CaptureFixture):
    ila.cmd_uninstall(_args())
    output = capsys.readouterr().out
    for verb in ("disable", "bootout", "/bin/rm"):
        assert verb in output


# ------------------------------------------------------------------- refusals
def test_install_refuses_while_the_legacy_agent_is_loaded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, _never_run_a_command
):
    class _Legacy:
        active = True
        undetermined = False

        def describe(self) -> str:
            return "launchd: loaded"

    monkeypatch.setattr(ila, "legacy_system_status", lambda: _Legacy())

    assert ila.cmd_install(_args()) == ila.EXIT_REFUSED
    output = capsys.readouterr().out

    assert "Refusing to install" in output
    assert "launchctl bootout gui/$UID/com.soundarraj.tradingautomation.starttrading" in output
    assert "bootstrap" not in output, "no install command may be offered while refusing"


def test_install_refuses_when_the_legacy_state_cannot_be_determined(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    class _Legacy:
        active = True
        undetermined = True

        def describe(self) -> str:
            return "unknown"

    monkeypatch.setattr(ila, "legacy_system_status", lambda: _Legacy())

    assert ila.cmd_install(_args()) == ila.EXIT_REFUSED
    assert "could not be determined" in capsys.readouterr().out


def test_install_refuses_on_a_system_timezone_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    monkeypatch.setattr(ila, "system_timezone_matches", lambda tz: False)
    monkeypatch.setattr(ila, "system_timezone_name", lambda: "America/New_York")

    assert ila.cmd_install(_args()) == ila.EXIT_REFUSED
    output = capsys.readouterr().out
    assert "timezone" in output
    assert "America/New_York" in output


# ------------------------------------------------------------------- validate
def test_validate_accepts_the_committed_plists(capsys: pytest.CaptureFixture):
    assert ila.cmd_validate(_args()) == ila.EXIT_OK
    assert "plist(s) valid" in capsys.readouterr().out


def test_validate_is_read_only(_never_run_a_command):
    ila.cmd_validate(_args())
    for command in _never_run_a_command:
        joined = " ".join(command)
        assert "launchctl" not in joined
        assert "cp" not in joined


def test_logs_names_the_launchd_and_controller_logs(capsys: pytest.CaptureFixture):
    assert ila.cmd_logs(_args()) == ila.EXIT_OK
    output = capsys.readouterr().out
    assert "autostart.err.log" in output
    assert "auto_start.log" in output


def test_install_notes_that_loading_is_not_enabling(capsys: pytest.CaptureFixture):
    """The two gates must not be confused for one another."""
    ila.cmd_install(_args())
    output = capsys.readouterr().out
    assert "auto_start.enabled" in output
    assert "does NOT start trading" in output


def test_no_command_line_carries_a_secret(capsys: pytest.CaptureFixture):
    ila.cmd_install(_args())
    ila.cmd_uninstall(_args())
    ila.cmd_status(_args())
    output = capsys.readouterr().out
    for token in ("DHAN_PIN", "TOTP", "access_token", "BOT_TOKEN", "TELEGRAM"):
        assert token not in output
