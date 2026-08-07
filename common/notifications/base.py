"""Notification contract and the wrapper that makes it non-fatal.

The spec's rule is unambiguous: "Notification failure must be logged and counted
but must not stop trading." Telegram is an operational channel, not a control
dependency — a timeout to a chat API must never abort a square-off.

:class:`SafeNotifier` is where that rule is enforced, once, around any notifier.
Relying on each call site to wrap its own ``try/except`` is how a missing one
eventually takes down a worker at the worst moment.

Phase 7 Part 2 widens what :class:`SafeNotifier` does, without widening what a
caller has to know about it — every existing call site's ``notifier.send(event)``
is unchanged:

* **Rate limiting and aggregation.** An identical ``(runtime_id, strategy_id,
  event_type, message)`` sent again inside a short window is suppressed rather
  than delivered a second time; the next delivery after the window notes how
  many were collapsed. This is what "aggregate repeated errors" (spec) means in
  practice, and it is now a property of every call site, not of the two that
  happened to hand-roll their own boolean latch.
* **Deferred delivery**, opt-in via ``deferred=True``. A synchronous ``send()``
  to a slow channel (Telegram's timeout is 5s) blocks whatever thread called
  it — which for :class:`~common.engine.engine.TradingEngine` is the tick
  thread. Deferred mode enqueues onto a small bounded queue and returns
  immediately; a dedicated drain thread does the real send. The queue drops
  the oldest entry rather than blocking the producer when full (spec: "may use
  a small internal queue" — not Celery, not an external broker).
* **Failure persistence**, via an injected ``on_failure`` callback rather than a
  direct database dependency — this module stays as decoupled from
  ``common.execution`` as :mod:`common.engine` deliberately is from it. A
  failure observed on the *drain thread* is never handed to ``on_failure``
  directly: the callback typically touches a ``sqlite3`` connection, and those
  refuse cross-thread use (the exact bug Phase 7 Part 1 found and fixed for
  ``feed_events``). Deferred failures are queued in memory instead and pumped
  out through ``on_failure`` on the caller's own thread, inside the next
  ``send()`` — see :meth:`SafeNotifier._pump_failures`.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from common.config.models import ExecutionMode
from common.logging import active_redactor, get_logger

_log = get_logger(__name__)

#: Default bound for a deferred notifier's internal queue. Generous for how
#: rarely a real alarm fires, small enough that a stuck channel cannot let
#: unbounded memory stand in for a problem that should just be visible.
DEFAULT_MAX_QUEUE = 256

#: Default aggregation window. A condition that repeats faster than this
#: collapses into one delivery plus a count; spaced further apart than this,
#: each is treated as a fresh occurrence worth its own message.
DEFAULT_AGGREGATION_WINDOW_SECONDS = 60.0

#: How long the drain thread waits on the queue before checking for a stop
#: request. Bounds shutdown latency without busy-waiting.
_DRAIN_POLL_SECONDS = 0.2


@dataclass(frozen=True)
class NotificationEvent:
    """One operational event worth telling a human about."""

    event_type: str
    message: str
    runtime_id: str
    strategy_id: str | None = None
    execution_mode: ExecutionMode | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: The order/correlation ID this event is about, when one exists and is
    #: safe to publish (spec: "Correlation/order ID where safe"). Our own
    #: correlation IDs (``p_io_st01_...``) are generated identifiers, never
    #: broker secrets, so "safe" holds whenever one is available at all.
    correlation_id: str | None = None
    #: What an operator reading this should *do*, for the events where there
    #: is a concrete answer (an alarm, a blocked strategy). Left ``None`` for
    #: routine lifecycle events, which need no action.
    required_action: str | None = None

    def rendered(self) -> str:
        """Human-facing text, always stating the mode.

        The mode is included deliberately: an operator reading "position opened"
        on a phone must never have to guess whether it was real money.

        Redacted before it is returned — the second, independent layer
        :mod:`common.logging.redaction`'s own docstring already claims for
        every notified message, made actually true here rather than only for
        the logging boundary. Every field feeding this text is a typed,
        non-secret value (event/runtime/strategy/mode/timestamp/our-own-
        correlation-id/operator-authored prose) by construction — this is
        defence in depth for that discipline, not a substitute for it.
        """
        mode = f"[{self.execution_mode.value.upper()}]" if self.execution_mode else "[SYSTEM]"
        scope = f" {self.strategy_id}" if self.strategy_id else ""
        header = f"{mode} {self.runtime_id}{scope} — {self.event_type}: {self.message}"
        detail = f"time={self.occurred_at.isoformat()}"
        if self.correlation_id:
            detail += f" ref={self.correlation_id}"
        lines = [header, detail]
        if self.required_action:
            lines.append(f"Action: {self.required_action}")
        text = "\n".join(lines)

        redactor = active_redactor()
        return redactor.redact(text) if redactor is not None else text


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


@dataclass
class _AggregationState:
    window_start: datetime
    suppressed_count: int = 0


class SafeNotifier:
    """Wraps a notifier so no delivery failure — or slow delivery — can reach
    trading code.
    """

    def __init__(
        self,
        inner: Notifier,
        *,
        deferred: bool = False,
        max_queue: int = DEFAULT_MAX_QUEUE,
        aggregation_window_seconds: float = DEFAULT_AGGREGATION_WINDOW_SECONDS,
        on_failure: Callable[[NotificationEvent, str], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._inner = inner
        self._deferred = deferred
        self._window = aggregation_window_seconds
        self._on_failure = on_failure
        self._clock = clock

        self.failure_count = 0
        self.success_count = 0
        self.last_failure: str | None = None
        #: Deliveries collapsed by aggregation — never delivered, not a failure.
        self.suppressed_count = 0
        #: Events dropped because a full deferred queue evicted the oldest entry.
        self.dropped_count = 0

        self._lock = threading.Lock()
        self._recent: dict[tuple[str, str | None, str, str], _AggregationState] = {}
        #: Failures observed on the drain thread, waiting to be handed to
        #: ``on_failure`` on a thread that is actually safe to call it from.
        #: See the module docstring and :meth:`_pump_failures`.
        self._pending_failures: list[tuple[NotificationEvent, str]] = []

        self._queue: queue.Queue[NotificationEvent] | None = None
        self._stop = threading.Event()
        self._drain_thread: threading.Thread | None = None
        if deferred:
            self._queue = queue.Queue(maxsize=max_queue)
            self._drain_thread = threading.Thread(
                target=self._drain_loop, name="notifier-drain", daemon=True
            )
            self._drain_thread.start()

    @property
    def channel(self) -> str:
        return self._inner.channel

    @property
    def deferred(self) -> bool:
        return self._deferred

    def set_on_failure(self, callback: Callable[[NotificationEvent, str], None] | None) -> None:
        """Late-bind the failure-persistence callback.

        Some callers (the supervisor) construct their ``SafeNotifier`` before a
        repository exists — the lock has to be acquired and migrations have to
        run first. This mirrors :meth:`~common.feed.reconnect.ReconnectingFeed.
        set_health_event_sink`, the identical need Phase 7 Part 1 already
        solved for the same reason.
        """
        self._on_failure = callback

    # ---------------------------------------------------------------- send
    def send(self, event: NotificationEvent) -> bool:
        """Deliver (or accept for deferred delivery) one event.

        Returns ``True`` when the event was sent (synchronous mode) or handed
        to the deferred queue (deferred mode) — in the latter case this is
        acceptance, not a delivery guarantee; see the module docstring.
        Returns ``False`` when suppressed by aggregation or when a
        synchronous send failed.
        """
        effective = self._aggregate(event)
        if effective is None:
            return False

        if self._deferred:
            self._enqueue(effective)
            self._pump_failures()
            return True

        return self._deliver_now(effective)

    # ------------------------------------------------------------ aggregation
    def _aggregate(self, event: NotificationEvent) -> NotificationEvent | None:
        """Collapse an identical repeat within the window; ``None`` means suppress.

        Keyed on the fields that make two events "the same alarm firing again",
        not on identity — a *different* trade's ``entry`` event has a different
        ``message`` (it names the price/symbol) and is never suppressed by this.
        """
        key = (event.runtime_id, event.strategy_id, event.event_type, event.message)
        now = self._clock()
        with self._lock:
            self._evict_stale(now)
            state = self._recent.get(key)
            if state is not None and (now - state.window_start).total_seconds() < self._window:
                state.suppressed_count += 1
                self.suppressed_count += 1
                return None

            suppressed_before = state.suppressed_count if state is not None else 0
            self._recent[key] = _AggregationState(window_start=now)

        if suppressed_before:
            return replace(
                event,
                message=f"{event.message} (repeated {suppressed_before}x since last notice)",
                occurred_at=now,
            )
        return event

    def _evict_stale(self, now: datetime) -> None:
        """Bound ``_recent``'s size. Called under ``self._lock``.

        A window's worth of extra grace before eviction: an event evicted the
        instant its window closes would never get the chance to report a
        suppressed-count it was still entitled to on its very next occurrence.
        """
        cutoff = now - timedelta(seconds=self._window * 2)
        stale = [k for k, v in self._recent.items() if v.window_start < cutoff]
        for k in stale:
            del self._recent[k]

    # ------------------------------------------------------------- synchronous
    def _deliver_now(self, event: NotificationEvent) -> bool:
        try:
            delivered = self._inner.send(event)
        except Exception as exc:
            self._on_send_failure(event, str(exc))
            return False
        with self._lock:
            if delivered:
                self.success_count += 1
            else:
                self.failure_count += 1
        if not delivered:
            self._on_send_failure(event, "notifier returned False", count_failure=False)
        return delivered

    def _on_send_failure(
        self, event: NotificationEvent, reason: str, *, count_failure: bool = True
    ) -> None:
        """Synchronous-path failure: safe to call ``on_failure`` directly — we
        are already on the caller's own thread."""
        with self._lock:
            if count_failure:
                self.failure_count += 1
            self.last_failure = reason
        _log.warning(
            "notification failed channel=%s event_type=%s error=%s",
            self._inner.channel,
            event.event_type,
            reason,
        )
        if self._on_failure is not None:
            try:
                self._on_failure(event, reason)
            except Exception:
                _log.exception("on_failure callback raised; ignoring")

    # ----------------------------------------------------------------- deferred
    def _enqueue(self, event: NotificationEvent) -> None:
        assert self._queue is not None
        try:
            self._queue.put_nowait(event)
            return
        except queue.Full:
            pass
        # Full: drop the oldest, never block the producer.
        try:
            self._queue.get_nowait()
            with self._lock:
                self.dropped_count += 1
        except queue.Empty:  # pragma: no cover - the drain thread raced us to it
            pass
        try:
            self._queue.put_nowait(event)
        except queue.Full:  # pragma: no cover - the drain thread raced us to a slot
            with self._lock:
                self.dropped_count += 1

    def _drain_loop(self) -> None:
        assert self._queue is not None
        while True:
            try:
                event = self._queue.get(timeout=_DRAIN_POLL_SECONDS)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                delivered = self._inner.send(event)
            except Exception as exc:
                self._record_deferred_failure(event, str(exc))
                continue
            with self._lock:
                if delivered:
                    self.success_count += 1
                else:
                    self.failure_count += 1
            if not delivered:
                self._record_deferred_failure(event, "notifier returned False", count_failure=False)

    def _record_deferred_failure(
        self, event: NotificationEvent, reason: str, *, count_failure: bool = True
    ) -> None:
        """Drain-thread failure: never call ``on_failure`` from here directly.

        ``on_failure`` typically closes over a ``sqlite3`` connection owned by
        another thread. Queue the failure instead; :meth:`_pump_failures`
        forwards it from a thread that is actually entitled to use that
        connection.
        """
        with self._lock:
            if count_failure:
                self.failure_count += 1
            self.last_failure = reason
            self._pending_failures.append((event, reason))
        _log.warning(
            "deferred notification failed channel=%s event_type=%s error=%s",
            self._inner.channel,
            event.event_type,
            reason,
        )

    def _pump_failures(self) -> None:
        """Forward drain-thread failures via ``on_failure``, on the calling thread.

        Called from :meth:`send` (so a busy notifier keeps draining its own
        backlog) and from :meth:`close` (so nothing is lost at shutdown).
        """
        with self._lock:
            pending, self._pending_failures = self._pending_failures, []
        if self._on_failure is None or not pending:
            return
        for event, reason in pending:
            try:
                self._on_failure(event, reason)
            except Exception:
                _log.exception("on_failure callback raised; ignoring")

    def close(self, timeout: float = 5.0) -> None:
        """Stop the drain thread once its queue is empty, then flush failures.

        A no-op in synchronous mode. Safe to call more than once. Callers with
        a deferred ``SafeNotifier`` (:class:`~common.engine.engine.TradingEngine`)
        call this once at shutdown so a run's last few notifications are not
        silently abandoned when the process exits.
        """
        if not self._deferred:
            return
        self._stop.set()
        if self._drain_thread is not None:
            self._drain_thread.join(timeout=timeout)
        self._pump_failures()
