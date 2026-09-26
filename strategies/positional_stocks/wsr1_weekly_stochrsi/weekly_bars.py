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
at or after 15:30 IST on that week's expected last session** — the last weekday
of the ISO week that is not a listed NSE holiday, from
:class:`~strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar.
TradingCalendar`. Normally that is the Friday; in a holiday-Friday week it is
the Thursday, and the week is released then rather than being held an extra day
for a session that was never going to happen.

===========================  ========================================
Run                          Effect
===========================  ========================================
Friday 18:00 (fetch)         the week just ended is released
Saturday / Sunday 10:00      same, idempotently
Monday 08:30 (decide)        prior weeks released, current week not
mid-week, by hand            the current week is not released
===========================  ========================================

Release is about the clock; **completeness of the data is a separate question**
and is not decided here. A released week whose series is missing the expected
last session is still built, and flagged — see :attr:`~strategies.
positional_stocks.wsr1_weekly_stochrsi.models.WeeklyBar.is_truncated` and spec
6.2's fail-closed staleness gate, which is what actually stops a run. Dropping
such a week here would put a silent hole in the middle of a 260-week history.

Weekend and out-of-hours special sessions
-----------------------------------------
NSE occasionally runs a session that falls in the ISO week but **after** the
release moment: the Muhurat session on Diwali, and budget-day sessions
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
from typing import TYPE_CHECKING

from common.logging import get_logger
from common.utils.timeutils import DEFAULT_TZ, combine

from .models import DailyBar, WeeklyBar

if TYPE_CHECKING:  # pragma: no cover - import cycle: trading_calendar imports iso_key
    from .trading_calendar import TradingCalendar

_log = get_logger(__name__)

#: When the NSE cash session closes. A week is released after this time on its
#: expected last session — see the module docstring for why the calendar and
#: not the data decides.
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


def week_release_moment(
    day: date, *, calendar: TradingCalendar, tz_name: str = DEFAULT_TZ
) -> datetime:
    """The instant the ISO week containing ``day`` becomes usable.

    15:30 IST on the week's **expected last session** (spec 6.2), not on its
    Friday. The two differ in a holiday-Friday week, where waiting for Friday
    would hold a finished week back for a session that was never scheduled.
    """
    return combine(calendar.expected_last_session(iso_key(day)), SESSION_CLOSE_IST, tz_name)


def is_week_complete(
    day: date, as_of: datetime, *, calendar: TradingCalendar, tz_name: str = DEFAULT_TZ
) -> bool:
    """Has the ISO week containing ``day`` closed, as of ``as_of``?

    This is a question about the **clock**, not about the data. A week can be
    complete and its series still be missing the session it was expected to end
    on — see :attr:`~.models.WeeklyBar.is_truncated`.

    Raises:
        ValueError: ``as_of`` is naive. A naive datetime here would be compared
            against an IST-aware release moment and raise deep inside the
            comparison; refusing it by name is clearer, and matches the
            repository's standing rule against naive datetimes.
        CalendarError: the week has no trading weekday at all.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware; a naive datetime has no IST meaning")
    return as_of >= week_release_moment(day, calendar=calendar, tz_name=tz_name)


def build_weekly_bars(
    daily: Iterable[DailyBar],
    *,
    as_of: datetime,
    calendar: TradingCalendar,
    tz_name: str = DEFAULT_TZ,
    warn_uncovered: bool = True,
) -> list[WeeklyBar]:
    """Aggregate daily sessions into **released** weekly bars, oldest first.

    ``warn_uncovered=False`` logs a truncated week only when the calendar's
    holiday list covers its year (spec 15, 4b): before that, every holiday
    Friday looks truncated, and a full-history series would log thousands.
    The bars themselves are identical either way.

    Sessions may arrive in any order and are sorted here. An unreleased current
    week is dropped rather than returned partially built — spec section 3's
    "a run must never use an incomplete current week" is enforced at the only
    place that could violate it.

    ``calendar`` is required, not optional. A default would mean a caller could
    build weeks without one, and "which session should this week have ended on"
    is precisely the question that must never be answered from the data.

    A released week missing its expected last session **is still built**, with
    :attr:`~.models.WeeklyBar.is_truncated` set and a warning logged. It is not
    dropped: a symbol halted on one past Friday would otherwise lose that week
    entirely, and a hole in the middle of a 260-week history is invisible to
    every indicator that runs over it. Refusing to *decide* on such a week is
    spec 6.2's staleness gate, which is the run's job.

    Raises:
        ValueError: ``as_of`` is naive.
        CalendarError: a week present in ``daily`` has no trading weekday.
    """
    # Checked once here rather than inside the per-week comparison: it is a
    # property of the argument, and an empty series must be refused too.
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware; a naive datetime has no IST meaning")

    sessions = sorted(daily, key=lambda bar: bar.session)

    grouped: dict[tuple[int, int], list[DailyBar]] = {}
    for bar in sessions:
        grouped.setdefault(iso_key(bar.session), []).append(bar)

    bars: list[WeeklyBar] = []
    for key in sorted(grouped):
        week = grouped[key]
        expected = calendar.expected_last_session(key)
        if as_of < combine(expected, SESSION_CLOSE_IST, tz_name):
            continue
        weekly = _aggregate(key, week, expected)
        if weekly.is_truncated and (warn_uncovered or calendar.covers(expected)):
            _log.warning(
                "weekly bar %d-W%02d ends %s but the calendar expects %s: "
                "the session is unpublished, the exchange closed unlisted, or this "
                "series alone missed it",
                weekly.iso_year,
                weekly.iso_week,
                weekly.week_ending.isoformat(),
                expected.isoformat(),
            )
        bars.append(weekly)
    return bars


def _aggregate(key: tuple[int, int], week: list[DailyBar], expected: date) -> WeeklyBar:
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
        expected_last_session=expected,
        volume=sum(bar.volume for bar in week),
        sessions=len(week),
    )


def last_session_of_week(daily: Iterable[DailyBar], week: tuple[int, int]) -> date | None:
    """The latest session this series has inside ``week``, or ``None``.

    Spec 6.2's staleness check is "does every used series contain week W's last
    session". That required session comes from
    :meth:`~.trading_calendar.TradingCalendar.expected_last_session`; this
    answers what one series actually has, which is the other half of the
    comparison.
    """
    candidates = [bar.session for bar in daily if iso_key(bar.session) == week]
    return max(candidates) if candidates else None
