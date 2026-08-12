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

from common.candles.aggregator import floor_to_interval
from common.engine.session import MarketSession
from common.utils.timeutils import combine, local_date_in


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
    tz = session.timezone
    interval = timedelta(minutes=interval_minutes)
    day_start = combine(day, session.start, tz)
    day_end = combine(day, session.square_off, tz)
    cursor = floor_to_interval(day_start, interval_minutes * 60)
    buckets: list[datetime] = []
    while cursor < day_end:
        if cursor >= day_start and cursor + interval <= day_end:
            buckets.append(cursor)
        cursor += interval
    return buckets


def is_applicable_session_bucket(
    session: MarketSession, start_at: datetime, interval_minutes: int
) -> bool:
    """True iff ``start_at`` is exactly one of :func:`session_bucket_starts`'
    entries for its own trading day.

    Rejects a candle that production aggregation could never have actually
    produced: off the ``floor_to_interval`` grid, on a non-trading day, or
    with an interval that starts before the session opens or extends past
    its close. Used to keep such a candle from ever being able to
    contribute to a trusted ``WARMED`` result.
    """
    day = local_date_in(start_at, session.timezone)
    return start_at in session_bucket_starts(session, day, interval_minutes)
