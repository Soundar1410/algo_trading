"""Per-run deadline, request throttle and token-life check (spec 6.1, 10.1, 10.3).

Three small process-local primitives the weekly run needs and the platform does
not already have.

**These are not the live-trading rate limiter.**
``common.broker.live_rate_limiter.LiveOrderRateLimiter`` is account-wide,
cross-process and database-backed, because two live workers must not
independently believe they have order capacity. Nothing here coordinates across
processes and nothing here touches an order: this is one batch job pacing its
own reads of a market-data endpoint, and the weekly job takes a process lock so
only one of it exists at a time. Using the live limiter for this would put
market-data reads into the live order-rate ledger, which is the opposite of
what that table is for.

**Why the deadline is not the client's timeout.**
:class:`~common.market_data.dhan_historical.DhanHistoricalDataClient` bounds a
single call and a single call's retries. It cannot bound a run: 201 symbols
times worst-case retries and backoff is hours, all of it made of individually
well-behaved calls. Spec 6.1 gives the *run* 20 minutes for ``fetch`` and 5 for
``decide``, and on expiry the run fails closed rather than deciding on a
partially refreshed universe.
"""

from __future__ import annotations

import time as _time
from collections import deque
from collections.abc import Callable

from common.authentication.token_cache import StoredToken

#: Spec 6.1. Dhan documents 5 requests/second for the Data API; 3 leaves
#: headroom, and the weekend fetch window is the only time this job runs, when
#: no intraday runtime is competing for the same account-wide budget.
DEFAULT_REQUESTS_PER_SECOND = 3

#: Spec 6.1 / 10.1, in minutes.
DEFAULT_FETCH_DEADLINE_MINUTES = 20
DEFAULT_DECIDE_DEADLINE_MINUTES = 5


class RunDeadlineExceeded(RuntimeError):
    """The run exhausted its wall-clock budget. Fail closed; decide nothing."""


class RunDeadline:
    """A wall-clock budget for one run, checked at the caller's own checkpoints.

    Monotonic by construction, so a clock adjustment mid-run cannot extend or
    collapse the budget. ``monotonic`` is injectable purely so tests need no
    real time.

    This never interrupts anything: it is checked between units of work, which
    is what makes "fail closed and report" a clean outcome rather than a
    half-written cache.
    """

    def __init__(
        self,
        total_seconds: float,
        *,
        monotonic: Callable[[], float] = _time.monotonic,
    ) -> None:
        if total_seconds <= 0:
            raise ValueError(f"total_seconds must be positive, got {total_seconds}")
        self._total = float(total_seconds)
        self._monotonic = monotonic
        self._started = monotonic()

    @classmethod
    def of_minutes(
        cls, minutes: float, *, monotonic: Callable[[], float] = _time.monotonic
    ) -> RunDeadline:
        return cls(minutes * 60.0, monotonic=monotonic)

    @property
    def total_seconds(self) -> float:
        return self._total

    def elapsed(self) -> float:
        return self._monotonic() - self._started

    def remaining(self) -> float:
        """Seconds left, floored at zero."""
        return max(0.0, self._total - self.elapsed())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def check(self, step: str) -> None:
        """Raise if the budget is gone, naming what was about to happen.

        Raises:
            RunDeadlineExceeded: the budget is exhausted.
        """
        if self.expired:
            raise RunDeadlineExceeded(
                f"Run deadline of {self._total:.0f}s exceeded before {step} "
                f"(elapsed {self.elapsed():.1f}s). Failing closed."
            )


class RequestThrottle:
    """Caps outbound requests at ``max_per_second``, sleeping only when needed.

    A sliding window over the last ``max_per_second`` request instants: a new
    request waits exactly until the oldest of them is a second old, and not a
    moment longer. A fixed ``sleep(1/rate)`` between calls would instead pay the
    delay even when the previous call took longer than the interval, which over
    201 symbols is minutes of avoidable waiting.

    Passed to the historical client as ``before_request``, so it is applied to
    retries too — a throttle wrapped around the fetch would miss exactly the
    burst a 429 provokes.

    Not thread-safe, and deliberately so: the weekly run is single-threaded and
    holds a process lock. ``monotonic``/``sleep`` are injectable so tests never
    sleep for real.
    """

    def __init__(
        self,
        max_per_second: int = DEFAULT_REQUESTS_PER_SECOND,
        *,
        monotonic: Callable[[], float] = _time.monotonic,
        sleep: Callable[[float], None] = _time.sleep,
    ) -> None:
        if max_per_second <= 0:
            raise ValueError(f"max_per_second must be positive, got {max_per_second}")
        self._max = int(max_per_second)
        self._monotonic = monotonic
        self._sleep = sleep
        self._recent: deque[float] = deque(maxlen=self._max)
        #: Total seconds this throttle has spent waiting — reported at the end
        #: of a run, so "the fetch took 14 minutes" can be separated into work
        #: and deliberate pacing.
        self.total_wait_seconds = 0.0

    @property
    def max_per_second(self) -> int:
        return self._max

    def __call__(self) -> None:
        """Block until another request is allowed, then record it."""
        if len(self._recent) == self._max:
            earliest = self._recent[0]
            wait = 1.0 - (self._monotonic() - earliest)
            if wait > 0:
                self._sleep(wait)
                self.total_wait_seconds += wait
        self._recent.append(self._monotonic())


class TokenLifeError(RuntimeError):
    """The cached token cannot cover the run that is about to start."""


def require_remaining_life(token: StoredToken, minimum_seconds: float) -> float:
    """Assert the cached token has at least ``minimum_seconds`` of life left.

    Spec 10.3 says the Friday-18:00 fetch job reuses the day's 09:00 token,
    "with roughly 15 hours of life left", and requires Phase 1 to **assert**
    that rather than assume Dhan keeps issuing 24-hour tokens.

    The remaining life is read from the token's own JWT ``exp`` claim, via
    :meth:`~common.authentication.token_cache.StoredToken.seconds_until_expiry`
    — never from the cache file's ``expiry_time`` field, which is written
    without a timezone marker while its neighbouring ``created_at`` carries one
    (runbook D93, finding (a)). The ``exp`` claim is epoch seconds and has no
    such ambiguity.

    An **undeterminable** expiry passes. That matches ``StoredToken.is_usable``
    and is the right default here too: "cannot tell" is not "expired", a
    genuinely dead token is rejected by the first API call, and this check
    exists to catch a *shortened* token life, not to become a second place a
    good token can be thrown away.

    Returns:
        The remaining seconds, or ``-1.0`` when the expiry is undeterminable.

    Raises:
        TokenLifeError: the token expires within ``minimum_seconds``.
    """
    remaining = token.seconds_until_expiry()
    if remaining is None:
        return -1.0
    if remaining < minimum_seconds:
        raise TokenLifeError(
            f"Cached token has {remaining / 3600:.2f} h of life left, which is below the "
            f"{minimum_seconds / 3600:.2f} h this run requires. Dhan's token lifetime may "
            "have changed; do not assume 24 hours."
        )
    return remaining
