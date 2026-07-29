"""Path resolution — one root, everything derived, nothing hard-coded."""

from __future__ import annotations

from pathlib import Path

import pytest

from common.config import ProjectPaths, ProjectRootError, Settings, load_paths, resolve_project_root


def test_explicit_root_wins(tmp_path: Path):
    assert resolve_project_root(tmp_path) == tmp_path.resolve()


def test_root_comes_from_settings_when_not_explicit(tmp_path: Path):
    settings = Settings(project_root=str(tmp_path))
    assert resolve_project_root(settings=settings) == tmp_path.resolve()


def test_root_falls_back_to_the_repository_containing_the_source():
    """A fresh checkout with no .env must still resolve, or tests need config."""
    root = resolve_project_root(settings=Settings())
    assert (root / "pyproject.toml").is_file()


def test_missing_root_fails_loudly(tmp_path: Path):
    """A runtime must not invent a data directory on an unmounted volume."""
    with pytest.raises(ProjectRootError, match="does not exist"):
        resolve_project_root(tmp_path / "not_mounted")


def test_every_path_derives_from_the_root(tmp_path: Path):
    paths = ProjectPaths(project_root=tmp_path)
    for derived in (
        paths.data_root,
        paths.log_root,
        paths.runtime_root,
        paths.operational_root,
        paths.cache_root,
        paths.config_root,
        paths.pid_root,
        paths.lock_root,
    ):
        assert tmp_path in derived.parents or derived == tmp_path


def test_no_home_directory_or_reference_repo_path_leaks(tmp_path: Path):
    paths = ProjectPaths(project_root=tmp_path)
    rendered = " ".join(str(p) for p in (paths.data_root, paths.log_root, paths.operational_root))
    assert "Trading_Automation" not in rendered
    assert str(Path.home()) not in rendered


def test_database_path_is_per_runtime_group(tmp_path: Path):
    paths = ProjectPaths(project_root=tmp_path)
    assert paths.database_path("intraday_options").name == "intraday_options.db"
    assert paths.database_path("intraday_options") != paths.database_path("positional_options")


def test_ensure_writable_dirs_creates_only_output_directories(tmp_path: Path):
    paths = load_paths(tmp_path)
    paths.ensure_writable_dirs()

    assert paths.operational_root.is_dir()
    assert paths.pid_root.is_dir()
    assert paths.lock_root.is_dir()
    # Inputs are not fabricated: an empty config/ would mask "you have not
    # written the config yet" as a confusing empty-directory error later.
    assert not paths.config_root.exists()
    assert not paths.reference_root.exists()


def test_ensure_writable_dirs_is_idempotent(tmp_path: Path):
    paths = load_paths(tmp_path)
    paths.ensure_writable_dirs()
    paths.ensure_writable_dirs()
    assert paths.runtime_root.is_dir()
