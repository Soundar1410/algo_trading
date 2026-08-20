"""The generated ``/bin/sh`` wrapper, executed for real against a fake volume.

Everything here runs the *actual* generated script through ``/bin/sh``, with
two substitutions that make it deterministic and harmless:

* the "interpreter" is a temp-directory path a test creates when it wants the
  "volume" to appear, and the exec target is ``/bin/echo`` — no Python, no
  runtime, no controller, nothing started;
* ``/bin/date`` is shadowed by a stub earlier on ``PATH``... except it cannot
  be, because the script calls it by absolute path. So the wrapper is
  generated with a substituted date binary instead, which is the same string
  substitution the generator itself performs.

That matters: asserting on the script's *text* proves what was written, but
only running it proves the boundary arithmetic is right. The 07:00-must-not-
expire-at-13:15 case is precisely the kind of bug text assertions miss.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from common.config import ProjectPaths
from orchestration.launchd.generate_plists import (
    DASHBOARD_WAIT_SECONDS,
    MOUNT_POLL_SECONDS,
    PLIST_SPECS,
    build_plist,
)

PROJECT_ROOT = Path("/Volumes/Trading/algo_trading")


def _spec(short_name: str):
    return next(spec for spec in PLIST_SPECS if spec.short_name == short_name)


def _script(short_name: str) -> str:
    document = build_plist(_spec(short_name), ProjectPaths(project_root=PROJECT_ROOT))
    return document["ProgramArguments"][2]


def _make_stub(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _runnable(
    short_name: str,
    tmp_path: Path,
    *,
    now_hhmm: str,
    volume_path: Path,
    appears_after: int = 0,
) -> str:
    """The real generated script, retargeted at a fake volume and clock.

    ``appears_after`` is the number of ``sleep`` calls after which the fake
    volume "mounts" — the stub sleep creates the target file, so the loop
    exits exactly as it would when the drive really appeared.
    """
    date_stub = _make_stub(tmp_path / "date", f'echo "{now_hhmm}"')
    counter = tmp_path / "sleeps"
    sleep_stub = _make_stub(
        tmp_path / "sleep",
        f'echo x >> "{counter}"\n'
        f'n=$(wc -l < "{counter}" | tr -d " ")\n'
        f'if [ "$n" -ge {appears_after} ] && [ {appears_after} -gt 0 ]; then\n'
        f'  : > "{volume_path}"; chmod +x "{volume_path}"\n'
        f"fi",
    )

    script = _script(short_name)
    script = script.replace("/bin/date", str(date_stub))
    script = script.replace("/bin/sleep", str(sleep_stub))
    script = script.replace(str(PROJECT_ROOT / ".venv" / "bin" / "python"), str(volume_path))
    # Never exec a real interpreter, a real caffeinate, or the project root.
    script = script.replace("'/usr/bin/caffeinate' '-i' '-s' ", "")
    script = script.replace(f"cd '{PROJECT_ROOT}'", f"cd '{tmp_path}'")
    script = script.replace(
        f"'{volume_path}' '-m' 'orchestration.auto_start'", "/bin/echo STARTED-CONTROLLER"
    )
    script = script.replace(
        f"'{volume_path}' '-m' 'scripts.start_dashboard'", "/bin/echo STARTED-DASHBOARD"
    )
    return script


def _run(script: str, *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", "-c", script], capture_output=True, text=True, timeout=timeout
    )


def _sleep_count(tmp_path: Path) -> int:
    counter = tmp_path / "sleeps"
    return len(counter.read_text().splitlines()) if counter.is_file() else 0


# ============================================================ syntax and shape
@pytest.mark.parametrize("spec", PLIST_SPECS, ids=lambda s: s.short_name)
def test_sh_n_accepts_every_generated_wrapper(spec):
    """`sh -n` parses and executes nothing."""
    result = subprocess.run(
        ["/bin/sh", "-n", "-c", _script(spec.short_name)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("spec", PLIST_SPECS, ids=lambda s: s.short_name)
def test_every_pre_mount_executable_is_on_the_boot_volume(spec):
    """Whatever the wrapper runs *before* the volume appears must already exist."""
    script = _script(spec.short_name)
    prologue = script.split("done;")[0]
    for binary in ("/bin/date", "/bin/sleep"):
        if binary in prologue:
            assert Path(binary).exists()
            assert not binary.startswith("/Volumes/")


# ===================================================== the trading controller
def test_a_0700_invocation_does_not_expire_at_1315(tmp_path: Path):
    """The boundary bug, stated as a test.

    Under the old elapsed budget (``deadline - startup`` = 6h15m added to the
    invocation time) a 07:00 start expired at **13:15** — and while that shell
    was still waiting, launchd would not start a second copy for the 09:00
    StartCalendarInterval event, so the day was lost by the very mechanism
    meant to save it.

    Driving the clock to 13:15 is the decisive check: an elapsed
    implementation gives up here, an absolute one carries on to 15:15.
    """
    volume = tmp_path / "python"
    script = _runnable(
        "autostart", tmp_path, now_hhmm="1315", volume_path=volume, appears_after=2
    )
    result = _run(script)

    assert result.returncode == 0, result.stderr
    assert "did not appear before" not in result.stderr
    assert "STARTED-CONTROLLER" in result.stdout


def test_an_early_invocation_keeps_waiting_rather_than_giving_up(tmp_path: Path):
    """07:00 is simply 'before 15:15'; the wait continues across 09:00."""
    volume = tmp_path / "python"
    script = _runnable(
        "autostart", tmp_path, now_hhmm="0700", volume_path=volume, appears_after=4
    )
    result = _run(script)

    assert result.returncode == 0, result.stderr
    assert "STARTED-CONTROLLER" in result.stdout
    assert _sleep_count(tmp_path) >= 4, "it must genuinely have kept polling"


def test_a_volume_appearing_at_0906_starts_the_controller(tmp_path: Path):
    volume = tmp_path / "python"
    script = _runnable(
        "autostart", tmp_path, now_hhmm="0906", volume_path=volume, appears_after=2
    )
    result = _run(script)

    assert result.returncode == 0, result.stderr
    assert "STARTED-CONTROLLER" in result.stdout


def test_a_volume_appearing_at_1430_still_starts_the_controller(tmp_path: Path):
    """Late, but still before the boundary — the day is not lost."""
    volume = tmp_path / "python"
    script = _runnable(
        "autostart", tmp_path, now_hhmm="1430", volume_path=volume, appears_after=3
    )
    result = _run(script)

    assert result.returncode == 0, result.stderr
    assert "STARTED-CONTROLLER" in result.stdout


def test_a_volume_appearing_at_1200_still_starts_the_controller(tmp_path: Path):
    volume = tmp_path / "python"
    script = _runnable(
        "autostart", tmp_path, now_hhmm="1200", volume_path=volume, appears_after=1
    )
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert "STARTED-CONTROLLER" in result.stdout


@pytest.mark.parametrize("now", ["1515", "1516", "1800", "2300"])
def test_no_start_at_or_after_the_deadline(tmp_path: Path, now: str):
    volume = tmp_path / "python"
    script = _runnable("autostart", tmp_path, now_hhmm=now, volume_path=volume)
    result = _run(script)

    assert result.returncode == 75
    assert "did not appear before the 15:15 session deadline" in result.stderr
    assert "STARTED-CONTROLLER" not in result.stdout
    assert _sleep_count(tmp_path) == 0, "past the boundary it must not even wait"


def test_a_pre_0900_volume_appearance_execs_python_and_lets_the_gate_decide(tmp_path: Path):
    """The wrapper has no opinion about 09:00 — it only knows the deadline.

    A volume present at 08:30 means Python runs, and the *Python* early gate
    exits 0 and defers to the 09:00 calendar trigger. Keeping that decision in
    one place is why the shell does not second-guess it.
    """
    volume = tmp_path / "python"
    volume.write_text("")
    volume.chmod(0o755)
    script = _runnable("autostart", tmp_path, now_hhmm="0830", volume_path=volume)
    result = _run(script)

    assert result.returncode == 0
    assert "STARTED-CONTROLLER" in result.stdout
    assert _sleep_count(tmp_path) == 0, "a mounted volume must not wait at all"

    script_text = _script("autostart")
    assert "0900" not in script_text and "startup_time" not in script_text


def test_the_0900_calendar_trigger_cannot_be_lost_to_an_elapsed_deadline():
    """No elapsed arithmetic survives in the trading wrapper.

    An earlier shell that expired mid-morning would block the 09:00 trigger,
    because launchd will not start a second copy of a job already running.
    """
    script = _script("autostart")
    assert "n=0" not in script
    assert "n=$((n+1))" not in script
    assert "-gt" not in script, "an attempt counter would be elapsed-based"
    assert "-ge 11515" in script


def test_the_wrapper_enters_the_project_root_before_exec(tmp_path: Path):
    """Proven by running it: the exec'd command must see the project root."""
    volume = tmp_path / "python"
    volume.write_text("")
    volume.chmod(0o755)
    script = _runnable("autostart", tmp_path, now_hhmm="0930", volume_path=volume)
    script = script.replace("/bin/echo STARTED-CONTROLLER", "/bin/pwd")
    result = _run(script)

    assert result.returncode == 0
    assert result.stdout.strip() == str(tmp_path.resolve())


def test_a_missing_project_root_after_mount_fails_loudly(tmp_path: Path):
    volume = tmp_path / "python"
    volume.write_text("")
    volume.chmod(0o755)
    script = _runnable("autostart", tmp_path, now_hhmm="0930", volume_path=volume)
    script = script.replace(f"cd '{tmp_path}'", "cd '/nonexistent-project-root'")
    result = _run(script)

    assert result.returncode == 75
    assert "cannot enter" in result.stderr


# ================================================================ the dashboard
def test_the_dashboard_wait_is_independent_of_the_trading_deadline():
    """It has no calendar trigger and no session; a 15:15 boundary would stop
    a Saturday dashboard from ever starting."""
    script = _script("dashboard")
    assert "-ge 11515" not in script
    assert "session deadline" not in script
    assert "/bin/date" not in script, "the dashboard must not consult the wall clock at all"


@pytest.mark.parametrize("now", ["0300", "1515", "1900", "2359"])
def test_the_dashboard_starts_at_any_hour_including_past_1515(tmp_path: Path, now: str):
    volume = tmp_path / "python"
    volume.write_text("")
    volume.chmod(0o755)
    script = _runnable("dashboard", tmp_path, now_hhmm=now, volume_path=volume)
    result = _run(script)

    assert result.returncode == 0, result.stderr
    assert "STARTED-DASHBOARD" in result.stdout


def test_the_dashboard_waits_for_a_late_mount_then_starts(tmp_path: Path):
    volume = tmp_path / "python"
    script = _runnable(
        "dashboard", tmp_path, now_hhmm="1900", volume_path=volume, appears_after=3
    )
    result = _run(script)

    assert result.returncode == 0, result.stderr
    assert "STARTED-DASHBOARD" in result.stdout
    assert _sleep_count(tmp_path) >= 3


def test_the_dashboard_budget_is_bounded_and_documented():
    script = _script("dashboard")
    attempts = DASHBOARD_WAIT_SECONDS // MOUNT_POLL_SECONDS
    assert f"-gt {attempts}" in script
    assert str(DASHBOARD_WAIT_SECONDS) in script
    assert "Log in again to retry" in script, "give-up must say what to do next"


def test_the_dashboard_gives_up_after_its_budget(tmp_path: Path):
    volume = tmp_path / "python"
    script = _runnable("dashboard", tmp_path, now_hhmm="1200", volume_path=volume)
    # Shrink the budget so the give-up path is reachable in a test.
    attempts = DASHBOARD_WAIT_SECONDS // MOUNT_POLL_SECONDS
    script = script.replace(f'-gt {attempts} ]', '-gt 2 ]')
    result = _run(script)

    assert result.returncode == 75
    assert "did not appear within" in result.stderr
    assert "STARTED-DASHBOARD" not in result.stdout


def test_the_dashboard_wrapper_also_enters_the_project_root(tmp_path: Path):
    volume = tmp_path / "python"
    volume.write_text("")
    volume.chmod(0o755)
    script = _runnable("dashboard", tmp_path, now_hhmm="1000", volume_path=volume)
    script = script.replace("/bin/echo STARTED-DASHBOARD", "/bin/pwd")
    result = _run(script)

    assert result.returncode == 0
    assert result.stdout.strip() == str(tmp_path.resolve())
