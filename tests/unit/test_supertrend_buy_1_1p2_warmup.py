"""Warm-up trust proofs for ``supertrend_buy_1_1p2`` (spec section 7).

Driven through the **real** :class:`~common.warmup.manager.WarmupManager` and
:class:`~common.engine.session.MarketSession`, with only the history *fetch* stubbed —
so what is proven here is the manager's genuine verdict on genuine session-grid
candles, not a restatement of the strategy's own arithmetic.

Why this file exists at all
---------------------------
``SuperTrend.warmup_requirement()`` declares ``min_bars = period``, which is ``1`` for
this strategy. That is a correct statement about when the *ATR* has a value and a
dangerous one about when the *trend* can be trusted: the direction is latched and
path-dependent, so a replay of a single recent candle seeds a direction outright and
the first live crossing can then be read as a fresh flip that never happened — or the
opposite direction held and a real flip swallowed. ``SupertrendBuy1x1p2Strategy``
therefore raises its own floor to :data:`~strategies.intraday_options.
supertrend_buy_1_1p2.strategy.DEFAULT_WARMUP_MIN_BARS` (75) inside ``warmup_spec()``,
leaving the shared indicator untouched.

Sizing, verified rather than assumed: the canonical 5-minute grid for a 09:15-15:20
session holds **73** full buckets (``session_bucket_count``), the 09:15 through
15:15-15:20 bars. 75 is therefore deliberately more than one complete session, so the
required suffix always spans two trading sessions and a start right at the open cannot
be satisfied by a partial day.

Calendar used throughout (verified weekdays): 2026-08-17 Mon, 08-18 Tue, 08-19 Wed,
08-20 Thu, 08-21 Fri; 2026-08-12 Wed, 08-13 Thu, 08-14 Fri.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from common.candles.builder import to_ohlc
from common.engine.config import SessionConfig
from common.engine.session import MarketSession
from common.models import Candle
from common.warmup.manager import WarmupManager
from common.warmup.requirements import StrategyWarmupSpec
from common.warmup.session_buckets import session_bucket_count, session_bucket_starts
from common.warmup.source import WarmupSource
from strategies.intraday_options.supertrend_buy_1_1p2.strategy import (
    DEFAULT_WARMUP_MIN_BARS,
    SupertrendBuy1x1p2Strategy,
)

IST = ZoneInfo("Asia/Kolkata")
TIMEFRAME_MINUTES = 5
_SOURCE = WarmupSource(security_id="13", exchange_segment="IDX_I", instrument_type="INDEX")

#: Every NSE 2026 weekday closure this file needs, plus the one it injects on purpose.
_AUGUST_HOLIDAY = "2026-08-14"


def _session(*, holidays: tuple[str, ...] = ()) -> MarketSession:
    return MarketSession(
        SessionConfig(
            timezone="Asia/Kolkata",
            start_time="09:15",
            end_time="15:15",
            square_off_time="15:20",
            holidays=holidays,
        )
    )


def _candle(start_at: datetime, close: float) -> Candle:
    return Candle(
        security_id="13",
        instrument="NIFTY",
        open=close,
        high=close,
        low=close,
        close=close,
        volume=0,
        start_at=start_at,
        end_at=start_at + timedelta(minutes=TIMEFRAME_MINUTES),
    )


def _grid(session: MarketSession, day: date) -> list[datetime]:
    """The canonical production bucket-starts for one session — the same helper the
    manager's own completeness check and the historical fetch filter use."""
    return session_bucket_starts(session, day, TIMEFRAME_MINUTES)


def _candles(starts: list[datetime]) -> list[Candle]:
    """Valid, ordered, duplicate-free candles on the given bucket-starts.

    The closes walk a gentle ramp so the SuperTrend actually latches a direction
    rather than sitting on a degenerate flat series.
    """
    return [_candle(s, 24000.0 + i) for i, s in enumerate(starts)]


def _strategy(**kwargs: object) -> SupertrendBuy1x1p2Strategy:
    return SupertrendBuy1x1p2Strategy(**kwargs)  # type: ignore[arg-type]


def _warm(
    candles: list[Candle],
    *,
    now: datetime,
    session: MarketSession | None = None,
    spec: StrategyWarmupSpec | None = None,
    strategy: SupertrendBuy1x1p2Strategy | None = None,
    max_lookback_sessions: int = 3,
    requested: list[int] | None = None,
):
    """Run a real warm-up, replaying into a real strategy. Returns (result, strategy).

    The stubbed fetch honours ``lookback_sessions`` the way the production fetch does
    (``common.warmup.historical.fetch_warmup_candles_range`` requests everything from
    ``prior_trading_day(today, lookback_sessions)`` onwards), so a lookback budget too
    small to reach the required history shows up here exactly as it would in
    production — as missing coverage — instead of being silently bypassed by a stub
    that hands back every candle regardless.
    """
    session = session or _session()
    strategy = strategy or _strategy()
    spec = spec if spec is not None else strategy.warmup_spec()
    assert spec is not None

    def _fetch(source, *, session, timeframe_minutes, lookback_sessions, now=None):
        assert source is _SOURCE
        if requested is not None:
            requested.append(lookback_sessions)
        assert now is not None
        from_day = session.prior_trading_day(now.astimezone(IST).date(), max(1, lookback_sessions))
        return [c for c in candles if c.start_at.astimezone(IST).date() >= from_day]

    manager = WarmupManager(_fetch, max_lookback_sessions=max_lookback_sessions)
    result = manager.warm(
        lambda candle: strategy.on_candle(to_ohlc(candle), candle.start_at),
        _SOURCE,
        spec,
        session=session,
        timeframe_minutes=TIMEFRAME_MINUTES,
        now=now,
    )
    strategy.on_warmup_complete(context_trusted=result.status == "WARMED")
    return result, strategy


def _suffix_ending_at_previous_close(
    session: MarketSession, days: list[date], count: int
) -> list[datetime]:
    """The ``count`` most recent bucket-starts across ``days`` (oldest first)."""
    starts: list[datetime] = []
    for day in days:
        starts.extend(_grid(session, day))
    assert len(starts) >= count
    return starts[-count:]


# ------------------------------------------------------------------ the premise
def test_one_full_session_holds_seventy_three_buckets_so_75_spans_two_sessions():
    """The arithmetic the 75 rests on, pinned rather than assumed."""
    session = _session()
    assert session_bucket_count(session, TIMEFRAME_MINUTES) == 73
    assert DEFAULT_WARMUP_MIN_BARS == 75
    assert session_bucket_count(session, TIMEFRAME_MINUTES) < DEFAULT_WARMUP_MIN_BARS


# --------------------------------------- 1. one recent candle is not sufficient
def test_one_recent_candle_is_not_trusted_for_this_strategy():
    """Mandated proof 1. A single valid, current, on-grid candle — the exact input
    ``min_bars = period = 1`` would have accepted — must not be trusted here."""
    session = _session()
    now = datetime(2026, 8, 20, 9, 15, tzinfo=IST)  # Thu, before today's first bucket
    last_bucket = _grid(session, date(2026, 8, 19))[-1]  # Wed 15:15

    result, strategy = _warm(_candles([last_bucket]), now=now, session=session)

    assert result.status != "WARMED"
    assert result.candles_replayed == 1  # it WAS replayed; it just is not trusted
    spec = strategy.warmup_spec()
    assert spec is not None and spec.entry_blocked_by(result.status) is True
    # And the strategy itself refuses to act on the seed it was just given.
    assert strategy._context_trusted is False


def test_the_same_single_candle_would_have_been_warmed_at_min_bars_one():
    """The counterfactual that makes proof 1 mean something: with the indicator's own
    ``min_bars = period = 1`` the identical input **is** classified WARMED and would
    have been trusted. The strategy's raised floor is the only thing standing between
    a one-candle replay and a tradable "fresh flip"."""
    session = _session()
    now = datetime(2026, 8, 20, 9, 15, tzinfo=IST)
    last_bucket = _grid(session, date(2026, 8, 19))[-1]
    naive_spec = StrategyWarmupSpec(min_bars=1, continuity_required=True)

    result, _ = _warm(_candles([last_bucket]), now=now, session=session, spec=naive_spec)

    assert result.status == "WARMED"
    assert naive_spec.entry_blocked_by(result.status) is False
    assert SupertrendBuy1x1p2Strategy().warmup_spec().min_bars == 75  # type: ignore[union-attr]


# ------------------------------------------- 2. fewer than 75 buckets is blocked
def test_seventy_four_valid_buckets_still_blocks_entries():
    """Mandated proof 2, at the exact boundary — one bucket short is short."""
    session = _session()
    now = datetime(2026, 8, 20, 9, 15, tzinfo=IST)
    starts = _suffix_ending_at_previous_close(
        session, [date(2026, 8, 18), date(2026, 8, 19)], 74
    )

    result, strategy = _warm(_candles(starts), now=now, session=session)

    assert result.status == "PARTIAL"
    spec = strategy.warmup_spec()
    assert spec is not None and spec.entry_blocked_by(result.status) is True
    assert strategy._context_trusted is False


def test_a_single_missing_in_session_bucket_inside_an_otherwise_complete_set_blocks():
    """A hole in the middle is not "74 of 75", it is a discontinuity — and a latched
    indicator replayed across one is worse than cold because it looks warm."""
    session = _session()
    now = datetime(2026, 8, 20, 9, 15, tzinfo=IST)
    starts = _suffix_ending_at_previous_close(
        session, [date(2026, 8, 18), date(2026, 8, 19)], 76
    )
    del starts[40]  # drop one mid-session bucket, keep the count at 75

    result, strategy = _warm(_candles(starts), now=now, session=session)

    assert result.status == "PARTIAL"
    spec = strategy.warmup_spec()
    assert spec is not None and spec.entry_blocked_by(result.status) is True


# ------------------------------- 3./4. exactly 75, anchored at the market open
def test_exactly_seventy_five_contiguous_buckets_at_the_open_is_warmed():
    """Mandated proofs 3 and 4 together: exactly 75 valid contiguous completed
    buckets, ending at the previous session's final bar, taken at today's open.

    Because one session is 73 buckets, this necessarily spans two sessions — Wed's
    full 73 plus Tue's last two (15:10 and 15:15) — which is the coverage the spec's
    "at market open, the required suffix may be the previous valid trading session"
    case actually resolves to."""
    session = _session()
    now = datetime(2026, 8, 20, 9, 15, tzinfo=IST)  # Thu open, no completed bucket yet
    starts = _suffix_ending_at_previous_close(
        session, [date(2026, 8, 18), date(2026, 8, 19)], 75
    )
    assert len(starts) == 75
    assert starts[0] == datetime(2026, 8, 18, 15, 10, tzinfo=IST)
    assert starts[1] == datetime(2026, 8, 18, 15, 15, tzinfo=IST)
    assert starts[2] == datetime(2026, 8, 19, 9, 15, tzinfo=IST)
    assert starts[-1] == datetime(2026, 8, 19, 15, 15, tzinfo=IST)

    result, strategy = _warm(_candles(starts), now=now, session=session)

    assert result.status == "WARMED", result.detail
    assert result.candles_replayed == 75
    spec = strategy.warmup_spec()
    assert spec is not None and spec.entry_blocked_by(result.status) is False
    assert strategy._context_trusted is True
    assert strategy._supertrend.count == 75


def test_a_start_after_the_close_still_anchors_on_a_complete_session():
    """The same open-of-day case seen from the other side of the clock: a worker that
    starts after 15:20 must still be judged against today's real final bucket."""
    session = _session()
    now = datetime(2026, 8, 19, 16, 30, tzinfo=IST)  # Wed, after the close
    starts = _suffix_ending_at_previous_close(
        session, [date(2026, 8, 18), date(2026, 8, 19)], 75
    )

    result, _ = _warm(_candles(starts), now=now, session=session)

    assert result.status == "WARMED", result.detail


# ------------------------------------------- 5. mid-session, spanning both days
def test_mid_session_coverage_spans_the_current_and_previous_sessions():
    """Mandated proof 5. At 10:05 today, ten of today's buckets have completed
    (09:15 through 10:00); the remaining 65 of the required 75 come from the previous
    session. Anything less than that exact suffix is not trusted."""
    session = _session()
    now = datetime(2026, 8, 20, 10, 5, tzinfo=IST)
    today_completed = [b for b in _grid(session, date(2026, 8, 20)) if b < now]
    assert len(today_completed) == 10
    starts = _grid(session, date(2026, 8, 19))[-65:] + today_completed
    assert len(starts) == 75

    result, strategy = _warm(_candles(starts), now=now, session=session)

    assert result.status == "WARMED", result.detail
    assert strategy._context_trusted is True
    # Today's own already-completed candles really were part of the replay.
    assert starts[-1] == datetime(2026, 8, 20, 10, 0, tzinfo=IST)


def test_mid_session_coverage_that_stops_before_today_is_stale():
    """The previous session alone, however complete, is not current coverage once
    today has completed buckets of its own."""
    session = _session()
    now = datetime(2026, 8, 20, 10, 5, tzinfo=IST)
    starts = _suffix_ending_at_previous_close(
        session, [date(2026, 8, 18), date(2026, 8, 19)], 75
    )

    result, strategy = _warm(_candles(starts), now=now, session=session)

    assert result.status == "PARTIAL"
    spec = strategy.warmup_spec()
    assert spec is not None and spec.entry_blocked_by(result.status) is True


# --------------------------------------- 6. weekends and holidays are not gaps
def test_a_weekend_transition_is_not_a_false_gap():
    """Mandated proof 6, first half. Monday 2026-08-17's open: the required suffix
    walks back across the weekend to Friday 08-14 and Thursday 08-13. The day-boundary
    jump must not be read as a missing bucket."""
    session = _session()
    now = datetime(2026, 8, 17, 9, 15, tzinfo=IST)  # Monday open
    starts = _suffix_ending_at_previous_close(
        session, [date(2026, 8, 13), date(2026, 8, 14)], 75
    )
    assert starts[1].date() == date(2026, 8, 13)
    assert starts[-1] == datetime(2026, 8, 14, 15, 15, tzinfo=IST)

    result, strategy = _warm(_candles(starts), now=now, session=session)

    assert result.status == "WARMED", result.detail
    assert strategy._context_trusted is True


def test_a_configured_holiday_transition_is_not_a_false_gap():
    """Mandated proof 6, second half. With Friday 2026-08-14 configured as a trading
    holiday, Monday's required suffix walks back to Thursday 08-13 and Wednesday
    08-12 instead — and the holiday itself contributes no buckets and no gap.

    This is also why the committed strategy config declares ``parameters.holidays``:
    ``MarketSession`` is the only thing that knows the calendar, and the warm-up
    walk-back goes through it."""
    session = _session(holidays=(_AUGUST_HOLIDAY,))
    now = datetime(2026, 8, 17, 9, 15, tzinfo=IST)  # Monday open
    assert _grid(session, date(2026, 8, 14)) == []  # the holiday has no session at all
    starts = _suffix_ending_at_previous_close(
        session, [date(2026, 8, 12), date(2026, 8, 13)], 75
    )
    assert starts[-1] == datetime(2026, 8, 13, 15, 15, tzinfo=IST)

    result, strategy = _warm(_candles(starts), now=now, session=session)

    assert result.status == "WARMED", result.detail
    assert strategy._context_trusted is True


def test_a_holiday_bucket_in_the_replay_is_refused():
    """The converse: a provider that returns candles for a configured holiday is
    returning bars production could never have produced, and is not trusted."""
    session = _session(holidays=(_AUGUST_HOLIDAY,))
    now = datetime(2026, 8, 17, 9, 15, tzinfo=IST)
    starts = _suffix_ending_at_previous_close(
        session, [date(2026, 8, 12), date(2026, 8, 13)], 75
    )
    holiday_bucket = datetime(2026, 8, 14, 9, 15, tzinfo=IST)

    replayed = [*_candles(starts), _candle(holiday_bucket, 24999.0)]

    result, _ = _warm(replayed, now=now, session=session)

    assert result.status == "PARTIAL"
    assert "outside the session boundary" in result.detail


# ------------------------------------------------- the lookback budget actually fits
def test_the_required_two_session_suffix_fits_the_configured_lookback_budget():
    """``min_bars = 75`` over a 73-bucket session makes ``_lookback_sessions`` ask for
    two prior sessions; the committed config allows three. A budget that was too small
    would surface as PARTIAL ("calendar walk-back exceeded max_lookback_sessions"),
    not as a silent downgrade — so this pins that the shipped pair is workable."""
    session = _session()
    now = datetime(2026, 8, 20, 9, 15, tzinfo=IST)
    starts = _suffix_ending_at_previous_close(
        session, [date(2026, 8, 18), date(2026, 8, 19)], 75
    )

    requested: list[int] = []
    ok, _ = _warm(
        _candles(starts), now=now, session=session, max_lookback_sessions=3, requested=requested
    )
    assert ok.status == "WARMED"
    # ceil(75 / 73) == 2 prior sessions plus today, capped by the configured 3.
    assert requested == [2]

    starved_requested: list[int] = []
    starved, _ = _warm(
        _candles(starts),
        now=now,
        session=session,
        max_lookback_sessions=1,
        requested=starved_requested,
    )
    assert starved_requested == [1]
    assert starved.status == "PARTIAL"
    assert starved.candles_replayed == 73  # one session reached, 75 needed
