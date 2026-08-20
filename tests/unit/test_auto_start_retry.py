"""orchestration.auto_start.retry: what gets retried, and how the waiting stops.

The classification half of this file is the safety-relevant one. "Keep trying
until it works" applied to a wrong PIN is an account lockout, so every terminal
case below is a case that must cost exactly one request to Dhan.
"""

from __future__ import annotations

import socket
import threading
import urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.authentication import (
    InvalidCredentialsError,
    MissingCredentialsError,
    TokenGenerationError,
    TokenRateLimitedError,
    TokenRejectedRecentlyError,
)
from common.config import ConfigError
from common.config.paths import ProjectRootError
from orchestration.auto_start.retry import (
    DeadlineWaiter,
    ProjectUnavailableError,
    Retryability,
    TerminalStartupError,
    classify,
)

IST = ZoneInfo("Asia/Kolkata")
START = datetime(2026, 8, 20, 9, 0, tzinfo=IST)


class _FakeClock:
    """A clock that only moves when something waits on it."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _RecordingEvent(threading.Event):
    """A stop event whose waits advance the fake clock instead of sleeping."""

    def __init__(self, clock: _FakeClock, *, set_after: int | None = None) -> None:
        super().__init__()
        self._clock = clock
        self._set_after = set_after
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:  # type: ignore[override]
        self.waits.append(float(timeout or 0))
        self._clock.advance(float(timeout or 0))
        if self._set_after is not None and len(self.waits) >= self._set_after:
            self.set()
        return self.is_set()


# ------------------------------------------------------------- classification
@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(MissingCredentialsError("no creds"), id="missing-credentials"),
        pytest.param(InvalidCredentialsError("bad pin"), id="invalid-pin-or-totp"),
        pytest.param(ConfigError("malformed yaml"), id="malformed-config"),
        pytest.param(TerminalStartupError("live gate is on"), id="live-safety-refusal"),
        pytest.param(TerminalStartupError("legacy detected"), id="legacy-active"),
        pytest.param(KeyError("unknown runtime"), id="unsupported-runtime-id"),
    ],
)
def test_terminal_failures_are_never_retried(exc: BaseException):
    assert classify(exc) is Retryability.TERMINAL


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(TokenRateLimitedError("slow down"), id="provider-rate-limit"),
        pytest.param(TokenRejectedRecentlyError("cooldown"), id="rejection-cooldown"),
    ],
)
def test_rate_limiting_waits_for_its_boundary_rather_than_retrying_freely(exc: BaseException):
    assert classify(exc) is Retryability.COOLDOWN


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(ConnectionError("no route to host"), id="no-network"),
        pytest.param(socket.gaierror("name resolution failed"), id="dns-failure"),
        pytest.param(TimeoutError("connect timed out"), id="connection-timeout"),
        pytest.param(urllib.error.URLError("unreachable"), id="url-error"),
        pytest.param(TokenGenerationError("dhan 503"), id="transient-auth-endpoint"),
        pytest.param(ProjectUnavailableError("volume not mounted"), id="delayed-mount"),
        pytest.param(ProjectRootError("root missing"), id="project-root-missing"),
    ],
)
def test_transient_infrastructure_failures_are_retried(exc: BaseException):
    assert classify(exc) is Retryability.RETRYABLE


def test_a_5xx_is_transient_but_a_401_is_not():
    server = urllib.error.HTTPError("u", 503, "busy", {}, None)  # type: ignore[arg-type]
    client = urllib.error.HTTPError("u", 401, "denied", {}, None)  # type: ignore[arg-type]
    assert classify(server) is Retryability.RETRYABLE
    assert classify(client) is Retryability.TERMINAL


def test_an_unrecognised_exception_fails_closed():
    """A bug must surface today, not hide behind a retry loop until 15:15."""
    assert classify(ValueError("something nobody classified")) is Retryability.TERMINAL


# -------------------------------------------------------------------- waiting
def _waiter(clock: _FakeClock, event: threading.Event, **kwargs) -> DeadlineWaiter:
    params = {
        "interval_seconds": 30.0,
        "max_interval_seconds": 300.0,
        "multiplier": 2.0,
        "clock": clock,
        "stop_event": event,
    }
    params.update(kwargs)
    return DeadlineWaiter(**params)


def test_backoff_grows_and_is_capped():
    clock = _FakeClock()
    event = _RecordingEvent(clock)
    waiter = _waiter(clock, event)
    deadline = START + timedelta(hours=6)

    for _ in range(6):
        assert waiter.wait(deadline=deadline)

    assert waiter.waits[:4] == [30.0, 60.0, 120.0, 240.0]
    assert max(waiter.waits) == 300.0  # capped, never unbounded


def test_there_is_no_busy_loop():
    """Every wait is a real interval — never zero, never a spin."""
    clock = _FakeClock()
    event = _RecordingEvent(clock)
    waiter = _waiter(clock, event)
    deadline = START + timedelta(hours=6)
    for _ in range(5):
        waiter.wait(deadline=deadline)
    assert all(delay >= 30.0 for delay in waiter.waits)


def test_a_zero_interval_is_refused_outright():
    with pytest.raises(ValueError, match="busy loop"):
        DeadlineWaiter(
            interval_seconds=0.0,
            max_interval_seconds=1.0,
            multiplier=1.0,
            clock=_FakeClock(),
        )


def test_waiting_stops_at_the_session_deadline():
    clock = _FakeClock()
    event = _RecordingEvent(clock)
    waiter = _waiter(clock, event)
    deadline = START + timedelta(seconds=100)

    assert waiter.wait(deadline=deadline)  # 30s
    assert waiter.wait(deadline=deadline)  # 60s -> now at 90s
    # Only 10s remain: the wait is truncated to the boundary, not skipped, so
    # one final attempt still happens right at it.
    assert waiter.wait(deadline=deadline)
    assert waiter.waits[-1] == pytest.approx(10.0)
    assert not waiter.wait(deadline=deadline)


def test_sigterm_interrupts_a_wait_promptly():
    """The stop event is what a SIGTERM handler sets; a wait in progress must
    end on it rather than after the remaining backoff."""
    clock = _FakeClock()
    event = _RecordingEvent(clock, set_after=1)
    waiter = _waiter(clock, event)
    assert not waiter.wait(deadline=START + timedelta(hours=6))
    assert waiter.stopped


def test_an_already_stopped_waiter_never_waits_at_all():
    clock = _FakeClock()
    event = _RecordingEvent(clock)
    event.set()
    waiter = _waiter(clock, event)
    assert not waiter.wait(deadline=START + timedelta(hours=6))
    assert waiter.waits == []


def test_a_cooldown_wait_targets_the_boundary_not_the_backoff():
    clock = _FakeClock()
    event = _RecordingEvent(clock)
    waiter = _waiter(clock, event)
    ready_at = START + timedelta(seconds=600)
    assert waiter.wait_until(ready_at, deadline=START + timedelta(hours=6))
    assert waiter.waits == [600.0]


def test_a_cooldown_wait_is_still_bounded_by_the_deadline():
    clock = _FakeClock()
    event = _RecordingEvent(clock)
    waiter = _waiter(clock, event)
    deadline = START + timedelta(seconds=100)
    waiter.wait_until(START + timedelta(seconds=600), deadline=deadline)
    assert waiter.waits == [100.0]


def test_reset_returns_to_the_base_interval():
    clock = _FakeClock()
    event = _RecordingEvent(clock)
    waiter = _waiter(clock, event)
    deadline = START + timedelta(hours=6)
    waiter.wait(deadline=deadline)
    waiter.wait(deadline=deadline)
    waiter.reset()
    waiter.wait(deadline=deadline)
    assert waiter.waits[-1] == 30.0
