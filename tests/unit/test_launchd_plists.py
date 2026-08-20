"""orchestration.launchd: the generator and its committed output.

The committed ``.plist`` files are generated output, never hand-edited (see
``generate_plists.py``'s own docstring) — so the drift guard
(``test_the_committed_plists_match_a_fresh_generation``) is the test that
actually matters here: everything else checks the generator's *rules*, which
only matter if the committed files still obey them.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path

import pytest

from orchestration.launchd.generate_plists import LABEL_PREFIX, PLIST_SPECS, generate_all

LAUNCHD_DIR = Path(__file__).resolve().parents[2] / "orchestration" / "launchd"
COMMITTED_PROJECT_ROOT = Path("/Volumes/Trading/algo_trading")


def _committed_plists() -> dict[str, dict[str, object]]:
    documents = {}
    for spec in PLIST_SPECS:
        path = LAUNCHD_DIR / spec.filename
        with path.open("rb") as handle:
            documents[spec.filename] = plistlib.load(handle)
    return documents


def test_the_committed_plists_match_a_fresh_generation():
    """The drift guard: what's on disk is exactly what the generator would
    write today. Byte-for-byte, the same check `generate_plists.py --check`
    performs."""
    # Committed LaunchAgents intentionally target the operator's stable Mac
    # mount, not whichever temporary checkout happens to run CI.
    fresh = generate_all(COMMITTED_PROJECT_ROOT)
    for filename, content in fresh.items():
        committed = LAUNCHD_DIR / filename
        assert committed.is_file(), f"{filename} has never been generated"
        assert committed.read_bytes() == content, (
            f"{filename} is stale — re-run "
            "`.venv/bin/python -m orchestration.launchd.generate_plists`"
        )


def test_exactly_two_agents_are_committed():
    """One controller plus the dashboard.

    The previous ``auth`` @08:45 + ``intraday_options`` @09:00 pair is gone:
    two independent triggers cannot express "no runtime before validated
    authentication", they can only be scheduled apart and hoped about. The
    on-disk assertion is what catches a stale plist surviving a rename.
    """
    names = {spec.short_name for spec in PLIST_SPECS}
    assert names == {"autostart", "dashboard"}

    on_disk = {path.name for path in LAUNCHD_DIR.glob("*.plist")}
    assert on_disk == {spec.filename for spec in PLIST_SPECS}
    assert "com.soundarraj.algotrading.auth.plist" not in on_disk
    assert "com.soundarraj.algotrading.intraday_options.plist" not in on_disk


def test_positional_options_is_covered_without_a_plist_of_its_own():
    """It is a real runtime, not a placeholder. The controller starts it."""
    from scripts._runtimes import RUNTIMES

    assert "positional_options" in RUNTIMES
    document = _committed_plists()["com.soundarraj.algotrading.autostart.plist"]
    command = " ".join(document["ProgramArguments"])
    assert "orchestration.auto_start" in command


@pytest.mark.parametrize("spec", PLIST_SPECS, ids=lambda s: s.short_name)
def test_every_plist_parses(spec):
    path = LAUNCHD_DIR / spec.filename
    with path.open("rb") as handle:
        plistlib.load(handle)  # raises on malformed XML


@pytest.mark.parametrize("spec", PLIST_SPECS, ids=lambda s: s.short_name)
def test_the_label_uses_the_shared_prefix_and_is_distinct_from_the_legacy_one(spec):
    assert spec.label.startswith(LABEL_PREFIX)
    assert spec.label != "com.soundarraj.tradingautomation.starttrading"


def _quoted_paths(document) -> list[str]:
    """Every single-quoted absolute path the generated command line carries.

    The real program now lives inside a ``/bin/sh -c`` script (the delayed-mount
    wrapper), so the paths that must be absolute are the quoted tokens within
    it rather than bare ``ProgramArguments`` entries.
    """
    joined = " ".join(document["ProgramArguments"])
    return [token for token in re.findall(r"'([^']+)'", joined) if token.startswith("/")]


@pytest.mark.parametrize("filename,document", list(_committed_plists().items()))
def test_every_path_is_absolute(filename, document):
    program_arguments = document["ProgramArguments"]
    assert Path(program_arguments[0]).is_absolute(), f"{filename}: {program_arguments[0]!r}"

    for token in _quoted_paths(document):
        assert Path(token).is_absolute(), f"{filename}: {token!r} is not an absolute path"
    assert ".." not in " ".join(program_arguments), f"{filename}: a relative path crept in"

    assert Path(document["WorkingDirectory"]).is_absolute()
    assert Path(document["StandardOutPath"]).is_absolute()
    assert Path(document["StandardErrorPath"]).is_absolute()


@pytest.mark.parametrize("filename,document", list(_committed_plists().items()))
def test_the_interpreter_is_this_projects_own_venv(filename, document):
    candidates = [token for token in _quoted_paths(document) if token.endswith("/.venv/bin/python")]
    assert candidates, f"{filename}: no .venv interpreter found in {document['ProgramArguments']}"
    assert all(Path(token).is_absolute() for token in candidates)


@pytest.mark.parametrize("filename,document", list(_committed_plists().items()))
def test_no_launchd_prerequisite_path_needs_the_mounted_volume(filename, document):
    """The correction that makes delayed-mount recovery real.

    launchd chdirs to ``WorkingDirectory`` and opens ``StandardOutPath`` /
    ``StandardErrorPath`` *before* it executes ``ProgramArguments[0]``. While
    any of the three pointed under ``/Volumes/Trading``, job setup could fail
    on an unmounted volume and the carefully-written wait loop would never run
    at all — so the wrapper did not actually prove what it claimed to.
    """
    for key in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
        value = document[key]
        assert not value.startswith("/Volumes/"), (
            f"{filename}: {key} = {value} — launchd must prepare this before the "
            "wait loop can run, so it cannot live on the mounted volume"
        )
        assert Path(value).is_absolute()
        assert "~" not in value and "$" not in value, (
            f"{filename}: {key} carries an unexpanded path; launchd expands neither"
        )


@pytest.mark.parametrize("filename,document", list(_committed_plists().items()))
def test_the_working_directory_exists_on_the_boot_volume(filename, document):
    working_directory = Path(document["WorkingDirectory"])
    assert working_directory.is_dir(), f"{filename}: {working_directory} must already exist"


@pytest.mark.parametrize("filename,document", list(_committed_plists().items()))
def test_the_wrapper_enters_the_project_root_before_exec(filename, document):
    """WorkingDirectory is no longer the project root, so the shell must cd —
    otherwise `.env` (loaded relative to CWD) and every relative path move."""
    script = document["ProgramArguments"][2]
    assert f"cd '{COMMITTED_PROJECT_ROOT}'" in script
    assert script.index("cd '") < script.index("exec "), "the cd must precede the exec"


@pytest.mark.parametrize("filename,document", list(_committed_plists().items()))
def test_project_root_still_points_at_the_mounted_volume(filename, document):
    """PROJECT_ROOT is an environment variable, not something launchd opens,
    so it may — and must — still name the real project."""
    assert document["EnvironmentVariables"]["PROJECT_ROOT"] == str(COMMITTED_PROJECT_ROOT)


def test_stdout_and_stderr_paths_are_distinct_across_every_label():
    documents = _committed_plists()
    stream_paths = [
        path
        for document in documents.values()
        for path in (document["StandardOutPath"], document["StandardErrorPath"])
    ]
    assert len(stream_paths) == len(set(stream_paths)), "two labels share a stdout/stderr path"


def test_stdout_and_stderr_paths_live_under_the_dedicated_boot_log_root():
    """A dedicated boot-volume directory, not the project's own logs/ — see
    test_no_launchd_prerequisite_path_needs_the_mounted_volume for why."""
    from orchestration.launchd.generate_plists import boot_log_root

    log_root = boot_log_root()
    for document in _committed_plists().values():
        assert Path(document["StandardOutPath"]).is_relative_to(log_root)
        assert Path(document["StandardErrorPath"]).is_relative_to(log_root)


def test_only_the_trading_controller_wraps_in_caffeinate():
    """Spec section 13's sleep-prevention requirement is scoped to the trading
    controller's own process lifetime — and that lifetime is the whole session,
    because the controller supervises its runtime children rather than spawning
    and exiting. The dashboard has no runtime-hours requirement of its own."""
    documents = _committed_plists()
    for spec in PLIST_SPECS:
        command = " ".join(documents[spec.filename]["ProgramArguments"])
        wraps_caffeinate = "/usr/bin/caffeinate" in command
        assert wraps_caffeinate == (spec.short_name == "autostart")


def test_keep_alive_is_false_on_every_plist():
    """launchd's own KeepAlive is unbounded — bounding restarts is
    orchestration.process_control.supervised_launch's job, not KeepAlive's."""
    for document in _committed_plists().values():
        assert document["KeepAlive"] is False


def test_the_controller_plist_points_at_the_auto_start_package():
    documents = _committed_plists()
    command = " ".join(
        documents["com.soundarraj.algotrading.autostart.plist"]["ProgramArguments"]
    )
    assert "orchestration.auto_start" in command
    # No runtime is named in the plist at all: which runtimes start is decided
    # by config at runtime, never baked into a LaunchAgent.
    assert "runtimes.intraday_options" not in command
    assert "--runtime-id" not in command


def test_keep_alive_false_matches_what_the_code_says_about_restarts():
    """The comments and the plist must agree.

    ``supervised_launch``'s docstring used to claim launchd would restart it
    after its attempt budget ran out. With ``KeepAlive=false`` and no
    ``ThrottleInterval``, launchd does no such thing — recovery is the next
    scheduled controller trigger. Assert both halves so they cannot drift.
    """
    import orchestration.process_control.supervised_launch as sl

    for document in _committed_plists().values():
        assert document["KeepAlive"] is False
        assert "ThrottleInterval" not in document
    assert "Exhaustion is final for the day" in (sl.__doc__ or "")


def test_every_plist_sets_project_root_in_its_environment():
    """PROJECT_ROOT names the real project — it used to be asserted equal to
    WorkingDirectory, which stopped being right when WorkingDirectory moved to
    the boot volume. What must still hold is that the environment variable and
    the directory the wrapper actually enters are the same place."""
    for document in _committed_plists().values():
        project_root = document["EnvironmentVariables"]["PROJECT_ROOT"]
        assert project_root == str(COMMITTED_PROJECT_ROOT)
        assert f"cd '{project_root}'" in document["ProgramArguments"][2]


# ------------------------------------------------- the delayed-volume mechanism
def test_every_agent_waits_for_the_project_volume():
    """The plists point into /Volumes/Trading, which may mount after launchd
    fires. Program[0] must therefore be something that exists regardless."""
    from orchestration.launchd.generate_plists import SHELL_BIN

    for filename, document in _committed_plists().items():
        program_arguments = document["ProgramArguments"]
        assert program_arguments[0] == SHELL_BIN, f"{filename} cannot survive a late mount"
        assert program_arguments[1] == "-c"


def test_the_wait_executable_lives_on_the_boot_volume_and_exists():
    from orchestration.launchd.generate_plists import CAFFEINATE_BIN, SHELL_BIN

    for binary in (SHELL_BIN, CAFFEINATE_BIN):
        assert Path(binary).is_absolute()
        assert not binary.startswith("/Volumes/"), f"{binary} would itself need the mount"
        assert Path(binary).exists(), f"{binary} must be present before any volume mounts"


def test_the_trading_wrapper_uses_an_absolute_wall_clock_deadline():
    """Not an elapsed budget, and this is the whole point.

    RunAtLoad fires whenever the Mac is switched on. Under a
    ``deadline - startup`` elapsed budget a 07:00 login with the volume
    unavailable would expire at 13:15 — and while that shell is still waiting,
    launchd will not start a second copy for the 09:00 StartCalendarInterval
    event. The day would be lost by the very mechanism meant to save it.
    """
    from orchestration.launchd.generate_plists import session_deadline_hhmm

    script = _committed_plists()["com.soundarraj.algotrading.autostart.plist"][
        "ProgramArguments"
    ][2]
    deadline = session_deadline_hhmm()
    assert deadline == "1515"
    assert f"-ge 1{deadline}" in script, "the boundary must be a wall-clock comparison"
    assert "/bin/date +%H%M" in script
    assert "session deadline" in script
    assert "is /Volumes/Trading mounted?" in script, "the log message must be actionable"
    # An elapsed counter must not survive anywhere in the trading wrapper.
    assert "n=$((n+1))" not in script
    assert "n=0" not in script


def test_the_wait_loop_is_valid_shell():
    """Parsed with `sh -n`, which checks syntax and executes nothing."""
    import subprocess

    for filename, document in _committed_plists().items():
        script = document["ProgramArguments"][2]
        result = subprocess.run(
            ["/bin/sh", "-n", "-c", script], capture_output=True, text=True
        )
        assert result.returncode == 0, f"{filename}: {result.stderr}"


def test_the_wait_loop_execs_this_projects_own_venv_interpreter():
    for document in _committed_plists().values():
        script = document["ProgramArguments"][2]
        assert "/.venv/bin/python" in script
        assert "exec " in script, "the shell must hand off, not linger as a parent"


def test_the_generator_refuses_a_path_containing_a_quote():
    from orchestration.launchd.generate_plists import _shell_quote

    with pytest.raises(ValueError, match="quote"):
        _shell_quote("/Volumes/Tra'ding")


# ------------------------------------------------------------------ scheduling
def test_the_controller_runs_at_load_and_on_weekdays_at_0900():
    document = _committed_plists()["com.soundarraj.algotrading.autostart.plist"]
    assert document["RunAtLoad"] is True, "a 09:10 login must still start the day"

    intervals = document["StartCalendarInterval"]
    assert {entry["Weekday"] for entry in intervals} == {1, 2, 3, 4, 5}
    assert all(entry["Hour"] == 9 and entry["Minute"] == 0 for entry in intervals)


def test_the_dashboard_is_load_triggered_only_so_it_works_at_weekends():
    document = _committed_plists()["com.soundarraj.algotrading.dashboard.plist"]
    assert document["RunAtLoad"] is True
    assert "StartCalendarInterval" not in document


# ---------------------------------------------------------------------- safety
def test_no_plist_carries_a_secret_or_a_live_enabling_value():
    forbidden = (
        "access_token",
        "ACCESS_TOKEN",
        "DHAN_PIN",
        "DHAN_CLIENT_ID",
        "TOTP",
        "totp",
        "bot_token",
        "BOT_TOKEN",
        "TELEGRAM",
        "live_trading_enabled",
        "live_approved",
        "live_execution_allowed",
        "mode: live",
        "mode=live",
        "--live",
    )
    for filename, document in _committed_plists().items():
        blob = repr(document)
        for token in forbidden:
            assert token not in blob, f"{filename} carries {token!r}"
        # Bare "live" would match KeepAlive, so check it as a standalone
        # command-line token instead of a substring.
        assert "live" not in " ".join(document["ProgramArguments"]).split()


def test_the_only_environment_variable_is_the_project_root():
    """Credentials reach the runtime through .env, read from WorkingDirectory —
    never through the plist, which is world-readable in ~/Library."""
    for document in _committed_plists().values():
        assert set(document["EnvironmentVariables"]) == {"PROJECT_ROOT"}
