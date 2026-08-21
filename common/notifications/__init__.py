"""Operational notifications. Never a trading dependency."""

from __future__ import annotations

from .base import (
    NotificationEvent,
    Notifier,
    NullNotifier,
    RecordingNotifier,
    SafeNotifier,
)
from .factory import build_notifier
from .guard import (
    DISABLE_EXTERNAL_NOTIFICATIONS_ENV,
    external_notifications_disabled,
)
from .telegram import TelegramNotifier

__all__ = [
    "DISABLE_EXTERNAL_NOTIFICATIONS_ENV",
    "NotificationEvent",
    "Notifier",
    "NullNotifier",
    "RecordingNotifier",
    "SafeNotifier",
    "TelegramNotifier",
    "build_notifier",
    "external_notifications_disabled",
]
