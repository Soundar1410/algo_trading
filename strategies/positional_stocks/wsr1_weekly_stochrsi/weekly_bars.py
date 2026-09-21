"""Fold daily sessions into completed ISO weekly bars (spec section 3).

One bar per ISO week, built from that week's trading sessions: open from the
first session, high and low the week's extremes, close from the last session,
and ``week_ending`` the last session's date.

Kept out of ``common/warmup/`` and ``common/candles/`` by spec section 13:
``parse_timeframe_minutes`` rejects ``1d`` and ``1W``, and the whole warm-up
stack buckets by intraday minutes inside one session. Nothing here extends that
vocabulary.

Pure and clock-free
-------------------
``as_of`` is always an argument. Nothing in this module calls ``now_ist()``, so
the completed-week decision is reproducible and testable at any instant — and
a host clock in another timezone cannot change which weeks a run sees, which is
what D88+ is about.

When is a week complete?
------------------------
Spec section 3 says a week is usable only after its last session has closed.
The data alone cannot answer that: a truncated series and a genuine holiday
week look identical, so "the last bar I have is Wednesday's" can never prove
Thursday and Friday will not arrive.

So the release rule is calendar-derived: **a week is complete once ``as_of`` is
at or after 15:30 IST on the Friday of that ISO week.** It needs no holiday
calendar, and it lands correctly on every scheduled run of spec 10.3 —

===========================  ========================================
Run                          Effect
===========================  ========================================
Friday 18:00 (fetch)         the week just ended is released
Saturday / Sunday 10:00      same, idempotently
Monday 08:30 (decide)        prior weeks released, current week not
mid-week, by hand            the current week is not released
===========================  ========================================

Where a week's last session came earlier than Friday — Thursday and Friday both
holidays, say — the bar is still held until Friday 15:30. That is a delay of at
most a weekend, always in the fail-closed direction, and the Friday fetch job
resolves it before the Monday decision run ever looks.

Weekend and out-of-hours special sessions
-----------------------------------------
NSE occasionally runs a session that falls in the ISO week but **after** the
Friday-15:30 release: the Muhurat session on Diwali, and budget-day sessions
that have historically fallen on a Saturday. Such a session belongs to its ISO
week here, and is included in the bar whenever the data contains it — see
``test_a_saturday_special_session_is_included_when_present``. What this module
cannot do is revisit a week already written to the cache: a Friday-18:00 fetch
that ran before a Saturday special session will have cached that week without
it, and nothing re-reads it. That is a known limitation, recorded in the
runbook; the ``--force-refetch`` that answers it is Phase 4 work, deliberately
not built here.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta

from common.utils.timeutils import DEFAULT_TZ, combine

from .models import DailyBar, WeeklyBar

#: When the NSE cash session closes. A week is released after this time on its
#: Friday — see the module docstring for why the clock and not the data decides.
SESSION_CLOSE_IST = time(15, 30)

#: ``date.isoweekday()`` for Friday. ISO weeks run Monday(1)..Sunday(7), so the
#: Friday of a week containing ``d`` is ``d + (5 - d.isoweekday())`` days.
_FRIDAY = 5


def iso_key(day: date) -> tuple[int, int]:
    """``(iso_year, iso_week)`` for a calendar date.

    ISO year is not the calendar year at a year boundary: 2027-01-01 is a
    Friday in ISO week 2026-W53. Grouping on the calendar year would split one
    trading week into two bars, which is why this is used everywhere in place
    of ``day.year``.
    """
    iso = day.isocalendar()
    return (iso.year, iso.week)


def friday_of(day: date) -> date:
    """The Friday of the ISO week containing ``day``."""
    return day + timedelta(days=_FRIDAY - day.isoweekday())


def week_release_moment(day: date, *, tz_name: str = DEFAULT_TZ) -> datetime:
    """The instant the ISO week containing ``day`` becomes usable."""
    return combine(friday_of(day), SESSION_CLOSE_IST, tz_name)


def is_week_complete(day: date, as_of: datetime, *, tz_name: str = DEFAULT_TZ) -> bool:
    """Has the ISO week containing ``day`` closed, as of ``as_of``?

    Raises:
        ValueError: ``as_of`` is naive. A naive datetime here would be compared
            against an IST-aware release moment and raise deep inside the
            comparison; refusing it by name is clearer, and matches the
            repository's standing rule against naive datetimes.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware; a naive datetime has no IST meaning")
    return as_of >= week_release_moment(day, tz_name=tz_name)


def build_weekly_bars(
    daily: Iterable[DailyBar],
    *,
    as_of: datetime,
    tz_name: str = DEFAULT_TZ,
) -> list[WeeklyBar]:
    """Aggregate daily sessions into **completed** weekly bars, oldest first.

    Sessions may arrive in any order and are sorted here. An incomplete current
    week is dropped rather than returned partially built — spec section 3's
    "a run must never use an incomplete current week" is enforced at the only
    place that could violate it.

    Raises:
        ValueError: ``as_of`` is naive.
    """
    sessions = sorted(daily, key=lambda bar: bar.session)

    grouped: dict[tuple[int, int], list[DailyBar]] = {}
    for bar in sessions:
        grouped.setdefault(iso_key(bar.session), []).append(bar)

    bars: list[WeeklyBar] = []
    for key in sorted(grouped):
        week = grouped[key]
        if not is_week_complete(week[0].session, as_of, tz_name=tz_name):
            continue
        bars.append(_aggregate(key, week))
    return bars


def _aggregate(key: tuple[int, int], week: list[DailyBar]) -> WeeklyBar:
    iso_year, iso_week = key
    return WeeklyBar(
        # The last *session*, not the week's Friday: spec section 3 defines it
        # that way, and in a holiday week the two differ.
        week_ending=week[-1].session,
        iso_year=iso_year,
        iso_week=iso_week,
        open=week[0].open,
        high=max(bar.high for bar in week),
        low=min(bar.low for bar in week),
        close=week[-1].close,
        volume=sum(bar.volume for bar in week),
        sessions=len(week),
    )


def last_session_of_week(daily: Iterable[DailyBar], week: tuple[int, int]) -> date | None:
    """The latest session this series has inside ``week``, or ``None``.

    Spec 6.2's staleness check is "does every used series contain week W's last
    session". The reference answer comes from the NIFTY 50 series, which trades
    every session; each symbol's own answer comes from here.
    """
    candidates = [bar.session for bar in daily if iso_key(bar.session) == week]
    return max(candidates) if candidates else None
