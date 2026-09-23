"""Phase 1 review. The week's expected last session (spec 6.2 v1.2b).

What is being pinned is a *direction*: the expected last session comes from the
calendar and never from the data. Phase 1 had it the other way round, and the
Phase 1 review showed what that costs — 21 September 2026 was an NSE trading
day, yet Dhan had not published its daily candle by 22:20 IST, so on a Friday
the data would have answered "Thursday", every series would have agreed, and a
Monday-to-Thursday bar would have been decided on with nothing raising.

**These tests run against the real committed holiday list**, not a fixture. A
spec 6.2 case that passes only against invented holidays proves nothing about
next Friday, and the list in ``config/global.yaml`` is the one the platform
actually runs on. The 2026 facts used below, all verified against it:

  * 2026-09-14 (Mon) Ganesh Chaturthi — the *only* September holiday, which is
    why 21 Sep was a trading day;
  * 2026-04-03 (Fri) Good Friday, and 2026-03-31 (Tue) in the same ISO week;
  * 2026-10-02 (Fri), 2026-12-25 (Fri) — two more holiday Fridays;
  * 2026-11-08 (Sun) Diwali Laxmi Pujan, the Muhurat session.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import (
    CalendarError,
    TradingCalendar,
    saturday_of,
)

CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture
def calendar() -> TradingCalendar:
    return TradingCalendar.from_config(CONFIG_ROOT)


# --------------------------------------------------------- the expected session
@pytest.mark.parametrize(
    "week,expected,why",
    [
        ((2026, 38), date(2026, 9, 18), "ordinary week: the Friday"),
        ((2026, 39), date(2026, 9, 25), "ordinary week: the Friday"),
        ((2026, 14), date(2026, 4, 2), "Good Friday 04-03 closed: the Thursday"),
        ((2026, 18), date(2026, 4, 30), "Maharashtra Day 05-01 closed: the Thursday"),
        ((2026, 26), date(2026, 6, 25), "Muharram 06-26 closed: the Thursday"),
        ((2026, 40), date(2026, 10, 1), "Gandhi Jayanti 10-02 closed: the Thursday"),
        ((2026, 52), date(2026, 12, 24), "Christmas 12-25 closed: the Thursday"),
        ((2026, 45), date(2026, 11, 6), "Sunday Muhurat does not move the Friday"),
    ],
)
def test_the_expected_last_session_is_the_weeks_last_non_holiday_weekday(
    calendar: TradingCalendar, week: tuple[int, int], expected: date, why: str
) -> None:
    assert calendar.expected_last_session(week) == expected, why


def test_a_monday_holiday_does_not_move_the_expected_session(
    calendar: TradingCalendar,
) -> None:
    """2026-W38 is the week of the Phase 1 acceptance run. Its Monday is Ganesh
    Chaturthi; its expected last session is still the Friday."""
    assert calendar.is_trading_day(date(2026, 9, 14)) is False
    assert calendar.expected_last_session((2026, 38)) == date(2026, 9, 18)


def test_the_expected_session_always_lands_inside_its_own_week(
    calendar: TradingCalendar,
) -> None:
    """Swept across every ISO week of 2026: the walk must never return a date
    from a neighbouring week, which is the failure mode the containment guard
    in ``expected_last_session`` exists to catch."""
    for iso_week in range(1, 54):
        week = (2026, iso_week)
        session = calendar.expected_last_session(week)
        assert session.isocalendar()[:2] == week
        assert session.isoweekday() <= 5
        assert calendar.is_trading_day(session) is True


def test_a_week_with_no_trading_weekday_raises_rather_than_leaving_the_week() -> None:
    """The alternative to raising is returning the previous week's Friday,
    which would silently pass a stale series as current."""
    calendar = TradingCalendar.from_holidays(
        ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]
    )

    with pytest.raises(CalendarError, match="no trading weekday"):
        calendar.expected_last_session((2026, 38))


def test_an_unmaintained_year_fails_closed_rather_than_open(
    calendar: TradingCalendar,
) -> None:
    """The holiday list is annual. With no 2027 list every 2027 holiday Friday
    is "expected", so that week's index series never contains it and the run
    stops. Noisy, never wrong — this pins the direction."""
    assert calendar.expected_last_session((2027, 1)) == date(2027, 1, 8)
    assert calendar.is_trading_day(date(2027, 1, 26)) is True  # Republic Day, unlisted


# ----------------------------------------------------- sessions off the calendar
def test_a_sunday_muhurat_session_is_reported_and_moves_nothing(
    calendar: TradingCalendar,
) -> None:
    """Spec 6.2: a session on a listed holiday is included in its ISO week and
    reported; the expected last session is unchanged."""
    sessions = [date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 8)]

    assert calendar.unlisted_sessions(sessions, (2026, 45)) == [date(2026, 11, 8)]
    assert calendar.expected_last_session((2026, 45)) == date(2026, 11, 6)


def test_a_saturday_special_session_is_reported_the_same_way(
    calendar: TradingCalendar,
) -> None:
    """Budget Saturdays have happened. 2026-08-15 is both a Saturday and a
    listed holiday, so it is doubly not a trading day."""
    assert calendar.unlisted_sessions([date(2026, 8, 15)], (2026, 33)) == [date(2026, 8, 15)]
    assert calendar.expected_last_session((2026, 33)) == date(2026, 8, 14)


def test_unlisted_sessions_ignores_sessions_from_other_weeks(
    calendar: TradingCalendar,
) -> None:
    assert calendar.unlisted_sessions([date(2026, 11, 8), date(2026, 11, 15)], (2026, 45)) == [
        date(2026, 11, 8)
    ]


def test_ordinary_sessions_are_never_reported_as_unlisted(
    calendar: TradingCalendar,
) -> None:
    assert calendar.unlisted_sessions([date(2026, 11, 5), date(2026, 11, 6)], (2026, 45)) == []


# ------------------------------------------------------------------ plumbing
def test_the_calendar_comes_from_the_one_committed_holiday_list() -> None:
    """Not a second copy. A holiday added for the intraday strategies is a
    holiday here on the same commit, because both read ``auto_start.holidays``
    in ``config/global.yaml``."""
    from common.config.loader import load_auto_start_config

    holidays = load_auto_start_config(CONFIG_ROOT).holiday_dates
    assert "2026-09-14" in holidays

    calendar = TradingCalendar.from_config(CONFIG_ROOT)
    for listed in holidays:
        day = date.fromisoformat(listed)
        assert calendar.is_trading_day(day) is False


def test_saturday_of_bounds_the_iso_week() -> None:
    for day in (date(2026, 9, 14), date(2026, 9, 17), date(2026, 9, 20)):
        saturday = saturday_of(day)
        assert saturday.isoweekday() == 6
        assert saturday.isocalendar()[:2] == day.isocalendar()[:2]


def test_the_calendar_reads_no_clock(calendar: TradingCalendar) -> None:
    """Every answer is a pure function of its argument, so a host clock in
    another zone cannot change which session a week is expected to end on.
    Asserted by repetition rather than by mocking a clock there is none of."""
    assert calendar.expected_last_session((2026, 38)) == calendar.expected_last_session((2026, 38))
    assert calendar.timezone == "Asia/Kolkata"
