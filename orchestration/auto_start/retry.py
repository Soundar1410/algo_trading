"""Failure classification and the deadline-bounded, interruptible wait.

"Keep retrying until it works" is only ever true of *infrastructure*. A wrong
PIN retried every thirty seconds until 15:15 is not resilience — it is an
account lockout with extra steps, and Dhan's own credential-rejection cooldown
exists precisely because clients do this. So every failure lands in exactly one
of three buckets:

* :attr:`Retryability.RETRYABLE` — no network, DNS failure, connection timeout,
  a 5xx or otherwise transient auth endpoint, an unmounted ``/Volumes/Trading``.
  Retried at a capped-backoff cadence until the session deadline.
* :attr:`Retryability.COOLDOWN` — the provider is rate limiting us, or this
  machine has a recorded credential rejection still in force. Retryable in
  principle, but only *after* the cooldown boundary, and only if the deadline
  is still ahead. Never sooner: retrying inside a cooldown is what turns one
  rejection into a lockout.
* :attr:`Retryability.TERMINAL` — missing credentials, an invalid PIN or TOTP,
  malformed configuration, a live-safety refusal, legacy Trading_Automation
  detected, an unsupported runtime id, a deliberate stop. None of these get
  better by being tried again, and all of them need a human.

The waiting itself is a :class:`threading.Event` wait rather than
``time.sleep``, so a ``SIGTERM`` delivered mid-backoff returns immediately
instead of up to five minutes later.
"""

from __future__ import annotations

import socket
import ssl
import threading
import urllib.error
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum

from pydantic import ValidationError

from common.authentication import (
    AuthError,
    InvalidCredentialsError,
    MissingCredentialsError,
    TokenRateLimitedError,
    TokenRejectedRecentlyError,
)
from common.config import ConfigError
from common.config.paths import ProjectRootError
from common.logging import get_logger

_log = get_logger(__name__)


class Retryability(StrEnum):
    RETRYABLE = "retryable"
    COOLDOWN = "cooldown"
    TERMINAL = "terminal"


class TerminalStartupError(RuntimeError):
    """A refusal an operator must resolve — never retried.

    Raised by the paper-safety, legacy-guard and environment-validation steps,
    which have no transient mode: a live gate that is on stays on, and a
    detected legacy system stays detected.
    """


class ProjectUnavailableError(RuntimeError):
    """The project root or its ``.venv`` is not present yet.

    Explicitly *retryable*: at login the Trading volume routinely mounts after
    ``launchd`` has already fired.
    """


#: Transient at the socket/transport layer. ``OSError`` covers the whole
#: ``ConnectionError``/``TimeoutError``/``socket.gaierror`` family — DNS
#: failures included, since ``gaierror`` is an ``OSError`` subclass.
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    socket.timeout,
    ssl.SSLError,
    urllib.error.URLError,
)

#: Terminal regardless of what else they inherit from — checked before the
#: transport family, because ``urllib.error.HTTPError`` is a ``URLError`` and a
#: 401 is not a network problem.
_TERMINAL_ERRORS: tuple[type[BaseException], ...] = (
    MissingCredentialsError,
    InvalidCredentialsError,
    ConfigError,
    ValidationError,
    TerminalStartupError,
    KeyError,  # scripts._runtimes.resolve_runtime on an unsupported runtime id
)

_COOLDOWN_ERRORS: tuple[type[BaseException], ...] = (
    TokenRateLimitedError,
    TokenRejectedRecentlyError,
)


def classify(exc: BaseException) -> Retryability:
    """Which bucket ``exc`` belongs in.

    Order matters. Credential and configuration failures are matched first so
    that nothing later — least of all the broad transport family — can promote
    a wrong PIN into "worth another go".
    """
    if isinstance(exc, _TERMINAL_ERRORS):
        return Retryability.TERMINAL
    if isinstance(exc, _COOLDOWN_ERRORS):
        return Retryability.COOLDOWN
    if isinstance(exc, ProjectUnavailableError | ProjectRootError):
        return Retryability.RETRYABLE
    if isinstance(exc, AuthError):
        # The auth layer already publishes this per exception class
        # (common/authentication/exceptions.py); trusting its own answer keeps
        # one classification, not a second one that can drift from it.
        return Retryability.RETRYABLE if exc.retryable else Retryability.TERMINAL
    if isinstance(exc, urllib.error.HTTPError):
        return Retryability.RETRYABLE if exc.code >= 500 else Retryability.TERMINAL
    if isinstance(exc, _TRANSPORT_ERRORS):
        return Retryability.RETRYABLE
    # An unrecognised exception is a bug, not a known-transient condition.
    # Failing closed here means the operator sees it today rather than the
    # process hiding it behind a retry loop until 15:15.
    return Retryability.TERMINAL


class DeadlineWaiter:
    """Capped exponential backoff that stops at a deadline and at ``SIGTERM``.

    ``stop_event`` is set by the process's one shutdown-signal installer
    (:func:`common.process.signals.shutdown_signals`), so a wait in progress
    ends on the signal rather than after the remaining backoff.
    """

    def __init__(
        self,
        *,
        interval_seconds: float,
        max_interval_seconds: float,
        multiplier: float,
        clock: Callable[[], datetime],
        stop_event: threading.Event | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive — a zero wait is a busy loop")
        self._interval = interval_seconds
        self._max_interval = max(max_interval_seconds, interval_seconds)
        self._multiplier = max(multiplier, 1.0)
        self._clock = clock
        self._stop_event = stop_event if stop_event is not None else threading.Event()
        self._current = interval_seconds
        self.waits: list[float] = []

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def reset(self) -> None:
        """Back to the base interval — call after a successful step."""
        self._current = self._interval

    def wait(self, *, deadline: datetime) -> bool:
        """Sleep one backoff step. ``False`` if we must stop instead.

        Returns ``False`` when the stop event is set, when the deadline has
        already passed, or when the next interval would run past it — in the
        last case the wait is *truncated* to the deadline rather than skipped,
        so a final attempt still happens right at the boundary.
        """
        if self.stopped:
            return False
        remaining = (deadline - self._clock()).total_seconds()
        if remaining <= 0:
            return False

        delay = min(self._current, remaining)
        self._current = min(self._current * self._multiplier, self._max_interval)
        self.waits.append(delay)
        _log.info("auto-start retry backoff: waiting %.0fs (%.0fs to deadline)", delay, remaining)

        # Event.wait returns True when the event was set, i.e. we were
        # interrupted; a timeout returns False and means "carry on".
        interrupted = self._stop_event.wait(delay)
        return not interrupted

    def wait_until(self, moment: datetime, *, deadline: datetime) -> bool:
        """Wait out a provider cooldown boundary, still bounded by ``deadline``.

        Used for :attr:`Retryability.COOLDOWN`: the retry cadence is dictated
        by the provider, not by our own backoff, so this waits exactly as long
        as it must and no longer.
        """
        if self.stopped:
            return False
        target = min(moment, deadline)
        delay = (target - self._clock()).total_seconds()
        if delay <= 0:
            return target >= self._clock()
        self.waits.append(delay)
        _log.info("auto-start honouring a provider cooldown: waiting %.0fs", delay)
        return not self._stop_event.wait(delay)
