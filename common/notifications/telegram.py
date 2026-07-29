"""Telegram notifier.

Two properties matter more than features here:

* **It is inert without credentials.** No token or chat ID means every send is a
  no-op returning False, so no test needs network access and a developer without
  a bot never sees an error storm.
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

from common.logging import get_logger

from .base import NotificationEvent

_log = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 5.0
_API_BASE = "https://api.telegram.org"


class TelegramNotifier:
    """Sends events to one Telegram chat, or does nothing if unconfigured."""

    def __init__(
        self,
        *,
        bot_token: str | None,
        chat_id: str | None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._bot_token = bot_token or None
        self._chat_id = chat_id or None
        self._timeout = timeout_seconds

    @property
    def channel(self) -> str:
        return "telegram"

    @property
    def is_configured(self) -> bool:
        return self._bot_token is not None and self._chat_id is not None

    def send(self, event: NotificationEvent) -> bool:
        if not self.is_configured:
            _log.debug("telegram not configured; dropping event_type=%s", event.event_type)
            return False

        payload = urllib.parse.urlencode(
            {"chat_id": self._chat_id, "text": event.rendered()}
        ).encode("utf-8")
        url = f"{_API_BASE}/bot{self._bot_token}/sendMessage"

        try:
            request = urllib.request.Request(url, data=payload, method="POST")
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
                return bool(body.get("ok", False))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            # Deliberately does not include the URL: it contains the bot token.
            _log.warning("telegram send failed event_type=%s error=%s", event.event_type, exc)
            return False
