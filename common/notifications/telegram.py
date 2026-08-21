"""Telegram notifier.

Two properties matter more than features here:

* **It is inert without credentials.** No token or chat ID means every send is a
  no-op returning False, so no test needs network access and a developer without
  a bot never sees an error storm.
* **It is inert while the external-notification guard is set**, credentials or
  not. :func:`~common.notifications.guard.external_notifications_disabled` is
  re-read on every send, immediately before the socket would be opened —
  the last line of defence behind :func:`~common.notifications.factory.
  build_notifier`, and the one that also covers a caller that constructed this
  class directly instead of going through the factory.
* **The token never leaves this module.** It is read from ``SecretStr`` at call
  time and placed in the URL; it is never logged, never included in an exception
  message, and never written to SQLite. The logging redactor masks it anyway —
  this is the second layer, not the only one.

Timeouts are short and failures are swallowed by :class:`SafeNotifier`. A slow
chat API must not delay a square-off.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from common.logging import get_logger

from .base import NotificationEvent
from .guard import DISABLE_EXTERNAL_NOTIFICATIONS_ENV, external_notifications_disabled

_log = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 5.0
_API_BASE = "https://api.telegram.org"

#: What a transport does: take a URL, a form-encoded body and a timeout, and
#: return the raw response bytes. The default is ``urllib``; a test injects a
#: fake so the request-building and response-parsing logic below can be
#: exercised for real without a socket ever being opened.
Transport = Callable[[str, bytes, float], bytes]


def _urllib_transport(url: str, payload: bytes, timeout: float) -> bytes:
    """The real network call. The only outbound egress in this package."""
    request = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        read: bytes = response.read()
    return read


class TelegramNotifier:
    """Sends events to one Telegram chat, or does nothing if unconfigured."""

    def __init__(
        self,
        *,
        bot_token: str | None,
        chat_id: str | None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Transport | None = None,
    ) -> None:
        self._bot_token = bot_token or None
        self._chat_id = chat_id or None
        self._timeout = timeout_seconds
        self._transport: Transport = transport if transport is not None else _urllib_transport

    @property
    def channel(self) -> str:
        return "telegram"

    @property
    def is_configured(self) -> bool:
        return self._bot_token is not None and self._chat_id is not None

    def send(self, event: NotificationEvent) -> bool:
        if external_notifications_disabled():
            # Checked here, not only in the factory: this is the last statement
            # before a socket would be opened, so it holds for every caller
            # including one that built this object directly.
            _log.debug(
                "%s is set; dropping event_type=%s without contacting Telegram",
                DISABLE_EXTERNAL_NOTIFICATIONS_ENV,
                event.event_type,
            )
            return False
        if not self.is_configured:
            _log.debug("telegram not configured; dropping event_type=%s", event.event_type)
            return False

        payload = urllib.parse.urlencode(
            {"chat_id": self._chat_id, "text": event.rendered()}
        ).encode("utf-8")
        url = f"{_API_BASE}/bot{self._bot_token}/sendMessage"

        try:
            raw = self._transport(url, payload, self._timeout)
            body = json.loads(raw.decode("utf-8"))
            return bool(body.get("ok", False))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            # Deliberately does not include the URL: it contains the bot token.
            _log.warning("telegram send failed event_type=%s error=%s", event.event_type, exc)
            return False
