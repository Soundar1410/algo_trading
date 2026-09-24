"""ISO-week arithmetic for ``wsr1_weekly_stochrsi`` — pure, no clock.

A week is identified by ``(iso_year, iso_week)``, the same key
:attr:`~.models.WeeklyBar.iso_key` carries. Every "N weeks since" in the spec
is counted in these weeks (spec 4.10 and 4.11, v1.2f), and so is the 6-month
look-back of spec 4.4 (v1.2e). The arithmetic goes through each week's Monday,
so an ISO year with 53 weeks (2026, for one) needs no special case.

Deliberately separate from ``weekly_bars.py``: that module imports logging and
the IST time utilities, and ``indicators.py`` and ``rules.py`` are held to a
strict import list (spec section 13).
"""

from __future__ import annotations

from datetime import date, timedelta

WeekKey = tuple[int, int]


def monday_of(week: WeekKey) -> date:
    """The Monday of an ISO week.

    Raises:
        ValueError: the week does not exist (e.g. week 53 of a 52-week year).
    """
    iso_year, iso_week = week
    return date.fromisocalendar(iso_year, iso_week, 1)


def week_of(day: date) -> WeekKey:
    """The ISO week containing ``day``."""
    iso = day.isocalendar()
    return (iso.year, iso.week)


def shift(week: WeekKey, weeks: int) -> WeekKey:
    """The ISO week ``weeks`` after ``week`` (before it, when negative)."""
    return week_of(monday_of(week) + timedelta(weeks=weeks))


def weeks_between(earlier: WeekKey, later: WeekKey) -> int:
    """How many ISO weeks ``later`` is after ``earlier`` (negative if before)."""
    return (monday_of(later) - monday_of(earlier)).days // 7
