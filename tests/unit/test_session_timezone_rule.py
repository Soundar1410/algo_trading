"""The same instant must produce the same decision (Phase 4 Part 3).

**This file exists because of a live-blocking defect nothing else could see.**

`MarketSession` and `SessionSquareOffAuthority` compared a timestamp's *raw*
wall-clock time against IST session bounds, with no conversion, while
`SquareOffPolicy` converted. `DhanMarketFeedAdapter` produces **UTC-aware**
ticks, so a real 10:00 IST tick arrives as `04:30+00:00` and `is_open` compared
`04:30` against a `09:15` opening — False, for the entire session. The engine
would have built no candles, evaluated no signals and placed no orders, live,
reporting nothing wrong.

Every fixture in this repository is IST-offset, and no test drove a session
predicate with a UTC timestamp, so the whole suite passed. The organising
assertion here is therefore not "is this answer right?" but **"do these two
spellings of one instant agree?"** — a property that is impossible to satisfy by
accident and that would have failed on day one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.engine.config import SessionConfig
from common.engine.session import MarketSession
from common.engine.square_off import SessionSquareOffAuthority
from common.risk.squareoff import SquareOffPolicy, SquareOffState, SquareOffTrigger
from common.utils.timeutils import NaiveDatetimeError, local_date_in, local_time_in

IST = ZoneInfo("Asia/Kolkata")
NEW_YORK = ZoneInfo("America/New_York")

#: A Monday, mid-session. 10:00 IST is 04:30 UTC — the exact case that failed.
MID_SESSION_IST = datetime(2026, 8, 3, 10, 0, tzinfo=IST)
#: Past the 15:20 square-off.
AFTER_SQUARE_OFF_IST = datetime(2026, 8, 3, 15, 25, tzinfo=IST)
#: Before the 09:15 open.
PRE_OPEN_IST = datetime(2026, 8, 3, 8, 30, tzinfo=IST)


def _session(**kwargs: object) -> MarketSession:
    return MarketSession(SessionConfig(**kwargs))  # type: ignore[arg-type]


def _spellings(moment: datetime) -> list[tuple[str, datetime]]:
    """One instant, in three timezones. Every predicate must not care which."""
    return [
        ("IST", moment.astimezone(IST)),
        ("UTC", moment.astimezone(UTC)),
        ("New_York", moment.astimezone(NEW_YORK)),
    ]


# ------------------------------------------------------- the organising property
@pytest.mark.parametrize(
    "moment",
    [PRE_OPEN_IST, MID_SESSION_IST, AFTER_SQUARE_OFF_IST],
    ids=["pre_open", "mid_session", "after_square_off"],
)
@pytest.mark.parametrize("predicate", ["is_open", "can_enter", "is_trading_day", "is_holiday"])
def test_every_session_predicate_ignores_how_the_instant_is_spelled(moment, predicate):
    session = _session()
    answers = {name: getattr(session, predicate)(value) for name, value in _spellings(moment)}
    assert len(set(answers.values())) == 1, (
        f"{predicate} disagreed with itself about one instant: {answers}"
    )


@pytest.mark.parametrize(
    "moment",
    [MID_SESSION_IST, AFTER_SQUARE_OFF_IST],
    ids=["mid_session", "after_square_off"],
)
def test_the_square_off_authority_ignores_how_the_instant_is_spelled(moment):
    authority = SessionSquareOffAuthority(_session())
    answers = {name: authority.due(value) for name, value in _spellings(moment)}
    assert len(set(answers.values())) == 1, f"due() disagreed about one instant: {answers}"


@pytest.mark.parametrize(
    "moment", [MID_SESSION_IST, AFTER_SQUARE_OFF_IST], ids=["mid_session", "after"]
)
def test_the_policy_and_the_authority_agree_about_one_instant(moment):
    """The two deciders resolved the same instant differently before Part 3 —
    the policy converted, the authority did not."""
    session = _session()
    authority = SessionSquareOffAuthority(session)
    policy = SquareOffPolicy(square_off_at=session.square_off, timezone="Asia/Kolkata")
    for name, value in _spellings(moment):
        by_policy = policy.trigger_at(value, state=SquareOffState.PENDING)
        assert authority.due(value) is (by_policy is SquareOffTrigger.SQUARE_OFF), (
            f"policy and authority disagree in {name}"
        )


# ------------------------------------------------------------- the exact defect
def test_a_real_utc_tick_mid_session_is_inside_the_session():
    """The regression, stated as the case that failed: 10:00 IST == 04:30 UTC."""
    utc = MID_SESSION_IST.astimezone(UTC)
    assert utc.hour == 4 and utc.minute == 30, "the fixture no longer reproduces the case"
    assert _session().is_open(utc) is True
    assert _session().can_enter(utc) is True


def test_the_hub_and_the_engine_agree_about_one_real_shaped_tick():
    """The hub converts and always did; the engine did not. A tick the hub
    accepts must not be one the engine treats as out-of-session."""
    from common.candles.aggregator import CandleAggregator
    from common.models import Tick

    utc = MID_SESSION_IST.astimezone(UTC)
    tick = Tick(
        security_id="13",
        instrument="NIFTY",
        last_price=24000.0,
        exchange_time=utc,
        received_at=utc,
    )
    aggregator = CandleAggregator()
    aggregator.add(tick)
    hub_accepted = aggregator.rejected_out_of_session == 0

    assert hub_accepted is True
    assert _session().is_open(tick.exchange_time) is hub_accepted


def test_the_adapter_really_does_produce_utc_ticks():
    """The premise the whole defect rests on — corrected 6 August 2026, known
    limitation 20.

    This test used to pass ``"04:30:00"`` as ``LTT`` and treat that as already
    the correct UTC answer — i.e. it assumed the SDK pre-converts to UTC, the
    same wrong assumption :func:`reconstruct_exchange_time` itself made. A
    real capture disproved it: ``LTT`` is IST wall-clock. The correct premise
    is the one this test now states: a real 10:00 IST tick's ``LTT`` reads
    ``"10:00:00"``, and reconstruction must *convert* that to its true UTC
    equivalent, 04:30:00 — not relabel the digits. If this premise ever
    changes, the tests above (which assume already-UTC ticks) stop covering
    the real case and should be revisited rather than quietly continuing to
    pass.
    """
    from common.market_data.dhan import reconstruct_exchange_time

    rebuilt = reconstruct_exchange_time("10:00:00", datetime(2026, 8, 3, 4, 30, 5, tzinfo=UTC))
    assert rebuilt == datetime(2026, 8, 3, 4, 30, 0, tzinfo=UTC)
    assert rebuilt.utcoffset() == timedelta(0), "the reconstructed value must still be UTC-labelled"


# ------------------------------------------------------------------- boundaries
@pytest.mark.parametrize(
    ("clock", "expect_open"),
    [
        ("09:14:59", False),
        ("09:15:00", True),
        ("15:20:00", True),  # square_off is inclusive for is_open
        ("15:20:01", False),
    ],
)
def test_the_session_boundaries_hold_in_utc_too(clock, expect_open):
    """Boundary correctness is where an offset error hides: a whole-hour offset
    might coincidentally agree, but IST's :30 cannot."""
    hour, minute, second = (int(part) for part in clock.split(":"))
    moment = datetime(2026, 8, 3, hour, minute, second, tzinfo=IST)
    assert _session().is_open(moment) is expect_open
    assert _session().is_open(moment.astimezone(UTC)) is expect_open


def test_a_late_evening_ist_instant_keeps_its_own_date():
    """23:50 IST is already the next UTC day. The holiday and weekday answers
    must follow the *session's* calendar, not UTC's."""
    friday_night = datetime(2026, 8, 7, 23, 50, tzinfo=IST)  # Friday
    assert friday_night.astimezone(UTC).date() == friday_night.date()  # still same day
    saturday_night = datetime(2026, 8, 8, 23, 50, tzinfo=IST)  # Saturday
    assert _session().is_trading_day(saturday_night) is False
    assert _session().is_trading_day(saturday_night.astimezone(UTC)) is False


def test_a_holiday_is_resolved_on_the_session_calendar():
    session = _session(holidays=("2026-08-03",))
    for _name, value in _spellings(MID_SESSION_IST):
        assert session.is_holiday(value) is True
        assert session.is_open(value) is False


def test_an_early_morning_utc_instant_lands_on_the_right_ist_day():
    """01:00 IST on Monday is 19:30 UTC the previous *Sunday*. Resolved on the
    UTC date it would be a weekend and therefore not a trading day."""
    monday_small_hours = datetime(2026, 8, 3, 1, 0, tzinfo=IST)
    assert monday_small_hours.astimezone(UTC).weekday() == 6  # Sunday in UTC
    assert _session().is_trading_day(monday_small_hours) is True
    assert _session().is_trading_day(monday_small_hours.astimezone(UTC)) is True


# ---------------------------------------------------------------------- refusal
@pytest.mark.parametrize("predicate", ["is_open", "can_enter", "is_trading_day", "is_holiday"])
def test_a_naive_datetime_is_refused_not_guessed(predicate):
    """Python would read it as system-local, which is the bug class this closes.
    Reading it as IST would instead hide that a caller lost its timezone."""
    naive = datetime(2026, 8, 3, 10, 0)
    with pytest.raises(NaiveDatetimeError, match="must be timezone-aware"):
        getattr(_session(), predicate)(naive)


def test_the_square_off_authority_refuses_a_naive_datetime():
    with pytest.raises(NaiveDatetimeError):
        SessionSquareOffAuthority(_session()).due(datetime(2026, 8, 3, 15, 25))


def test_the_policy_refuses_a_naive_datetime():
    """Tightened by Part 3: it used to read one as system-local."""
    with pytest.raises(NaiveDatetimeError):
        SquareOffPolicy().trigger_at(datetime(2026, 8, 3, 15, 25), state=SquareOffState.PENDING)


# ------------------------------------------------------------------ the helper
def test_local_time_in_converts_rather_than_truncating():
    assert local_time_in(MID_SESSION_IST.astimezone(UTC), "Asia/Kolkata").hour == 10
    assert local_time_in(MID_SESSION_IST, "UTC").hour == 4


def test_local_date_in_uses_the_target_zones_day():
    late = datetime(2026, 8, 3, 23, 50, tzinfo=IST)
    assert local_date_in(late, "Asia/Kolkata").isoformat() == "2026-08-03"
    assert local_date_in(late, "UTC").isoformat() == "2026-08-03"
    later = datetime(2026, 8, 4, 3, 0, tzinfo=IST)  # 21:30 UTC on the 3rd
    assert local_date_in(later, "Asia/Kolkata").isoformat() == "2026-08-04"
    assert local_date_in(later, "UTC").isoformat() == "2026-08-03"


def test_the_error_names_the_argument_so_the_caller_can_find_it():
    with pytest.raises(NaiveDatetimeError, match="tick_time"):
        local_time_in(datetime(2026, 8, 3, 10, 0), argument="tick_time")
