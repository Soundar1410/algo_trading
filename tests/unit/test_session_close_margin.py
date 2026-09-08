"""``seconds_until_session_close`` — the token-lifetime figure both runtimes ask
:class:`~common.authentication.bootstrap.AuthBootstrap` for.

Why this function exists at all is a real incident, not a hypothetical. On
2026-09-08 the cached Dhan token had ~950 s of life at the 09:00 start. That is
comfortably over ``DEFAULT_EXPIRY_MARGIN_SECONDS`` (300 s), so it was accepted;
it expired at 09:15:54, fifteen minutes into the session. Every worker that
restarted after that got HTTP 401 on its history fetch, warmed up ``COLD_START``
and — for a ``continuity_required`` strategy — was blocked from entering for the
rest of the day. ``st05_supertrend_buy`` and ``st12_supertrend_buy`` both lost
the session that way.

A 24-hour token's usefulness depends entirely on *when* it was minted, so the
only honest question at startup is "does this one outlast today's session?".
That is what this function answers.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from common.utils.timeutils import (
    MARKET_CLOSE_TIME,
    NaiveDatetimeError,
    seconds_until_session_close,
)

IST = ZoneInfo("Asia/Kolkata")
GRACE = 15 * 60  # what both runtimes pass, mirroring SESSION_DEADLINE_GRACE_SECONDS


def _ist(hour: int, minute: int = 0) -> datetime:
    """A moment on Tuesday 2026-09-08 — the incident's own trading day."""
    return datetime(2026, 9, 8, hour, minute, tzinfo=IST)


def test_the_close_is_the_real_nse_session_end():
    assert time(15, 30) == MARKET_CLOSE_TIME


def test_a_market_open_start_demands_most_of_the_trading_day():
    """09:00, the auto-start trigger: 6 h 30 m of session plus the 15 m grace."""
    assert seconds_until_session_close(now=_ist(9, 0), grace_seconds=GRACE) == (
        int(6.5 * 3600) + GRACE
    )


def test_the_figure_shrinks_as_the_day_runs_on():
    """A late start must not demand a whole session it will never use — that
    would regenerate a token needlessly against Dhan's rate limit."""
    morning = seconds_until_session_close(now=_ist(9, 0), grace_seconds=GRACE)
    midday = seconds_until_session_close(now=_ist(12, 0), grace_seconds=GRACE)
    late = seconds_until_session_close(now=_ist(14, 30), grace_seconds=GRACE)
    assert morning > midday > late > 0
    assert late == 3600 + GRACE  # 14:30 -> 15:30 close, plus grace


def test_it_never_goes_negative_after_the_close():
    """Floored at zero so a caller can use it as a lower bound with
    ``max(DEFAULT_EXPIRY_MARGIN_SECONDS, ...)`` and never accidentally *weaken*
    the ordinary guard on an evening or weekend run."""
    assert seconds_until_session_close(now=_ist(15, 46), grace_seconds=GRACE) == 0
    assert seconds_until_session_close(now=_ist(20, 47), grace_seconds=GRACE) == 0
    assert seconds_until_session_close(now=_ist(23, 59), grace_seconds=GRACE) == 0


def test_the_grace_extends_past_the_close_itself():
    """The token must outlive the run's own hard deadline, not merely the last
    tick — the supervisor keeps working past 15:30 to square off and shut down."""
    at_close = seconds_until_session_close(now=_ist(15, 30), grace_seconds=GRACE)
    assert at_close == GRACE


def test_the_incident_figure_is_refused_and_a_fresh_token_is_not():
    """The whole point, expressed in the incident's own numbers: at 09:00 the
    required lifetime must exceed the 950 s the dead token had, and must not
    exceed what a freshly minted 24-hour token offers."""
    required = seconds_until_session_close(now=_ist(9, 0), grace_seconds=GRACE)
    assert required > 950, "the token that died mid-session would still be accepted"
    assert required < 24 * 3600, "a freshly minted 24h token must still satisfy this"


def test_a_naive_now_is_refused_rather_than_guessed():
    """Same discipline as ``local_time_in``: interpreting a naive datetime as
    system-local is the class of bug this module exists to prevent."""
    with pytest.raises(NaiveDatetimeError):
        seconds_until_session_close(now=datetime(2026, 9, 8, 9, 0))


def test_a_utc_now_is_converted_not_read_literally():
    """A tick-shaped UTC instant must resolve to its IST wall clock. 03:30 UTC
    is 09:00 IST, so it must produce the same answer as the IST spelling — the
    UTC-vs-IST confusion that has already caused a real entry-gate bug here."""
    from datetime import UTC

    utc_0900_ist = datetime(2026, 9, 8, 3, 30, tzinfo=UTC)
    assert seconds_until_session_close(now=utc_0900_ist, grace_seconds=GRACE) == (
        seconds_until_session_close(now=_ist(9, 0), grace_seconds=GRACE)
    )
