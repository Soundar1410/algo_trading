"""Builds the production notifier from ``Settings`` — the one place that reads
Telegram credentials to decide what a process notifies through.

**Called independently by the supervisor's own entrypoint** (``runtimes/
intraday_options/__main__.py``) **and by each spawned worker** (``runtimes/
intraday_options/worker.py``), not built once and handed down. ``spawn``
re-imports the child from scratch, so it cannot inherit the parent's already-
constructed notifier object across the process boundary — and threading a
Telegram bot token through :class:`~runtimes.intraday_options.worker.
WorkerConfig` (a plain picklable dataclass, by design carrying nothing that
isn't already meant to travel through ``multiprocessing``'s pickling) would be
the wrong way to move a secret between processes when the OS already hands
every child the same environment the parent read ``.env`` from. Each process
therefore calls :func:`build_notifier` with its own freshly-loaded
:class:`~common.config.Settings` and gets there independently.
"""

from __future__ import annotations

from common.config import Settings
from common.config.secrets import read_secret
from common.logging import get_logger

from .base import Notifier, NullNotifier
from .guard import DISABLE_EXTERNAL_NOTIFICATIONS_ENV, external_notifications_disabled
from .telegram import TelegramNotifier

_log = get_logger(__name__)


def build_notifier(settings: Settings) -> Notifier:
    """A :class:`~common.notifications.telegram.TelegramNotifier` when
    credentials are present, else :class:`~common.notifications.base.
    NullNotifier`.

    Logs the decision once, at build time, rather than refusing to start:
    running paper-only with no bot configured is a normal, supported setup —
    spec section 9 ("Telegram notifications"), not a misconfiguration.

    The external-notification guard is checked **before** the credentials, and
    overrides them: with it active this returns a
    :class:`~common.notifications.base.NullNotifier` even when ``settings``
    carries a perfectly valid bot token freshly loaded from a ``.env``. That
    ordering is the whole point — see :mod:`common.notifications.guard` for
    the flood of real ``strategy_id=skelfix`` messages that proved a guard
    placed anywhere upstream of credential loading does not hold.
    """
    if external_notifications_disabled(settings):
        _log.info(
            "%s is set; external notifications are disabled and every channel is inert",
            DISABLE_EXTERNAL_NOTIFICATIONS_ENV,
        )
        return NullNotifier()
    if not settings.has_telegram_credentials():
        _log.info("no Telegram credentials configured; notifications are local-only (log/DB)")
        return NullNotifier()
    _log.info("Telegram notifications enabled")
    return TelegramNotifier(
        bot_token=read_secret(settings.telegram_bot_token),
        chat_id=read_secret(settings.telegram_chat_id),
    )
