"""Notifications must never be able to stop trading."""

from __future__ import annotations

from common.config.models import ExecutionMode
from common.notifications import (
    NotificationEvent,
    NullNotifier,
    RecordingNotifier,
    SafeNotifier,
    TelegramNotifier,
)


def _event(event_type: str = "order_filled") -> NotificationEvent:
    return NotificationEvent(
        event_type=event_type,
        message="BUY 50 NIFTY @ 100.5",
        runtime_id="intraday_options",
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
    )


class _ExplodingNotifier:
    @property
    def channel(self) -> str:
        return "exploding"

    def send(self, event: NotificationEvent) -> bool:
        raise RuntimeError("chat API is down")


class _FailingNotifier:
    @property
    def channel(self) -> str:
        return "failing"

    def send(self, event: NotificationEvent) -> bool:
        return False


# ------------------------------------------------------------- safety
def test_a_raising_notifier_does_not_propagate():
    """The spec: notification failure is logged and counted, never fatal."""
    safe = SafeNotifier(_ExplodingNotifier())
    assert safe.send(_event()) is False
    assert safe.failure_count == 1
    assert safe.last_failure == "chat API is down"


def test_repeated_failures_are_counted_not_amplified():
    safe = SafeNotifier(_ExplodingNotifier())
    for _ in range(5):
        safe.send(_event())
    assert safe.failure_count == 5


def test_a_notifier_returning_false_counts_as_a_failure():
    safe = SafeNotifier(_FailingNotifier())
    assert safe.send(_event()) is False
    assert safe.failure_count == 1
    assert safe.success_count == 0


def test_successes_are_counted():
    safe = SafeNotifier(RecordingNotifier())
    assert safe.send(_event()) is True
    assert safe.success_count == 1
    assert safe.failure_count == 0


# -------------------------------------------------------------- rendering
def test_the_rendered_message_always_states_the_mode():
    """An operator reading this on a phone must not have to guess."""
    assert _event().rendered().startswith("[PAPER]")


def test_a_system_event_without_a_mode_is_labelled_system():
    event = NotificationEvent(
        event_type="runtime_start", message="up", runtime_id="intraday_options"
    )
    assert event.rendered().startswith("[SYSTEM]")


def test_the_rendered_message_names_the_strategy_and_event():
    rendered = _event().rendered()
    assert "st01" in rendered
    assert "order_filled" in rendered


# ------------------------------------------------------------- notifiers
def test_the_null_notifier_accepts_everything_silently():
    assert NullNotifier().send(_event()) is True


def test_the_recording_notifier_captures_events_in_order():
    recorder = RecordingNotifier()
    recorder.send(_event("first"))
    recorder.send(_event("second"))
    assert [e.event_type for e in recorder.events] == ["first", "second"]


# -------------------------------------------------------------- telegram
def test_telegram_is_inert_without_credentials():
    """No token means no network call — which is why tests need no secrets."""
    notifier = TelegramNotifier(bot_token=None, chat_id=None)
    assert notifier.is_configured is False
    assert notifier.send(_event()) is False


def test_telegram_is_inert_with_only_a_token():
    notifier = TelegramNotifier(bot_token="123:abc", chat_id=None)
    assert notifier.is_configured is False
    assert notifier.send(_event()) is False


def test_telegram_reports_its_channel_name():
    assert TelegramNotifier(bot_token=None, chat_id=None).channel == "telegram"


def test_a_telegram_failure_is_absorbed_by_the_safe_wrapper():
    safe = SafeNotifier(TelegramNotifier(bot_token=None, chat_id=None))
    assert safe.send(_event()) is False
    assert safe.failure_count == 1
