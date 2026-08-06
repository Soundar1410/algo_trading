"""Scoped exception to ``tests/conftest.py``'s ``isolated_env`` fixture.

The repo-wide ``isolated_env`` autouse fixture clears every ``DHAN_*`` /
``ALGO_*`` / ``TELEGRAM_*`` env var before each test runs, so the suite can
never depend on a real credential by accident. The opt-in live smoke tests in
this directory are the one deliberate exception: they exist specifically to
run against a real Dhan account when the operator exports
``ALGO_LIVE_SMOKE=1`` plus real credentials. Those tests gate correctly on
*whether* they run (the module-level ``_ENABLED`` / ``needs_credentials``
checks in ``test_live_feed_smoke.py`` read the environment at import time,
before any fixture executes), but their bodies re-read
``os.environ.get("DHAN_PIN")`` etc. at call time — by then ``isolated_env``
has already wiped it, so an opted-in run fails closed with
``MissingCredentialsError`` instead of exercising anything.

Fix, confined to this directory only: snapshot the real env at collection
time (before any fixture runs), and — only when the operator actually opted
in — restore it after ``isolated_env`` has cleared it. Every other test
outside ``tests/smoke`` is untouched; a ``tests/smoke`` run without
``ALGO_LIVE_SMOKE=1`` behaves exactly as before (nothing restored, nothing
to restore for).
"""

from __future__ import annotations

import os

import pytest

_MANAGED_PREFIXES = ("DHAN_", "TELEGRAM_", "ALGO_")

#: Taken while conftest.py is imported during collection, i.e. before any
#: fixture (including isolated_env) has had a chance to clear anything.
_REAL_LIVE_SMOKE_ENV = {
    key: value for key, value in os.environ.items() if key.startswith(_MANAGED_PREFIXES)
}


@pytest.fixture(autouse=True)
def _restore_live_smoke_credentials(
    isolated_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Undo ``isolated_env``'s clearing, but only for an opted-in live run."""
    if _REAL_LIVE_SMOKE_ENV.get("ALGO_LIVE_SMOKE") != "1":
        return
    for key, value in _REAL_LIVE_SMOKE_ENV.items():
        monkeypatch.setenv(key, value)
