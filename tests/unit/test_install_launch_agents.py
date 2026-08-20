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


class _Result:
    """A scripted `subprocess.CompletedProcess` stand-in."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Launchctl:
    """Records every command and answers from a scripted table.

    No real `launchctl`, `cp`, `mkdir` or `rm` ever runs: the whole point is to
    drive the installer through outcomes a real Mac would produce (a first
    install where nothing is loaded, a reinstall where something is, a
    permission failure) without touching the machine.
    """

    #: What a real `launchctl bootout` says for a label that is not loaded.
    NOT_LOADED = _Result(3, "", "Boot-out failed: 3: No such process")
    #: The other authoritative form, from a different launchd release.
    NO_SUCH_SERVICE = _Result(113, "", "Could not find specified service")

    def __init__(self, responses: dict[str, _Result] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._responses = responses or {}

    def __call__(self, command, **kwargs):
        command = list(command)
        self.calls.append(command)
        for pattern, result in self._responses.items():
            if pattern in " ".join(command):
                return result
        return _Result()

    def launchctl_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "/bin/launchctl"]


@pytest.fixture(autouse=True)
def _never_run_a_command(monkeypatch: pytest.MonkeyPatch) -> _Launchctl:
    """Any subprocess this script starts is recorded, never executed."""
    runner = _Launchctl()
    monkeypatch.setattr(ila.subprocess, "run", runner)
    return runner


def _script(monkeypatch: pytest.MonkeyPatch, responses: dict[str, _Result]) -> _Launchctl:
    runner = _Launchctl(responses)
    monkeypatch.setattr(ila.subprocess, "run", runner)
    return runner


def _verbs(runner: _Launchctl) -> list[str]:
    """The launchctl subcommands issued, in order: ['bootout', 'enable', ...]."""
    return [c[1] for c in runner.launchctl_calls()]


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
    launchctl_calls = [c for c in _never_run_a_command.calls if "launchctl" in " ".join(c)]
    assert launchctl_calls == [], "nothing may be loaded without --execute"


def test_uninstall_executes_nothing_without_the_execute_flag(
    capsys: pytest.CaptureFixture, _never_run_a_command
):
    assert ila.cmd_uninstall(_args()) == ila.EXIT_OK
    output = capsys.readouterr().out

    assert "bootout" in output
    assert "dry run" in output
    assert [c for c in _never_run_a_command.calls if "launchctl" in " ".join(c)] == []


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
    for command in _never_run_a_command.calls:
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


# ==================================================== install lifecycle (scripted)
AUTOSTART = "com.soundarraj.algotrading.autostart"


def test_a_first_time_install_succeeds_when_nothing_is_loaded(
    monkeypatch: pytest.MonkeyPatch,
):
    """The defect, stated as a test.

    On a genuine first install every `bootout` returns non-zero — the label was
    never loaded. Treating that as fatal stopped the run *before* `bootstrap`,
    so `install --execute` silently installed nothing.

    The probe now answers "not loaded" authoritatively, so bootout is skipped
    outright rather than attempted-and-forgiven.
    """
    runner = _script(
        monkeypatch,
        {"launchctl print": _Launchctl.NOT_LOADED, "launchctl bootout": _Launchctl.NOT_LOADED},
    )

    assert ila.cmd_install(_args(execute=True)) == ila.EXIT_OK

    verbs = _verbs(runner)
    assert "bootstrap" in verbs, "installation must reach bootstrap on a first install"
    assert verbs.count("bootstrap") == len(ila.PLIST_SPECS)
    assert "bootout" not in verbs, "nothing to boot out when the probe says so"


def test_a_bootout_reporting_not_loaded_does_not_abort_the_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """The other half of the fix: the tolerance path itself.

    When the probe cannot tell (or the service exits between the probe and the
    bootout), bootout runs and reports "not loaded". That is an expected
    outcome, and installation must continue to bootstrap.
    """
    runner = _script(
        monkeypatch,
        {
            "launchctl print": _Result(1, "", "Operation not permitted"),  # unreadable
            "launchctl bootout": _Launchctl.NOT_LOADED,
        },
    )

    assert ila.cmd_install(_args(execute=True)) == ila.EXIT_OK

    verbs = _verbs(runner)
    assert "bootout" in verbs, "an unreadable state must still attempt a bootout"
    assert verbs.index("bootout") < verbs.index("bootstrap")
    assert "not loaded (expected)" in capsys.readouterr().out


def test_the_alternative_not_found_message_is_also_tolerated(monkeypatch: pytest.MonkeyPatch):
    """launchctl's codes have moved between releases; both forms are handled."""
    runner = _script(
        monkeypatch,
        {
            "launchctl print": _Launchctl.NO_SUCH_SERVICE,
            "launchctl bootout": _Launchctl.NO_SUCH_SERVICE,
        },
    )

    assert ila.cmd_install(_args(execute=True)) == ila.EXIT_OK
    assert "bootstrap" in _verbs(runner)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(_Result(1, "", "Operation not permitted"), id="permission-denied"),
        pytest.param(_Result(9, "", "Bad request"), id="malformed-domain"),
        pytest.param(_Result(5, "", "Input/output error"), id="io-error"),
    ],
)
def test_an_unexpected_bootout_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch, failure: _Result
):
    """Only the authoritative 'not loaded' result is harmless. Everything else
    means we could not tell, which is not the same as 'it was not there'."""
    runner = _script(
        monkeypatch,
        {"launchctl print": _Result(0), "launchctl bootout": failure},
    )

    assert ila.cmd_install(_args(execute=True)) == ila.EXIT_FAILED
    assert "bootstrap" not in _verbs(runner), "must not load after an unexplained failure"


def test_an_unexpected_bootstrap_failure_fails_closed(monkeypatch: pytest.MonkeyPatch):
    _script(
        monkeypatch,
        {
            "launchctl print": _Launchctl.NOT_LOADED,
            "launchctl bootout": _Launchctl.NOT_LOADED,
            "launchctl bootstrap": _Result(1, "", "Operation not permitted"),
        },
    )
    assert ila.cmd_install(_args(execute=True)) == ila.EXIT_FAILED


def test_enable_occurs_before_bootstrap(monkeypatch: pytest.MonkeyPatch):
    """uninstall runs `disable`, and a disabled label cannot be bootstrapped.
    Re-enabling first is what makes reinstall-after-uninstall work at all."""
    runner = _script(
        monkeypatch,
        {"launchctl print": _Launchctl.NOT_LOADED, "launchctl bootout": _Launchctl.NOT_LOADED},
    )
    ila.cmd_install(_args(execute=True))

    verbs = _verbs(runner)
    assert verbs.index("enable") < verbs.index("bootstrap")


def test_reinstallation_after_a_previous_disable_succeeds(monkeypatch: pytest.MonkeyPatch):
    """The post-uninstall state: plist gone, label disabled, nothing loaded."""
    runner = _script(
        monkeypatch,
        {"launchctl print": _Launchctl.NOT_LOADED, "launchctl bootout": _Launchctl.NOT_LOADED},
    )

    assert ila.cmd_install(_args(execute=True)) == ila.EXIT_OK
    verbs = _verbs(runner)
    assert verbs.count("enable") == len(ila.PLIST_SPECS)
    assert verbs.count("bootstrap") == len(ila.PLIST_SPECS)


def test_reinstalling_a_loaded_agent_boots_it_out_and_reloads_it_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
):
    runner = _script(monkeypatch, {"launchctl print": _Result(0)})  # everything is loaded

    assert ila.cmd_install(_args(execute=True)) == ila.EXIT_OK

    verbs = _verbs(runner)
    assert verbs.count("bootout") == len(ila.PLIST_SPECS)
    assert verbs.count("bootstrap") == len(ila.PLIST_SPECS)
    per_label = [c for c in runner.launchctl_calls() if AUTOSTART in " ".join(c)]
    assert [c[1] for c in per_label].count("bootstrap") == 1, "exactly one reload"


def test_a_label_whose_state_cannot_be_read_is_still_booted_out(
    monkeypatch: pytest.MonkeyPatch,
):
    """`None` is not `False`. Skipping bootout on an unreadable answer would
    risk a duplicate load."""
    runner = _script(
        monkeypatch,
        {"launchctl print": _Result(1, "", "Operation not permitted")},
    )
    ila.cmd_install(_args(execute=True))
    assert "bootout" in _verbs(runner)


def test_label_is_loaded_distinguishes_all_three_answers(monkeypatch: pytest.MonkeyPatch):
    _script(monkeypatch, {"launchctl print": _Result(0)})
    assert ila.label_is_loaded(AUTOSTART) is True

    _script(monkeypatch, {"launchctl print": _Launchctl.NOT_LOADED})
    assert ila.label_is_loaded(AUTOSTART) is False

    _script(monkeypatch, {"launchctl print": _Result(1, "", "Operation not permitted")})
    assert ila.label_is_loaded(AUTOSTART) is None


def test_a_dry_run_install_probes_nothing_at_all(_never_run_a_command):
    """Dry run must not even ask launchctl what is loaded."""
    ila.cmd_install(_args())
    assert _never_run_a_command.launchctl_calls() == []


# ================================================== uninstall lifecycle (scripted)
def test_uninstalling_an_absent_agent_still_removes_its_plist(
    monkeypatch: pytest.MonkeyPatch,
):
    """A stale plist left behind because bootout said 'not loaded' would
    silently reinstall itself at the next login."""
    runner = _script(
        monkeypatch,
        {
            "launchctl disable": _Launchctl.NOT_LOADED,
            "launchctl bootout": _Launchctl.NOT_LOADED,
        },
    )

    assert ila.cmd_uninstall(_args(execute=True)) == ila.EXIT_OK

    removals = [c for c in runner.calls if c[:2] == ["/bin/rm", "-f"]]
    assert len(removals) == len(ila.PLIST_SPECS)


def test_uninstall_removes_the_plist_last_and_unconditionally():
    steps = ila.uninstall_steps()
    for spec in ila.PLIST_SPECS:
        label_steps = [
            s for s in steps
            if spec.label in " ".join(s.command) and s.command[0] == "/bin/launchctl"
        ]
        removal = [s for s in steps if spec.filename in " ".join(s.command)]
        assert removal, f"{spec.filename} is never removed"
        assert removal[0].command[0] == "/bin/rm"
        assert steps.index(removal[0]) > steps.index(label_steps[-1])
        assert not removal[0].tolerate_not_loaded


def test_an_unexpected_uninstall_failure_is_reported_honestly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    _script(monkeypatch, {"launchctl disable": _Result(1, "", "Operation not permitted")})

    assert ila.cmd_uninstall(_args(execute=True)) == ila.EXIT_FAILED
    output = capsys.readouterr().out
    assert "FAILED" in output
    assert "Operation not permitted" in output


def test_uninstall_tolerates_a_not_loaded_disable_and_continues(
    monkeypatch: pytest.MonkeyPatch,
):
    runner = _script(monkeypatch, {"launchctl disable": _Launchctl.NOT_LOADED})
    assert ila.cmd_uninstall(_args(execute=True)) == ila.EXIT_OK
    assert "bootout" in _verbs(runner)


# ============================================================ log paths (defect 3)
def test_cmd_logs_prints_the_boot_log_root_for_both_agents(capsys: pytest.CaptureFixture):
    """The plists write launchd streams to the boot volume; the command used to
    print the old project-relative paths, which no longer exist."""
    from orchestration.launchd.generate_plists import boot_log_root

    assert ila.cmd_logs(_args()) == ila.EXIT_OK
    output = capsys.readouterr().out

    for spec in ila.PLIST_SPECS:
        for stream in ("out", "err"):
            expected = boot_log_root() / f"{spec.short_name}.{stream}.log"
            assert str(expected) in output, f"{expected} not offered"

    assert "/Volumes/Trading/algo_trading/logs/launchd" not in output, "stale path still printed"


def test_cmd_logs_still_prints_the_controller_application_log(
    capsys: pytest.CaptureFixture,
):
    """That one genuinely is under the project log root — the controller writes
    it after the volume is up."""
    from common.config.paths import resolve_project_root

    ila.cmd_logs(_args())
    output = capsys.readouterr().out
    assert str(resolve_project_root() / "logs" / "auto_start.log") in output


def test_cmd_logs_runs_no_command(_never_run_a_command):
    ila.cmd_logs(_args())
    assert _never_run_a_command.calls == []


# ================================================================== non-mutation
def test_a_dry_run_of_every_command_mutates_nothing(_never_run_a_command):
    """Every command goes through the patched runner, so nothing reaches the
    machine. The only subprocess a dry run may make is `plutil -lint`, which
    reads a file and changes nothing."""
    ila.cmd_install(_args())
    ila.cmd_uninstall(_args())
    ila.cmd_status(_args())
    ila.cmd_logs(_args())

    for command in _never_run_a_command.calls:
        assert command[0] == "/usr/bin/plutil", f"a dry run ran {command}"
        assert "-lint" in command
    assert _never_run_a_command.launchctl_calls() == []


def test_every_command_in_every_plan_is_an_absolute_path():
    plans = ila.install_steps(source=ila._source_dir()) + ila.uninstall_steps()
    for step in plans:
        assert step.command[0].startswith("/"), step.command
