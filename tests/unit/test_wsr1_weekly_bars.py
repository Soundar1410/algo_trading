"""Phase 1. The ISO weekly bar builder (spec section 3).

Two things are being pinned: the aggregation itself, and the *release rule*
that decides which weeks a run may see at all. The second is the one with
teeth — spec section 3's "a run must never use an incomplete current week" is
enforced in exactly one place, and an off-by-one there would let the Monday
08:30 decision run trade on a week that has not finished.

Every test states ``as_of`` explicitly. Nothing in ``weekly_bars`` reads the
clock, so the whole file is deterministic under any host timezone, which
``test_the_release_rule_is_unaffected_by_the_host_timezone`` checks directly.

Calendar facts used below, all 2026 unless stated:
  * Mon 14 Sep .. Sun 20 Sep is ISO week 2026-W38, Friday 18 Sep;
  * Mon 21 Sep .. Sun 27 Sep is ISO week 2026-W39, Friday 25 Sep;
  * Fri 1 Jan 2027 falls in ISO week 2026-W53, not 2027-W01.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from strategies.positional_stocks.wsr1_weekly_stochrsi.models import DailyBar
from strategies.positional_stocks.wsr1_weekly_stochrsi.weekly_bars import (
    build_weekly_bars,
    friday_of,
    is_week_complete,
    iso_key,
    last_session_of_week,
)

IST = ZoneInfo("Asia/Kolkata")


def _ist(day: date, at: time = time(18, 0)) -> datetime:
    return datetime.combine(day, at, tzinfo=IST)


def _bar(day: date, *, open_: float, high: float, low: float, close: float, vol: float = 1000.0):
    return DailyBar(session=day, open=open_, high=high, low=low, close=close, volume=vol)


#: Mon 14 Sep .. Fri 18 Sep 2026 -- one ordinary five-session week (W38).
_W38 = [
    _bar(date(2026, 9, 14), open_=100.0, high=104.0, low=99.0, close=103.0),
    _bar(date(2026, 9, 15), open_=103.0, high=107.0, low=102.0, close=106.0),
    _bar(date(2026, 9, 16), open_=106.0, high=110.0, low=98.0, close=99.0),
    _bar(date(2026, 9, 17), open_=99.0, high=105.0, low=97.0, close=104.0),
    _bar(date(2026, 9, 18), open_=104.0, high=112.0, low=103.0, close=111.0),
]


# ------------------------------------------------------------------ iso weeks
def test_iso_key_groups_by_iso_year_not_calendar_year() -> None:
    """1 Jan 2027 is a Friday in ISO week 2026-W53. Grouping on the calendar
    year would split one trading week into two bars."""
    assert iso_key(date(2026, 12, 31)) == (2026, 53)
    assert iso_key(date(2027, 1, 1)) == (2026, 53)
    assert iso_key(date(2027, 1, 4)) == (2027, 1)


@pytest.mark.parametrize(
    "day",
    [date(2026, 9, 14), date(2026, 9, 16), date(2026, 9, 18), date(2026, 9, 20)],
)
def test_friday_of_is_the_same_friday_for_every_day_of_one_iso_week(day: date) -> None:
    assert friday_of(day) == date(2026, 9, 18)


def test_friday_of_handles_the_year_straddling_week() -> None:
    assert friday_of(date(2026, 12, 30)) == date(2027, 1, 1)


# ------------------------------------------------------------- aggregation
def test_a_five_session_week_aggregates_open_high_low_close() -> None:
    bars = build_weekly_bars(_W38, as_of=_ist(date(2026, 9, 21)))

    assert len(bars) == 1
    week = bars[0]
    assert week.open == 100.0, "the first session's open"
    assert week.high == 112.0, "the week's extreme, not the last session's"
    assert week.low == 97.0
    assert week.close == 111.0, "the last session's close"
    assert week.volume == 5000.0
    assert week.sessions == 5
    assert week.iso_key == (2026, 38)


def test_week_ending_is_the_last_session_not_the_friday() -> None:
    """Spec section 3 defines it that way, and in a holiday week the two
    differ. Two symbols' bars for the same week can therefore carry different
    ``week_ending`` dates, which is why ``iso_key`` is the identity."""
    short_week = _W38[:3]  # Mon .. Wed, Thu and Fri not traded

    week = build_weekly_bars(short_week, as_of=_ist(date(2026, 9, 21)))[0]

    assert week.week_ending == date(2026, 9, 16)
    assert friday_of(week.week_ending) == date(2026, 9, 18)
    assert week.iso_key == (2026, 38)


def test_a_holiday_week_of_four_sessions_still_produces_a_bar() -> None:
    week = build_weekly_bars(_W38[1:], as_of=_ist(date(2026, 9, 21)))[0]

    assert week.sessions == 4
    assert week.open == 103.0, "the first session that traded"
    assert week.close == 111.0


def test_a_single_session_week_produces_a_bar_whose_ohlc_is_that_session() -> None:
    week = build_weekly_bars([_W38[2]], as_of=_ist(date(2026, 9, 21)))[0]

    assert week.sessions == 1
    assert (week.open, week.high, week.low, week.close) == (106.0, 110.0, 98.0, 99.0)


def test_a_week_with_no_sessions_simply_has_no_bar() -> None:
    """A fully closed week is absent, not a zero bar — the indicators count
    bars, and an invented one would shift every lookback by a week."""
    two_weeks = [*_W38, _bar(date(2026, 10, 5), open_=1.0, high=2.0, low=1.0, close=2.0)]

    bars = build_weekly_bars(two_weeks, as_of=_ist(date(2026, 10, 12)))

    assert [bar.iso_key for bar in bars] == [(2026, 38), (2026, 41)]


def test_sessions_may_arrive_in_any_order() -> None:
    week = build_weekly_bars(list(reversed(_W38)), as_of=_ist(date(2026, 9, 21)))[0]

    assert week.open == 100.0
    assert week.close == 111.0


def test_bars_are_returned_oldest_first_across_the_year_boundary() -> None:
    sessions = [
        _bar(date(2026, 12, 30), open_=10.0, high=11.0, low=9.0, close=10.5),
        _bar(date(2027, 1, 1), open_=10.5, high=12.0, low=10.0, close=11.5),
        _bar(date(2027, 1, 4), open_=11.5, high=13.0, low=11.0, close=12.5),
    ]

    bars = build_weekly_bars(sessions, as_of=_ist(date(2027, 1, 11)))

    assert [bar.iso_key for bar in bars] == [(2026, 53), (2027, 1)]
    assert bars[0].sessions == 2, "30 Dec and 1 Jan are the same ISO week"
    assert bars[0].close == 11.5


def test_an_empty_series_produces_no_bars() -> None:
    assert build_weekly_bars([], as_of=_ist(date(2026, 9, 21))) == []


# ------------------------------------------------------- the completed-week rule
@pytest.mark.parametrize(
    "as_of,complete",
    [
        (_ist(date(2026, 9, 17), time(23, 59)), False),  # Thursday night
        (_ist(date(2026, 9, 18), time(9, 15)), False),  # Friday, market open
        (_ist(date(2026, 9, 18), time(15, 29, 59)), False),  # one second early
        (_ist(date(2026, 9, 18), time(15, 30)), True),  # the release moment
        (_ist(date(2026, 9, 18), time(18, 0)), True),  # the Friday fetch job
        (_ist(date(2026, 9, 19), time(10, 0)), True),  # Saturday retry
        (_ist(date(2026, 9, 20), time(10, 0)), True),  # Sunday retry
        (_ist(date(2026, 9, 21), time(8, 30)), True),  # Monday decision run
    ],
)
def test_week_38_is_released_exactly_at_friday_1530_ist(as_of: datetime, complete: bool) -> None:
    assert is_week_complete(date(2026, 9, 16), as_of) is complete


def test_the_monday_decision_run_never_sees_its_own_week() -> None:
    """The run of spec 10.3 fires at Monday 08:30 and decides the week that
    just ended. The week it is standing in must not appear."""
    current_week = [_bar(date(2026, 9, 21), open_=111.0, high=113.0, low=110.0, close=112.0)]

    bars = build_weekly_bars([*_W38, *current_week], as_of=_ist(date(2026, 9, 21), time(8, 30)))

    assert [bar.iso_key for bar in bars] == [(2026, 38)]


def test_a_midweek_run_releases_nothing_from_the_current_week() -> None:
    bars = build_weekly_bars(_W38[:3], as_of=_ist(date(2026, 9, 16), time(20, 0)))

    assert bars == [], "Wednesday cannot prove Thursday and Friday will not trade"


def test_a_week_whose_last_session_was_wednesday_is_still_held_until_friday() -> None:
    """Thursday and Friday both closed. The data cannot distinguish that from a
    truncated download, so the bar waits for the calendar — a delay of at most
    a weekend, always in the fail-closed direction."""
    short_week = _W38[:3]

    assert build_weekly_bars(short_week, as_of=_ist(date(2026, 9, 17), time(20, 0))) == []
    assert len(build_weekly_bars(short_week, as_of=_ist(date(2026, 9, 18), time(15, 30)))) == 1


def test_a_naive_as_of_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_weekly_bars(_W38, as_of=datetime(2026, 9, 21, 8, 30))


def test_the_release_rule_is_unaffected_by_the_host_timezone() -> None:
    """The same instant, expressed in three zones, releases the same weeks.
    This is what D88+ is about: the host clock must not decide which weeks a
    run sees."""
    moment_ist = _ist(date(2026, 9, 18), time(15, 30))

    for zone in ("UTC", "America/New_York", "Asia/Tokyo"):
        same_instant = moment_ist.astimezone(ZoneInfo(zone))
        assert is_week_complete(date(2026, 9, 16), same_instant) is True
        assert len(build_weekly_bars(_W38, as_of=same_instant)) == 1


def test_one_second_before_the_release_no_zone_reports_it_complete() -> None:
    moment_ist = _ist(date(2026, 9, 18), time(15, 29, 59))

    for zone in ("UTC", "America/New_York", "Asia/Tokyo"):
        assert is_week_complete(date(2026, 9, 16), moment_ist.astimezone(ZoneInfo(zone))) is False


# --------------------------------------------------- weekend special sessions
def test_a_saturday_special_session_is_included_when_present() -> None:
    """NSE runs occasional out-of-hours sessions — Muhurat trading on Diwali,
    and budget days that have historically fallen on a Saturday. Such a session
    belongs to its ISO week and is aggregated into the bar whenever the data
    contains it.

    What the builder cannot do is revisit a week already cached: a Friday-18:00
    fetch that ran *before* a Saturday session will have cached that week
    without it, and nothing re-reads it. That is a known limitation recorded in
    the runbook, and ``--force-refetch`` is Phase 4's answer to it.
    """
    saturday = _bar(date(2026, 9, 19), open_=111.0, high=120.0, low=111.0, close=118.0)

    week = build_weekly_bars([*_W38, saturday], as_of=_ist(date(2026, 9, 21)))[0]

    assert week.sessions == 6
    assert week.week_ending == date(2026, 9, 19), "the Saturday is the week's last session"
    assert week.high == 120.0, "its high reaches the weekly bar"
    assert week.close == 118.0, "and its close becomes the weekly close"
    assert week.iso_key == (2026, 38), "it does not start a new week"


def test_a_sunday_session_also_stays_in_the_same_iso_week() -> None:
    """ISO weeks run Monday..Sunday, so a Sunday session is the *last* day of
    its own week, not the first of the next."""
    sunday = _bar(date(2026, 9, 20), open_=111.0, high=115.0, low=110.0, close=114.0)

    bars = build_weekly_bars([*_W38, sunday], as_of=_ist(date(2026, 9, 21)))

    assert len(bars) == 1
    assert bars[0].iso_key == (2026, 38)
    assert bars[0].week_ending == date(2026, 9, 20)


# ------------------------------------------------------------ last session of
def test_last_session_of_week_finds_the_latest_session_inside_that_week() -> None:
    assert last_session_of_week(_W38, (2026, 38)) == date(2026, 9, 18)


def test_last_session_of_week_is_none_when_the_series_misses_the_week() -> None:
    """This is what spec 6.2's staleness check reads: a series with nothing in
    the week being decided cannot be used for it."""
    assert last_session_of_week(_W38, (2026, 39)) is None


def test_last_session_of_week_ignores_sessions_in_other_weeks() -> None:
    later = _bar(date(2026, 9, 25), open_=1.0, high=2.0, low=1.0, close=2.0)

    assert last_session_of_week([*_W38, later], (2026, 38)) == date(2026, 9, 18)
