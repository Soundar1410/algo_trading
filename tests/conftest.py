"""Shared test fixtures.

No test may depend on a real credential, a real network call or the operator's
local ``.env``. The ``isolated_env`` autouse fixture enforces the last part by
clearing every ``DHAN_*`` / ``TELEGRAM_*`` / ``ALGO_*`` variable, so a developer
who happens to have a populated ``.env`` gets the same result as CI.

**Clearing variables is necessary but nowhere near sufficient**, and treating
it as sufficient is what let a previous run deliver hundreds of real Telegram
messages for ``strategy_id=skelfix``: the credentials were never in the
environment, they were in ``.env``, and ``Settings`` re-reads that file on
every construction in every process. The actual guard is
``ALGO_DISABLE_EXTERNAL_NOTIFICATIONS``, set at import time by the repository
root ``conftest.py`` and checked downstream of credential loading by
:mod:`common.notifications.guard`. This fixture deliberately **exempts and
re-asserts** it rather than sweeping it away with the other ``ALGO_*``
variables — clearing it here would have re-opened the exact hole it closes.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

_MANAGED_PREFIXES = ("DHAN_", "TELEGRAM_", "ALGO_", "PROJECT_ROOT")

#: Matches ``ALGO_`` above and so would be cleared with the rest. It must not
#: be — see the module docstring. Kept as a literal for the same reason the
#: root ``conftest.py`` does; ``test_notification_guard.py`` pins them equal.
_NOTIFICATION_GUARD_ENV = "ALGO_DISABLE_EXTERNAL_NOTIFICATIONS"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Remove inherited platform env vars and run from a scratch directory.

    ``chdir`` matters because ``Settings`` reads ``.env`` relative to the working
    directory — without it, the repository's own ``.env`` would leak into tests.
    """
    for key in list(os.environ):
        if key.startswith(_MANAGED_PREFIXES) and key != _NOTIFICATION_GUARD_ENV:
            monkeypatch.delenv(key, raising=False)
    # Re-asserted rather than merely spared: a test that deliberately clears it
    # (see ``allow_external_notifications``) gets it back for the next test,
    # because monkeypatch unwinds per test.
    monkeypatch.setenv(_NOTIFICATION_GUARD_ENV, "1")
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture
def allow_external_notifications(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Lift the notification guard for one test, and only for one test.

    The narrow, explicit exception required by any test that exercises what
    *production* does when the guard is absent. Every such test must inject a
    fake transport — lifting the guard removes the outer latch, not the
    obligation to keep the socket closed. ``monkeypatch`` restores the guard
    when the test ends, and ``isolated_env`` re-asserts it for the next one.
    """
    monkeypatch.delenv(_NOTIFICATION_GUARD_ENV, raising=False)
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
