"""Notification contract and the wrapper that makes it non-fatal.

The spec's rule is unambiguous: "Notification failure must be logged and counted
but must not stop trading." Telegram is an operational channel, not a control
dependency — a timeout to a chat API must never abort a square-off.

:class:`SafeNotifier` is where that rule is enforced, once, around any notifier.
Relying on each call site to wrap its own ``try/except`` is how a missing one
eventually takes down a worker at the worst moment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from common.config.models import ExecutionMode
from common.logging import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class NotificationEvent:
    """One operational event worth telling a human about."""

    event_type: str
    message: str
    runtime_id: str
    strategy_id: str | None = None
    execution_mode: ExecutionMode | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def rendered(self) -> str:
        """Human-facing text, always stating the mode.

        The mode is included deliberately: an operator reading "position opened"
        on a phone must never have to guess whether it was real money.
        """
        mode = f"[{self.execution_mode.value.upper()}]" if self.execution_mode else "[SYSTEM]"
        scope = f" {self.strategy_id}" if self.strategy_id else ""
        return f"{mode} {self.runtime_id}{scope} — {self.event_type}: {self.message}"


@runtime_checkable
class Notifier(Protocol):
    """Somewhere to send an operational event."""

    @property
    def channel(self) -> str: ...

    def send(self, event: NotificationEvent) -> bool:
        """Deliver one event. Returns True on success."""
        ...


class NullNotifier:
    """Discards everything. The default when no channel is configured."""

    @property
    def channel(self) -> str:
        return "null"

    def send(self, event: NotificationEvent) -> bool:
        return True


class RecordingNotifier:
    """Captures events in memory. For tests and the dashboard's local preview."""

    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    @property
    def channel(self) -> str:
        return "recording"

    def send(self, event: NotificationEvent) -> bool:
        self.events.append(event)
        return True


class SafeNotifier:
    """Wraps a notifier so no delivery failure can escape into trading code."""

    def __init__(self, inner: Notifier) -> None:
        self._inner = inner
        self.failure_count = 0
        self.success_count = 0
        self.last_failure: str | None = None

    @property
    def channel(self) -> str:
        return self._inner.channel

    def send(self, event: NotificationEvent) -> bool:
        try:
            delivered = self._inner.send(event)
        except Exception as exc:
            self.failure_count += 1
            self.last_failure = str(exc)
            _log.warning(
                "notification failed channel=%s event_type=%s error=%s",
                self._inner.channel,
                event.event_type,
                exc,
            )
            return False
        if delivered:
            self.success_count += 1
        else:
            self.failure_count += 1
        return delivered
