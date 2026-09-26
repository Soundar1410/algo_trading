"""The week's expected last session, from the calendar (spec 6.2 v1.2b).

What this exists to prevent
---------------------------
Phase 1 derived "week W's last session" from NIFTY 50's own data, on the
reasoning that NIFTY trades every session the exchange holds, so its last bar
in a week *is* that week's last session. That reasoning has a hole, and the
Phase 1 review found it: it assumes the data is complete at the moment it is
read. **21 September 2026 was an NSE trading day** — NSE's September list has
only the 14th — yet Dhan had not published its daily candle by 22:20 IST.

Run that forward to a Friday. Dhan has not yet published Friday's candle, so
NIFTY's last bar is Thursday's, so Thursday *is* "the week's last session", so
every series in the universe contains it, so every series passes the staleness
check, and a Monday-to-Thursday bar is released as the week. Nothing errors. The
week is simply wrong, and the indicators computed on it are wrong with it.

So the expected last session comes from the **calendar**, which knows what the
exchange intended independently of what the vendor has published yet:

    the last weekday (Monday to Friday) of the ISO week that is not in the verified
    NSE holiday list in ``config/global.yaml``

and NIFTY missing that session means **not yet published** — fail closed,
report, and let the next scheduled attempt retry.

No second copy of the calendar
------------------------------
The weekend/holiday rules live in :class:`~common.engine.session.MarketSession`
and the walk itself in its :meth:`~common.engine.session.MarketSession.
prior_trading_day`, whose docstring already says any caller needing "N trading
days before this one" goes through it rather than reimplementing the rules.
This module is an adapter onto that, not a reimplementation: it converts an ISO
week into the Saturday that bounds it, hands that to ``prior_trading_day``, and
checks the answer did not walk out of the week.

Reading the holiday list is a **configuration read**
(:func:`~common.config.loader.load_auto_start_config`, the ``auto_start:``
block of ``config/global.yaml``). It is emphatically not routing this runtime
through ``orchestration.auto_start``, which CLAUDE.md forbids and which nothing
here touches: the weekly job is scheduled by its own LaunchAgents and has no
``RUNTIMES`` entry.

The list is annual, and that is safe in the right direction. Without a 2027
list every 2027 holiday Friday is "expected", so that week's index series never
contains it and the run fails closed — noisy, never wrong.

Pure and clock-free
-------------------
Nothing here reads the clock. A :class:`TradingCalendar` is a holiday set and a
timezone; every question it answers is a pure function of its argument.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from pathlib import Path

from common.config.loader import load_auto_start_config
from common.engine.config import SessionConfig
from common.engine.session import MarketSession
from common.utils.timeutils import combine

from .weekly_bars import iso_key

#: ``date.isoweekday()`` for Saturday — one past the last weekday, which is
#: what makes it the exclusive upper bound ``prior_trading_day`` wants.
_SATURDAY = 6

#: ``MarketSession`` asks date questions of a datetime; the time of day is
#: irrelevant to every predicate used here, so all of them are asked at
#: midnight in the calendar's own zone.
_MIDNIGHT = time(0, 0)


class CalendarError(ValueError):
    """The calendar cannot name an expected last session for a week.

    Raised only when an ISO week contains no trading weekday at all — every
    Monday-to-Friday listed as a holiday. That has never happened on NSE, and
    the alternative to raising is returning a date from the *previous* week,
    which would silently pass a stale series.
    """


def saturday_of(day: date) -> date:
    """The Saturday bounding the ISO week containing ``day``."""
    return day + timedelta(days=_SATURDAY - day.isoweekday())


class TradingCalendar:
    """The NSE weekday/holiday calendar, as this strategy asks questions of it.

    Wraps a :class:`~common.engine.session.MarketSession` rather than holding a
    holiday set of its own. ``MarketSession`` needs session *times* to
    construct; the repository defaults are used and none of them is read by any
    predicate here — only ``timezone`` and ``holidays`` matter to a calendar
    question. They are supplied because the constructor validates them, not
    because this module has an opinion about when the session opens.
    """

    def __init__(self, session: MarketSession, covered_years: Iterable[int] = ()) -> None:
        self._session = session
        self._covered_years = frozenset(covered_years)

    @classmethod
    def from_holidays(cls, holidays: Iterable[str]) -> TradingCalendar:
        """Build from ISO date strings — the shape ``SessionConfig`` takes."""
        listed = tuple(holidays)
        years = {int(day[:4]) for day in listed}
        return cls(MarketSession(SessionConfig(holidays=listed)), years)

    def covers(self, day: date) -> bool:
        """Is ``day``'s year in the verified holiday list? Outside it, every
        holiday Friday is "expected" — the safe direction for a live week, but
        a false truncation for an old one (spec 4b: truncated-week warnings
        only for calendar-covered weeks)."""
        return day.year in self._covered_years

    @classmethod
    def from_config(cls, config_root: Path) -> TradingCalendar:
        """Build from the verified holiday list in ``config/global.yaml``.

        One source for the calendar, shared with every runtime on the platform.
        A holiday added for the intraday strategies is a holiday here on the
        same commit.
        """
        return cls.from_holidays(load_auto_start_config(config_root).holiday_dates)

    @property
    def timezone(self) -> str:
        """The calendar's IANA zone, for callers that must resolve an instant
        the same way this one does."""
        return self._session.timezone

    def is_trading_day(self, day: date) -> bool:
        """A weekday that is not a listed holiday.

        Note what this is *not*: it is not "the exchange held a session". NSE
        occasionally runs one on a listed holiday or a weekend — the Diwali
        Muhurat session, historically a budget Saturday. Those are real
        sessions and appear in the data; see :meth:`unlisted_sessions`. They do
        not make the day a trading day for scheduling purposes, and they never
        move the week's expected last session.
        """
        return self._session.is_trading_day(self._midnight(day))

    def expected_last_session(self, week: tuple[int, int]) -> date:
        """The session ISO week ``week`` is expected to end on (spec 6.2).

        The last weekday of the week that is not a listed holiday: Friday
        normally, Thursday when Friday is a holiday, and so on backwards.

        Raises:
            CalendarError: the week contains no trading weekday at all.
        """
        iso_year, iso_week = week
        saturday = date.fromisocalendar(iso_year, iso_week, _SATURDAY)
        # prior_trading_day is exclusive of its argument, so Saturday resolves
        # to Friday when Friday trades. It is bounded and will happily walk
        # into the previous week, which is why the containment check below is
        # not optional.
        candidate = self._session.prior_trading_day(saturday, 1)
        if iso_key(candidate) != week:
            raise CalendarError(
                f"ISO week {iso_year}-W{iso_week:02d} has no trading weekday: every "
                f"Monday-to-Friday is a listed holiday. The nearest earlier session is "
                f"{candidate.isoformat()}, which is in another week and must not be used "
                "as this week's last session."
            )
        return candidate

    def first_session(self, week: tuple[int, int]) -> date:
        """The first weekday of ISO week ``week`` that is not a listed holiday.

        Spec 4.9 v1.2g: a buy that filled on this session was "at the week's
        open"; any later fill in the week was not. Tuesday, when Monday is a
        holiday.

        Raises:
            CalendarError: the week contains no trading weekday at all.
        """
        iso_year, iso_week = week
        for weekday in range(1, _SATURDAY):
            day = date.fromisocalendar(iso_year, iso_week, weekday)
            if self.is_trading_day(day):
                return day
        raise CalendarError(
            f"ISO week {iso_year}-W{iso_week:02d} has no trading weekday: every "
            "Monday-to-Friday is a listed holiday."
        )

    def next_session_after(self, day: date) -> date:
        """The first trading day strictly after ``day`` — where an order decided
        at ``day``'s close executes (spec 3).

        Raises:
            CalendarError: no trading day within the next three weeks, which
                only a corrupted holiday list could produce.
        """
        for offset in range(1, 22):
            candidate = day + timedelta(days=offset)
            if self.is_trading_day(candidate):
                return candidate
        raise CalendarError(f"no trading day within three weeks after {day.isoformat()}")

    def unlisted_sessions(self, sessions: Iterable[date], week: tuple[int, int]) -> list[date]:
        """Sessions inside ``week`` that fall on a non-trading day, ascending.

        A weekend or listed-holiday session the exchange actually held: the
        Muhurat session on 8 November 2026 (a Sunday), a budget Saturday. Spec
        6.2 says such a session **is** included in its ISO week's bar and **is**
        reported, and does not change the expected last session. This is the
        reporting half; the inclusion happens in ``weekly_bars`` simply by not
        filtering it out.
        """
        return sorted(
            day for day in sessions if iso_key(day) == week and not self.is_trading_day(day)
        )

    def _midnight(self, day: date) -> datetime:
        return combine(day, _MIDNIGHT, self._session.timezone)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"TradingCalendar(tz={self._session.timezone!r})"
