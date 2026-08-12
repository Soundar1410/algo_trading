"""Canonical session-aware candle bucket boundaries for the warm-up package.

Production candle aggregation — both the live path
(:class:`common.candles.builder.CandleBuilder`) and the historical-fetch
path (:func:`common.warmup.historical.aggregate_candles`) — buckets on
:func:`common.candles.aggregator.floor_to_interval`'s grid: floored to the
timestamp's own midnight, not anchored at ``session.start``. For a 5-minute
timeframe these two anchor points happen to agree (``session.start`` is
itself 5-minute-aligned from midnight), which is why the difference went
unnoticed. They disagree for any timeframe that doesn't evenly divide the
midnight-to-``session.start`` offset — a 1-hour timeframe on a 09:15 session
floors to ``10:00, 11:00, ...``, not ``09:15, 10:15, ...``.

This module is the single place that reconciles "the production bucket
grid" with "the configured session boundary," so
:mod:`common.warmup.manager` (completeness verification) and
:mod:`common.warmup.historical` (the historical fetch filter) cannot drift
apart from each other, or from what the live engine would actually produce,
again. Lives in :mod:`common.warmup` rather than
:mod:`common.candles.aggregator` deliberately: it needs both
:func:`~common.candles.aggregator.floor_to_interval` and
:class:`~common.engine.session.MarketSession`, and ``common.warmup``
already depends on both — adding that dependency to the lower-level
``common.candles`` package instead would invert the existing layering
(``common.engine`` already depends on ``common.candles``, not the reverse).

The live engine's own session-boundary enforcement (dropping out-of-session
ticks *before* they ever reach :class:`CandleBuilder` — see
:meth:`common.engine.engine.TradingEngine._on_underlying_tick`) is a
different, already-correct mechanism for a different code path and is not
touched by this module.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from common.engine.session import MarketSession
from common.utils.timeutils import combine, local_date_in


def _session_bucket_offsets(session: MarketSession, interval_minutes: int) -> range:
    """Midnight-relative starts whose full interval fits the session.

    The integer grid is the wall-clock equivalent of ``floor_to_interval``'s
    midnight anchor. Keeping it here lets both the dated timestamp builder and
    the calendar-independent capacity calculation use exactly the same rule.
    """
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")

    interval_seconds = interval_minutes * 60
    start_seconds = (
        session.start.hour * 3600 + session.start.minute * 60 + session.start.second
    )
    end_seconds = (
        session.square_off.hour * 3600
        + session.square_off.minute * 60
        + session.square_off.second
    )
    first_start = (
        (start_seconds + interval_seconds - 1) // interval_seconds
    ) * interval_seconds
    latest_start = end_seconds - interval_seconds
    if first_start > latest_start:
        return range(0)
    return range(first_start, latest_start + 1, interval_seconds)


def session_bucket_count(session: MarketSession, interval_minutes: int) -> int:
    """Number of canonical full-interval buckets in one trading session.

    This deliberately does not take a date: it is capacity arithmetic used to
    decide how many prior sessions the history fetch must request. Holiday and
    weekend selection remains the calendar's responsibility when those sessions
    are actually walked.
    """
    return len(_session_bucket_offsets(session, interval_minutes))


def session_bucket_starts(
    session: MarketSession, day: date, interval_minutes: int
) -> list[datetime]:
    """The canonical, production-grid bucket-start timestamps for one
    trading day's session.

    Walks :func:`floor_to_interval`'s own midnight-anchored grid (the same
    one live/historical aggregation use) rather than stepping from
    ``session.start``, so this matches production for any timeframe
    :func:`~common.utils.timeutils.parse_timeframe_minutes` supports (5m,
    30m, 1h, ...), not just ones that happen to divide evenly into
    ``session.start``'s offset from midnight.

    A bucket is included only when its **full interval**
    ``[start, start + interval)`` lies within ``[session.start,
    session.square_off)`` — this is what excludes both a leading bucket
    that starts before the session opens (e.g. a 1h grid's ``09:00-10:00``
    when the session starts at 09:15) and a terminal bucket that would
    extend past the close (e.g. a 15m grid's ``15:15-15:30`` when
    ``square_off`` is 15:20).

    Returns ``[]`` for a day that isn't a trading day at all.
    """
    if not session.is_trading_day(combine(day, time(0, 0), session.timezone)):
        return []
    midnight = combine(day, time(0, 0), session.timezone)
    return [
        midnight + timedelta(seconds=offset)
        for offset in _session_bucket_offsets(session, interval_minutes)
    ]


def is_applicable_session_bucket(
    session: MarketSession,
    start_at: datetime,
    end_at: datetime,
    interval_minutes: int,
) -> bool:
    """True iff a candle exactly occupies one canonical session bucket.

    Rejects a candle that production aggregation could never have actually
    produced: off the ``floor_to_interval`` grid, on a non-trading day, or
    with an interval that starts before the session opens or extends past
    its close. The candle's declared ``end_at`` must also be exactly one
    configured interval after ``start_at``; validating only the start would
    silently trust truncated or overlong provider candles.
    """
    day = local_date_in(start_at, session.timezone)
    expected_end = start_at + timedelta(minutes=interval_minutes)
    return (
        end_at == expected_end
        and start_at in session_bucket_starts(session, day, interval_minutes)
    )
