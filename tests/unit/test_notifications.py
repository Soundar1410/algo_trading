"""Notifications must never be able to stop trading, or slow it down."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

from common.config.models import ExecutionMode
from common.notifications import (
    NotificationEvent,
    NullNotifier,
    RecordingNotifier,
    SafeNotifier,
    TelegramNotifier,
)


def _event(event_type: str = "order_filled", **overrides: object) -> NotificationEvent:
    fields: dict[str, object] = {
        "event_type": event_type,
        "message": "BUY 50 NIFTY @ 100.5",
        "runtime_id": "intraday_options",
        "strategy_id": "st01",
        "execution_mode": ExecutionMode.PAPER,
    }
    fields.update(overrides)
    return NotificationEvent(**fields)  # type: ignore[arg-type]


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


class _SlowNotifier:
    """Blocks for a caller-chosen duration, like a real Telegram timeout."""

    def __init__(self, delay_seconds: float) -> None:
        self._delay = delay_seconds
        self.sent: list[NotificationEvent] = []
        self._lock = threading.Lock()

    @property
    def channel(self) -> str:
        return "slow"

    def send(self, event: NotificationEvent) -> bool:
        time.sleep(self._delay)
        with self._lock:
            self.sent.append(event)
        return True


def _ticking_clock(*moments: datetime):
    remaining = iter(moments)

    def clock() -> datetime:
        return next(remaining)

    return clock


# ------------------------------------------------------------- safety
def test_a_raising_notifier_does_not_propagate():
    """The spec: notification failure is logged and counted, never fatal."""
    safe = SafeNotifier(_ExplodingNotifier())
    assert safe.send(_event()) is False
    assert safe.failure_count == 1
    assert safe.last_failure == "chat API is down"


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


def test_the_rendered_message_always_carries_a_timestamp():
    when = datetime(2026, 8, 7, 9, 15, tzinfo=UTC)
    rendered = _event(occurred_at=when).rendered()
    assert when.isoformat() in rendered


def test_the_rendered_message_carries_the_correlation_id_when_present():
    rendered = _event(correlation_id="p_io_st01_20260807_0001").rendered()
    assert "p_io_st01_20260807_0001" in rendered


def test_the_rendered_message_omits_a_correlation_line_when_absent():
    rendered = _event().rendered()
    assert "ref=" not in rendered


def test_the_rendered_message_carries_the_required_action_when_present():
    rendered = _event(required_action="Close manually and check positions.").rendered()
    assert "Action: Close manually and check positions." in rendered


def test_the_rendered_message_has_no_action_line_when_absent():
    assert "Action:" not in _event().rendered()


def test_a_known_secret_is_redacted_from_the_rendered_message():
    """The second layer redaction.py's own docstring already claims for every
    notified message ("printed, persisted or notified") — made real here."""
    from common.logging import setup_logging

    setup_logging(settings=None)
    from common.logging import active_redactor

    active_redactor().add_secrets(["super-secret-token-value"])
    try:
        rendered = _event(message="token=super-secret-token-value").rendered()
        assert "super-secret-token-value" not in rendered
        assert "***REDACTED***" in rendered
    finally:
        setup_logging(settings=None)  # reset the module-global filter for later tests


def test_rendering_is_unredacted_when_no_logging_has_been_configured():
    """No active redactor (setup_logging never called in this process) must not
    raise — it degrades to unredacted rather than failing the send."""
    import common.logging.setup as setup_module

    previous = setup_module._ACTIVE_FILTER
    setup_module._ACTIVE_FILTER = None
    try:
        assert _event().rendered().startswith("[PAPER]")
    finally:
        setup_module._ACTIVE_FILTER = previous


# ---------------------------------------------------------- aggregation
def test_a_repeated_identical_event_is_suppressed_within_the_window():
    """This is what "aggregate repeated errors" (spec) means: the caller can
    call send() every poll for a persisting condition, and only the first
    reaches the channel — the two hand-rolled per-call-site latches this
    replaces (supervisor._stuck_subscription_alarmed, engine._entry_blocked)
    each did exactly this, by hand, for exactly one call site."""
    base = datetime(2026, 8, 7, 9, 15, tzinfo=UTC)
    safe = SafeNotifier(RecordingNotifier(), clock=_ticking_clock(*([base] * 5)))
    for _ in range(5):
        safe.send(_event())
    assert safe.success_count == 1
    assert safe.suppressed_count == 4
    assert safe.failure_count == 0


def test_repeated_failures_are_suppressed_not_amplified():
    """5 identical failures inside the window reach the channel once, not 5
    times — the property the old test's name promised without the aggregation
    to back it (Phase 7 Part 1 audit finding)."""
    base = datetime(2026, 8, 7, 9, 15, tzinfo=UTC)
    safe = SafeNotifier(_ExplodingNotifier(), clock=_ticking_clock(*([base] * 5)))
    for _ in range(5):
        safe.send(_event())
    assert safe.failure_count == 1
    assert safe.suppressed_count == 4


def test_a_distinct_message_of_the_same_event_type_is_never_suppressed():
    """Two different trades' entry events must both reach the channel — only
    an *identical* repeat is aggregation's target."""
    recorder = RecordingNotifier()
    safe = SafeNotifier(recorder)
    safe.send(_event(message="BUY 50 NIFTY @ 100.5"))
    safe.send(_event(message="BUY 50 NIFTY @ 101.0"))
    assert len(recorder.events) == 2
    assert safe.suppressed_count == 0


def test_a_repeat_after_the_window_elapses_is_delivered_with_a_count():
    base = datetime(2026, 8, 7, 9, 15, tzinfo=UTC)
    after_window = base + timedelta(seconds=61)
    recorder = RecordingNotifier()
    safe = SafeNotifier(
        recorder,
        aggregation_window_seconds=60.0,
        clock=_ticking_clock(base, base, base, after_window),
    )
    safe.send(_event())  # delivered
    safe.send(_event())  # suppressed
    safe.send(_event())  # suppressed
    safe.send(_event())  # window elapsed: delivered, with the suppressed count noted

    assert len(recorder.events) == 2
    assert "repeated 2x" in recorder.events[-1].message


def test_aggregation_keys_on_runtime_strategy_and_event_type_too():
    """Same message, different strategy: not the same alarm."""
    recorder = RecordingNotifier()
    safe = SafeNotifier(recorder)
    safe.send(_event(strategy_id="st01"))
    safe.send(_event(strategy_id="st02"))
    assert len(recorder.events) == 2
    assert safe.suppressed_count == 0


# ------------------------------------------------------------- failure persistence
def _recording_on_failure(calls: list[tuple[NotificationEvent, str]]):
    def _callback(event: NotificationEvent, reason: str) -> None:
        calls.append((event, reason))

    return _callback


def test_a_synchronous_failure_reaches_on_failure_immediately():
    calls: list[tuple[NotificationEvent, str]] = []
    safe = SafeNotifier(_ExplodingNotifier(), on_failure=_recording_on_failure(calls))
    safe.send(_event())
    assert len(calls) == 1
    assert calls[0][0].event_type == "order_filled"
    assert calls[0][1] == "chat API is down"


def test_on_failure_can_be_set_after_construction():
    """The supervisor builds SafeNotifier before its repository exists."""
    calls: list[tuple[NotificationEvent, str]] = []
    safe = SafeNotifier(_ExplodingNotifier())
    safe.send(_event())
    assert calls == []  # nothing registered yet
    safe.set_on_failure(_recording_on_failure(calls))
    safe.send(_event(message="a distinct second message"))
    assert len(calls) == 1


def test_a_raising_on_failure_callback_does_not_propagate():
    def _exploding_callback(_event: NotificationEvent, _reason: str) -> None:
        raise RuntimeError("database is down too")

    safe = SafeNotifier(_ExplodingNotifier(), on_failure=_exploding_callback)
    assert safe.send(_event()) is False  # the original notification failure, not the callback's


# --------------------------------------------------------------- deferred mode
def test_a_slow_notifier_does_not_delay_the_caller_in_deferred_mode():
    """This is the whole point: engine.py's notifier reaches the tick thread,
    and a 5s Telegram stall must never be that thread's problem."""
    slow = _SlowNotifier(delay_seconds=0.3)
    safe = SafeNotifier(slow, deferred=True)
    try:
        started = time.monotonic()
        result = safe.send(_event())
        elapsed = time.monotonic() - started
        assert result is True  # accepted, not yet delivered
        assert elapsed < 0.1, f"send() blocked for {elapsed:.3f}s in deferred mode"
    finally:
        safe.close()
    assert len(slow.sent) == 1  # delivered by the time close() returns


def test_deferred_mode_still_counts_success_and_failure():
    safe = SafeNotifier(_ExplodingNotifier(), deferred=True)
    try:
        safe.send(_event())
        safe.close()
    finally:
        pass
    assert safe.failure_count == 1


def test_deferred_failures_reach_on_failure_via_the_next_send_or_close():
    calls: list[tuple[NotificationEvent, str]] = []
    safe = SafeNotifier(
        _ExplodingNotifier(), deferred=True, on_failure=_recording_on_failure(calls)
    )
    safe.send(_event())
    safe.close()
    assert len(calls) == 1
    assert calls[0][1] == "chat API is down"


def test_a_full_deferred_queue_drops_the_oldest_and_counts_it_never_blocks():
    slow = _SlowNotifier(delay_seconds=0.5)  # keeps the drain thread busy on item 1
    safe = SafeNotifier(slow, deferred=True, max_queue=2)
    try:
        # Distinct messages: aggregation must not be what suppresses these.
        safe.send(_event(message="one"))  # picked up by the drain thread almost immediately
        safe.send(_event(message="two"))
        safe.send(_event(message="three"))
        safe.send(_event(message="four"))  # queue full: "two" (oldest still queued) is dropped
        assert safe.dropped_count >= 1
    finally:
        safe.close(timeout=3.0)


def test_close_is_a_no_op_in_synchronous_mode():
    safe = SafeNotifier(RecordingNotifier())
    safe.close()  # must not raise, must not hang
    assert safe.send(_event()) is True


def test_close_is_safe_to_call_more_than_once():
    safe = SafeNotifier(RecordingNotifier(), deferred=True)
    safe.send(_event())
    safe.close()
    safe.close()


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
