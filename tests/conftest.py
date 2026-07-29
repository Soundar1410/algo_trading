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
