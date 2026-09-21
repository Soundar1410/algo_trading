"""Phase 1. Run deadline, request throttle and the token-life assertion
(spec 6.1, 10.1, 10.3).

Nothing here sleeps or reads a real clock: both primitives take injectable
``monotonic``/``sleep``, which is the only way a rate-limiter test can be both
honest and instant.

``test_a_token_minted_at_0900_still_covers_the_friday_1800_fetch`` and its
neighbours are spec item 6 — "Phase 1 asserts the remaining life explicitly
rather than assuming Dhan keeps issuing 24-hour tokens". They read the JWT
``exp`` claim, never the cache file's ``expiry_time`` field, which is written
without a timezone marker while its neighbouring ``created_at`` carries one.
"""

from __future__ import annotations

import base64
import json

import pytest

from common.authentication.token_cache import StoredToken
from strategies.positional_stocks.wsr1_weekly_stochrsi.pacing import (
    DEFAULT_DECIDE_DEADLINE_MINUTES,
    DEFAULT_FETCH_DEADLINE_MINUTES,
    DEFAULT_REQUESTS_PER_SECOND,
    RequestThrottle,
    RunDeadline,
    RunDeadlineExceeded,
    TokenLifeError,
    require_remaining_life,
)


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        """Stand-in for ``time.sleep``: advances the same clock, instantly."""
        self.now += seconds


# =============================================================== RunDeadline
def test_the_spec_deadlines_are_twenty_and_five_minutes() -> None:
    assert DEFAULT_FETCH_DEADLINE_MINUTES == 20
    assert DEFAULT_DECIDE_DEADLINE_MINUTES == 5


def test_a_fresh_deadline_has_its_whole_budget_left() -> None:
    clock = _Clock()
    deadline = RunDeadline.of_minutes(20, monotonic=clock)

    assert deadline.total_seconds == 1200.0
    assert deadline.remaining() == 1200.0
    assert deadline.elapsed() == 0.0
    assert deadline.expired is False


def test_remaining_falls_as_the_clock_advances() -> None:
    clock = _Clock()
    deadline = RunDeadline.of_minutes(20, monotonic=clock)

    clock.advance(300.0)

    assert deadline.elapsed() == 300.0
    assert deadline.remaining() == 900.0


def test_check_passes_while_the_budget_holds() -> None:
    clock = _Clock()
    deadline = RunDeadline.of_minutes(20, monotonic=clock)
    clock.advance(1199.0)

    deadline.check("fetching RELIANCE")  # does not raise


def test_check_raises_once_the_budget_is_gone_and_names_the_step() -> None:
    """Fail closed: the run reports rather than deciding on a partially
    refreshed universe."""
    clock = _Clock()
    deadline = RunDeadline.of_minutes(20, monotonic=clock)
    clock.advance(1200.0)

    with pytest.raises(RunDeadlineExceeded) as caught:
        deadline.check("fetching RELIANCE")

    assert "fetching RELIANCE" in str(caught.value)
    assert "1200" in str(caught.value)
    assert "Failing closed" in str(caught.value)


def test_remaining_is_floored_at_zero_rather_than_going_negative() -> None:
    clock = _Clock()
    deadline = RunDeadline.of_minutes(1, monotonic=clock)

    clock.advance(600.0)

    assert deadline.remaining() == 0.0
    assert deadline.expired is True


def test_a_non_positive_budget_is_refused() -> None:
    for bad in (0, -1):
        with pytest.raises(ValueError, match="must be positive"):
            RunDeadline(bad)


def test_the_deadline_is_monotonic_so_a_clock_change_cannot_extend_it() -> None:
    """A wall-clock adjustment mid-run must not hand the run more time. This is
    why the budget is measured on ``time.monotonic``, not ``time.time``."""
    clock = _Clock()
    deadline = RunDeadline.of_minutes(5, monotonic=clock)

    clock.advance(301.0)

    assert deadline.expired is True


# ============================================================ RequestThrottle
def test_the_default_rate_leaves_headroom_under_dhans_documented_limit() -> None:
    """Dhan documents 5 requests/second for the Data API; spec 6.1 asks for 3."""
    assert DEFAULT_REQUESTS_PER_SECOND == 3
    assert RequestThrottle().max_per_second == 3


def test_the_first_requests_up_to_the_rate_do_not_wait() -> None:
    clock = _Clock()
    throttle = RequestThrottle(3, monotonic=clock, sleep=clock.sleep)

    for _ in range(3):
        throttle()

    assert throttle.total_wait_seconds == 0.0


def test_the_fourth_request_in_a_second_waits_for_the_window_to_slide() -> None:
    clock = _Clock()
    throttle = RequestThrottle(3, monotonic=clock, sleep=clock.sleep)

    for _ in range(3):
        throttle()
    clock.advance(0.2)
    throttle()

    assert throttle.total_wait_seconds == pytest.approx(0.8)


def test_a_slow_caller_is_never_made_to_wait() -> None:
    """A fixed ``sleep(1/rate)`` between calls would pay the delay even when the
    previous call already took longer than the interval. Over 201 symbols that
    is minutes of avoidable waiting."""
    clock = _Clock()
    throttle = RequestThrottle(3, monotonic=clock, sleep=clock.sleep)

    for _ in range(10):
        throttle()
        clock.advance(2.0)

    assert throttle.total_wait_seconds == 0.0


def test_the_observed_rate_never_exceeds_the_cap() -> None:
    """The property that matters: across a burst of 30 requests, no one-second
    window ever contains more than the configured number."""
    clock = _Clock()
    throttle = RequestThrottle(3, monotonic=clock, sleep=clock.sleep)
    stamps: list[float] = []

    for _ in range(30):
        throttle()
        stamps.append(clock.now)

    for i, start in enumerate(stamps):
        within = [s for s in stamps[i:] if s < start + 1.0]
        assert len(within) <= 3, f"{len(within)} requests inside one second from {start}"


def test_thirty_requests_at_three_per_second_take_about_nine_seconds() -> None:
    clock = _Clock()
    throttle = RequestThrottle(3, monotonic=clock, sleep=clock.sleep)
    started = clock.now

    for _ in range(30):
        throttle()

    assert clock.now - started == pytest.approx(9.0)


def test_a_rate_of_one_serialises_requests_a_second_apart() -> None:
    clock = _Clock()
    throttle = RequestThrottle(1, monotonic=clock, sleep=clock.sleep)
    started = clock.now

    for _ in range(4):
        throttle()

    assert clock.now - started == pytest.approx(3.0)


def test_a_non_positive_rate_is_refused() -> None:
    for bad in (0, -3):
        with pytest.raises(ValueError, match="must be positive"):
            RequestThrottle(bad)


def test_the_throttle_is_callable_as_the_clients_before_request_hook() -> None:
    """It is passed straight to ``DhanHistoricalDataClient(before_request=...)``,
    so retries are throttled too."""
    clock = _Clock()
    throttle = RequestThrottle(3, monotonic=clock, sleep=clock.sleep)

    assert callable(throttle)
    throttle()  # the client calls it with no arguments


# ========================================================== token remaining life
def _token(*, exp: int | None, now: float = 0.0) -> StoredToken:
    """A syntactically real JWT carrying only the claim under test.

    Not a real credential: three dot-separated base64url segments with a
    throwaway signature, which is all ``decode_token_exp`` reads.
    """
    claims: dict[str, object] = {"iss": "test"}
    if exp is not None:
        claims["exp"] = exp
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return StoredToken(
        access_token=f"header.{payload}.signature",
        client_id="client-1",
        created_at="2026-09-18T03:31:14+00:00",
        expiry_time="2026-09-19T09:01:14.904",
    )


def test_a_token_minted_at_0900_still_covers_the_friday_1800_fetch() -> None:
    """Spec 10.3: the Friday-18:00 fetch job reuses the day's 09:00 token,
    "with roughly 15 hours of life left". Asserted, not assumed."""
    import time as _time

    fifteen_hours = 15 * 3600
    token = _token(exp=int(_time.time()) + fifteen_hours)

    remaining = require_remaining_life(token, 10 * 3600)

    assert remaining == pytest.approx(fifteen_hours, abs=5)


def test_a_token_with_too_little_life_left_is_refused() -> None:
    """This is the assertion's whole point: if Dhan ever shortens the token
    lifetime, the fetch job says so rather than dying mid-run."""
    import time as _time

    token = _token(exp=int(_time.time()) + 600)

    with pytest.raises(TokenLifeError) as caught:
        require_remaining_life(token, 4 * 3600)

    assert "do not assume 24 hours" in str(caught.value)


def test_an_already_expired_token_is_refused() -> None:
    import time as _time

    token = _token(exp=int(_time.time()) - 3600)

    with pytest.raises(TokenLifeError):
        require_remaining_life(token, 60)


def test_an_undeterminable_expiry_passes_rather_than_discarding_a_good_token() -> None:
    """Matches ``StoredToken.is_usable``: "cannot tell" is not "expired". A
    genuinely dead token is rejected by the first API call, which is cheaper
    than a needless regeneration against a two-minute rate limit."""
    assert require_remaining_life(_token(exp=None), 10 * 3600) == -1.0
    assert require_remaining_life(StoredToken("not-a-jwt", "c", "", None), 10 * 3600) == -1.0


def test_the_remaining_life_comes_from_the_jwt_claim_not_the_expiry_time_field() -> None:
    """Runbook D93 finding (a): the cache file writes ``expiry_time`` without a
    timezone marker while ``created_at`` carries one, so the two are consistent
    only if the second is read as IST. The ``exp`` claim is epoch seconds and
    has no such ambiguity, so this check reads that instead — and a wildly
    contradictory ``expiry_time`` changes nothing.
    """
    import time as _time

    token = StoredToken(
        access_token=_token(exp=int(_time.time()) + 15 * 3600).access_token,
        client_id="client-1",
        created_at="2026-09-18T03:31:14+00:00",
        # Deliberately absurd and timezone-less. If this field were consulted,
        # the assertion below would fail.
        expiry_time="1999-01-01T00:00:00.000",
    )

    assert require_remaining_life(token, 10 * 3600) == pytest.approx(15 * 3600, abs=5)
