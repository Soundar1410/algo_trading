"""orchestration.launchd: the generator and its committed output.

The committed ``.plist`` files are generated output, never hand-edited (see
``generate_plists.py``'s own docstring) — so the drift guard
(``test_the_committed_plists_match_a_fresh_generation``) is the test that
actually matters here: everything else checks the generator's *rules*, which
only matter if the committed files still obey them.
"""

from __future__ import annotations

import plistlib
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


def test_exactly_the_three_real_entrypoints_have_a_plist():
    """Spec section 12 names five; only three have a real entrypoint (Phase 8
    audit — positional_options and intraday_stocks are placeholders/absent,
    runbook D56/D34). No plist should exist for either."""
    names = {spec.short_name for spec in PLIST_SPECS}
    assert names == {"auth", "intraday_options", "dashboard"}

    on_disk = {path.name for path in LAUNCHD_DIR.glob("*.plist")}
    assert on_disk == {spec.filename for spec in PLIST_SPECS}


@pytest.mark.parametrize("spec", PLIST_SPECS, ids=lambda s: s.short_name)
def test_every_plist_parses(spec):
    path = LAUNCHD_DIR / spec.filename
    with path.open("rb") as handle:
        plistlib.load(handle)  # raises on malformed XML


@pytest.mark.parametrize("spec", PLIST_SPECS, ids=lambda s: s.short_name)
def test_the_label_uses_the_shared_prefix_and_is_distinct_from_the_legacy_one(spec):
    assert spec.label.startswith(LABEL_PREFIX)
    assert spec.label != "com.soundarraj.tradingautomation.starttrading"


@pytest.mark.parametrize("filename,document", list(_committed_plists().items()))
def test_every_path_is_absolute(filename, document):
    program_arguments = document["ProgramArguments"]
    # The interpreter (or caffeinate/python) and every -m target argument
    # that looks like a path must be absolute; flags and module names
    # ("-m", "scripts.auth_bootstrap") are not paths and are skipped.
    for token in program_arguments:
        if token.startswith("-") or "/" not in token:
            continue
        assert Path(token).is_absolute(), f"{filename}: {token!r} is not an absolute path"

    assert Path(document["WorkingDirectory"]).is_absolute()
    assert Path(document["StandardOutPath"]).is_absolute()
    assert Path(document["StandardErrorPath"]).is_absolute()


@pytest.mark.parametrize("filename,document", list(_committed_plists().items()))
def test_the_interpreter_is_this_projects_own_venv(filename, document):
    program_arguments = document["ProgramArguments"]
    python_candidates = [arg for arg in program_arguments if arg.endswith("/.venv/bin/python")]
    assert python_candidates, f"{filename}: no .venv interpreter found in {program_arguments}"


@pytest.mark.parametrize("filename,document", list(_committed_plists().items()))
@pytest.mark.skipif(
    not (COMMITTED_PROJECT_ROOT / "pyproject.toml").is_file(),
    reason="operator-Mac mount validation requires /Volumes/Trading/algo_trading",
)
def test_working_directory_is_the_project_root(filename, document):
    working_directory = Path(document["WorkingDirectory"])
    assert (working_directory / "pyproject.toml").is_file(), (
        f"{filename}: WorkingDirectory {working_directory} does not look like the project root"
    )


def test_stdout_and_stderr_paths_are_distinct_across_every_label():
    documents = _committed_plists()
    stream_paths = [
        path
        for document in documents.values()
        for path in (document["StandardOutPath"], document["StandardErrorPath"])
    ]
    assert len(stream_paths) == len(set(stream_paths)), "two labels share a stdout/stderr path"


def test_stdout_and_stderr_paths_live_under_the_project_log_root():
    for document in _committed_plists().values():
        log_root = Path(document["WorkingDirectory"]) / "logs"
        assert Path(document["StandardOutPath"]).is_relative_to(log_root)
        assert Path(document["StandardErrorPath"]).is_relative_to(log_root)


def test_only_the_runtime_plist_wraps_in_caffeinate():
    """Spec section 13's sleep-prevention requirement is scoped to the
    trading runtime's own process lifetime — not the brief auth bootstrap,
    not the dashboard, which has no runtime-hours requirement of its own."""
    documents = _committed_plists()
    for spec in PLIST_SPECS:
        program_arguments = documents[spec.filename]["ProgramArguments"]
        wraps_caffeinate = program_arguments[0] == "/usr/bin/caffeinate"
        assert wraps_caffeinate == (spec.short_name == "intraday_options")


def test_keep_alive_is_false_on_every_plist():
    """launchd's own KeepAlive is unbounded — bounding restarts is
    orchestration.process_control.supervised_launch's job, not KeepAlive's."""
    for document in _committed_plists().values():
        assert document["KeepAlive"] is False


def test_the_intraday_options_plist_points_at_the_supervised_launcher_not_the_bare_entrypoint():
    documents = _committed_plists()
    program_arguments = documents["com.soundarraj.algotrading.intraday_options.plist"][
        "ProgramArguments"
    ]
    assert "orchestration.process_control.supervised_launch" in program_arguments
    assert "runtimes.intraday_options" not in program_arguments


def test_every_plist_sets_project_root_in_its_environment():
    for document in _committed_plists().values():
        assert document["EnvironmentVariables"]["PROJECT_ROOT"] == document["WorkingDirectory"]
