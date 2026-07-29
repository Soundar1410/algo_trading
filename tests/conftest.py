"""Shared test fixtures.

No test may depend on a real credential, a real network call or the operator's
local ``.env``. The ``isolated_env`` autouse fixture enforces the last part by
clearing every ``DHAN_*`` / ``TELEGRAM_*`` / ``ALGO_*`` variable, so a developer
who happens to have a populated ``.env`` gets the same result as CI.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

_MANAGED_PREFIXES = ("DHAN_", "TELEGRAM_", "ALGO_", "PROJECT_ROOT")


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Remove inherited platform env vars and run from a scratch directory.

    ``chdir`` matters because ``Settings`` reads ``.env`` relative to the working
    directory — without it, the repository's own ``.env`` would leak into tests.
    """
    for key in list(os.environ):
        if key.startswith(_MANAGED_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture
def config_root(tmp_path: Path) -> Path:
    """An empty ``config/`` tree with the directories the loader expects."""
    root = tmp_path / "config"
    (root / "runtimes").mkdir(parents=True)
    (root / "strategies").mkdir(parents=True)
    return root


# --------------------------------------------------------------- Phase 1
#: Absolute, because ``isolated_env`` chdirs away from the repository.
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def tick_tape_path() -> Path:
    """The recorded NIFTY tape: 24 ticks across six one-minute buckets."""
    return FIXTURES_DIR / "nifty_tick_tape.json"


@pytest.fixture
def runtime_dirs(tmp_path: Path) -> dict[str, Path]:
    """Isolated lock/PID/log/database locations for one test."""
    directories = {
        "lock_dir": tmp_path / "runtime" / "locks",
        "pid_dir": tmp_path / "runtime" / "pid",
        "log_dir": tmp_path / "logs",
        "operational": tmp_path / "operational",
    }
    for path in directories.values():
        path.mkdir(parents=True, exist_ok=True)
    return directories


@pytest.fixture
def database_path(runtime_dirs: dict[str, Path]) -> Path:
    return runtime_dirs["operational"] / "intraday_options.db"
