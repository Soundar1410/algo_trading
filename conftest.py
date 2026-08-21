"""Repository-root pytest bootstrap — the external-notification kill switch.

**Imported before any test module, any ``tests/conftest.py`` fixture and any
``common.*`` import.** pytest loads conftest files from the rootdir downwards,
and this one sets its environment variable at *module import* time rather than
in a hook, so the switch is already in ``os.environ`` before collection can
trigger a single import side effect.

Why an environment variable set here, rather than a fixture or a monkeypatch:

* ``tests/conftest.py``'s ``isolated_env`` fixture runs **per test**, long
  after collection has imported every test module. Anything a module builds at
  import time predates it.
* Fixtures and monkeypatches live in *this* interpreter. The processes that
  actually sent the real Telegram messages were **other interpreters** — a
  ``multiprocessing`` ``spawn`` worker and a ``subprocess`` child started with
  ``cwd=REPO_ROOT`` (``tests/end_to_end/test_supervisor_signal.py``), each of
  which re-imports everything from scratch and re-reads the repository's real
  ``.env``. An environment variable is inherited by all of them; nothing else
  in Python is.
* Deleting ``TELEGRAM_*`` variables does not help and never did: the
  credentials are in ``.env``, and ``Settings`` re-reads that file on every
  construction. The switch has to sit downstream of credential loading, which
  is where :mod:`common.notifications.guard` puts it.

Production is untouched: nothing outside this file and the test suite sets the
variable, and absent it the guard returns ``False`` and notifications behave
exactly as they always have.

The name is duplicated as a literal rather than imported from
:mod:`common.notifications.guard`, deliberately: importing the package here
would be the very first project import of the session, and this file's whole
job is to be ordered *before* project imports. ``test_notification_guard.py``
asserts the two spellings agree, so the duplication cannot drift.
"""

from __future__ import annotations

import os

DISABLE_EXTERNAL_NOTIFICATIONS_ENV = "ALGO_DISABLE_EXTERNAL_NOTIFICATIONS"

os.environ[DISABLE_EXTERNAL_NOTIFICATIONS_ENV] = "1"


def pytest_configure(config: object) -> None:
    """Re-assert the switch once pytest is configured.

    Belt and braces: the module-level assignment above is what actually
    matters, but a plugin or a stray ``os.environ.clear()`` between import and
    configure would otherwise go unnoticed until a message escaped.
    """
    os.environ[DISABLE_EXTERNAL_NOTIFICATIONS_ENV] = "1"
