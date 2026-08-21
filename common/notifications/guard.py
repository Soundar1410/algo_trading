"""The one switch that makes every external notification channel inert.

Why this exists, concretely: a previous ``pytest`` run delivered *hundreds* of
real Telegram messages to the operator's phone — ``worker_started`` /
``order_filled`` / ``worker_stopped``, all tagged ``strategy_id=skelfix``,
which is a **test fixture**. The path was not exotic. ``tests/end_to_end/
test_supervisor_signal.py`` starts its child process with
``cwd=REPO_ROOT`` and ``env=dict(os.environ)``; :class:`~common.config.
Settings` reads ``.env`` *relative to the working directory*, so that child —
and every ``spawn``ed worker under it, each of which builds its own notifier
from :data:`~runtimes.intraday_options.worker.NOTIFIER_FROM_SETTINGS` — loaded
the repository's real Telegram credentials and notified for real.

Two lessons are baked into the design here:

* **Clearing environment variables is not a guard.** ``tests/conftest.py``
  already deleted every ``TELEGRAM_*`` variable, and it made no difference:
  the credentials were never in the environment to begin with, they were in
  ``.env``, and ``Settings()`` re-reads that file on every construction, in
  every process, at any moment. A guard has to sit *downstream* of credential
  loading, not upstream of it.
* **It has to cross process boundaries.** ``spawn`` re-imports the child from
  scratch and ``subprocess`` starts a fresh interpreter, so no in-process
  monkeypatch, import hook or module-level flag survives the trip. An
  environment variable is the one thing the OS hands to every descendant
  unchanged — which is exactly why the switch is an environment variable and
  the check reads ``os.environ`` live rather than a value cached at import.

Default is **off**: absent the variable this returns ``False`` and production
notifies exactly as before. Nothing in ``config/`` or ``.env`` sets it; only
the test bootstrap (``conftest.py`` at the repository root) does.
"""

from __future__ import annotations

import os

#: The switch. Set to a truthy value ("1", "true", "yes", "on") to make every
#: external notification channel inert for this process and all its children.
DISABLE_EXTERNAL_NOTIFICATIONS_ENV = "ALGO_DISABLE_EXTERNAL_NOTIFICATIONS"

#: The matching :class:`~common.config.Settings` field, so an operator can also
#: park the switch in ``.env`` permanently. Named to match the environment
#: variable, as every ``ALGO_*`` setting in that model is.
DISABLE_EXTERNAL_NOTIFICATIONS_FIELD = "algo_disable_external_notifications"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _is_true(value: object) -> bool:
    """Truthiness for a switch that may arrive as a string, bool or ``None``."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE_VALUES


def external_notifications_disabled(settings: object | None = None) -> bool:
    """Whether external notification channels must be inert in this process.

    True when *either* source says so — the environment variable or the
    ``Settings`` field. Deliberately an OR, not a precedence chain: this is a
    safety latch, and the only failure mode worth optimising against is one
    source quietly cancelling the other and letting a message out.

    ``settings`` is duck-typed rather than imported, so this module keeps its
    standard-library-only dependency set and can be called from
    :mod:`common.notifications.telegram` — which has no ``Settings`` to hand —
    as the last check before a socket is opened.
    """
    if _is_true(os.environ.get(DISABLE_EXTERNAL_NOTIFICATIONS_ENV)):
        return True
    if settings is None:
        return False
    return _is_true(getattr(settings, DISABLE_EXTERNAL_NOTIFICATIONS_FIELD, None))
