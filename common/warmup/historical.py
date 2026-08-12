"""Historical intraday candles — used to warm up an indicator on startup.

Ported from the reference repository's ``framework/market_data/historical.py``
(Phase 4 Part 4), with more adaptation than a straight port: see the module's
own runbook entries (D43-D45) for what changed and why. In short —

* The reference calls ``dhanhq(ctx).intraday_minute_data(...)`` directly. This
  repository's SDK-isolation rule (only ``common/market_data/dhan.py`` may
  import ``dhanhq``, test-enforced) means the fetch here goes through
  :class:`common.market_data.dhan_historical.DhanHistoricalDataClient`
  instead — a REST client, not the SDK.
* The reference's ``Candle`` is a plain **mutable** dataclass; ``aggregate_candles``
  mutates buckets in place. This repository's :class:`common.models.Candle` is
  **frozen**, with a different field set (``start_at``/``end_at``, not
  ``start``; requires ``security_id``/``instrument``). :func:`aggregate_candles`
  is rewritten, not ported, to build fresh frozen instances per bucket —
  mirroring :mod:`common.candles.builder`'s own accumulate-then-freeze shape —
  bucketed through the *same* :func:`~common.candles.aggregator.floor_to_interval`
  the live engine's own :class:`~common.candles.builder.CandleBuilder` uses, so
  warm-up and live candles bucket identically by construction.
* The reference's ``_prior_trading_day`` calls ``session.is_trading_day(date)``.
  This repository's :class:`~common.engine.session.MarketSession.is_trading_day`
  takes a ``datetime`` and routes through the Phase 4 Part 3 timezone fix
  (``local_date_in``, D40). :func:`_prior_trading_day` here builds a
  timezone-aware midnight via :func:`common.utils.timeutils.combine` and calls
  that same public predicate, rather than reimplementing a comparison.
* Only :func:`fetch_warmup_candles_range` is ported — not the reference's
  today-only ``fetch_warmup_candles`` convenience wrapper (which cannot supply
  enough bars early in a session and has no production caller in the
  reference's own factory either) or ``fetch_previous_close`` (an unrelated
  pre-trade-filter concern, no consumer here).

A stateful trend indicator (e.g. SuperTrend) started mid-session has no memory
of the day's earlier price action, so it cold-starts in an arbitrary trend and
can miss (or invent) the very next flip relative to a bot that had been
running since the open. To avoid that, on startup this fetches enough prior
history from Dhan, aggregates it to the strategy timeframe, and replays it
through the indicator (see :meth:`common.engine.engine.TradingEngine._warm_up`)
so its trend state is what it would have been had the process run
continuously.

Fetch failures raise; :class:`~common.warmup.manager.WarmupManager` is what
catches them and falls back to a cold start — warm-up is a correctness
nicety, never a precondition for trading.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from common.candles.aggregator import floor_to_interval
from common.engine.session import MarketSession
from common.models import Candle
from common.utils.timeutils import combine, get_tz, now_tz

from .session_buckets import is_applicable_session_bucket

if TYPE_CHECKING:
    from common.market_data.dhan_historical import DhanHistoricalDataClient


def parse_intraday_response(resp: dict[str, Any], tz_name: str) -> list[Candle]:
    """Parse a Dhan intraday-candle response into 1-minute candles.

    Dhan's own documentation shows the parallel arrays
    (``open``/``high``/``low``/``close``/``volume``/``timestamp``, epoch
    seconds) at the **top level**. The reference's defensive fallback for a
    ``data``-nested shape is kept anyway — harmless, and this endpoint's exact
    response shape has no captured evidence in this repository, only the
    documented one.

    Every candle here carries a placeholder ``security_id``/``instrument`` —
    this function has no source for the real ones (the reference's mutable
    ``Candle`` had no such fields at all). :func:`aggregate_candles` is where
    the real identifiers land, on the candles that actually reach a caller.

    Rows with any missing/unparseable field are skipped rather than aborting
    the whole warm-up — including a row Dhan itself returned with an internally
    inconsistent OHLC (e.g. ``high < low``), which this repository's frozen
    ``Candle`` catches for free via its own ``__post_init__`` validation.
    """
    if not isinstance(resp, dict):
        raise ValueError(f"Unexpected intraday response type: {type(resp).__name__}")

    # Dhan's documented shape is top-level arrays; some builds/wrappers nest
    # them under "data". Accept either.
    block = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    if not isinstance(block, dict) or "close" not in block:
        status = resp.get("status")
        remarks = resp.get("remarks")
        raise ValueError(
            f"Intraday response has no candle arrays (status={status!r}, remarks={remarks!r})."
        )

    ts_key = (
        "timestamp" if "timestamp" in block else ("start_Time" if "start_Time" in block else None)
    )
    if ts_key is None:
        raise ValueError("Intraday response is missing a timestamp array.")

    opens = block.get("open") or []
    highs = block.get("high") or []
    lows = block.get("low") or []
    closes = block.get("close") or []
    stamps = block.get(ts_key) or []
    volumes = block.get("volume") or []

    tz = get_tz(tz_name)
    n = min(len(opens), len(highs), len(lows), len(closes), len(stamps))
    candles: list[Candle] = []
    for i in range(n):
        try:
            start = datetime.fromtimestamp(float(stamps[i]), tz)
            volume = float(volumes[i]) if i < len(volumes) else 0.0
            candle = Candle(
                security_id="",
                instrument="",
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                volume=round(volume),
                start_at=start,
                end_at=start + timedelta(minutes=1),
            )
        except (TypeError, ValueError):
            continue
        candles.append(candle)

    candles.sort(key=lambda c: c.start_at)
    return candles


def aggregate_candles(
    candles: list[Candle], interval_minutes: int, *, security_id: str, instrument: str
) -> list[Candle]:
    """Aggregate finer candles (e.g. 1-minute) into ``interval_minutes`` candles.

    Buckets are aligned with :func:`~common.candles.aggregator.floor_to_interval`
    — the exact boundaries :class:`~common.candles.builder.CandleBuilder` (the
    engine's own live candle builder, D23) uses — so a warm-up candle for, say,
    09:20-09:25 is identical in shape to the one the live feed would have
    built. Open = first sub-candle's open, high/low = extrema, close = last
    sub-candle's close, volume = sum. ``security_id``/``instrument`` are
    applied to every output candle (the input candles carry only placeholders
    — see :func:`parse_intraday_response`).

    Rewritten from the reference rather than ported: its ``Candle`` is a plain
    mutable dataclass and this function mutated buckets in place. This
    repository's ``Candle`` is frozen, so each bucket is accumulated in a
    private mutable holder and frozen into a real ``Candle`` only once its
    interval closes.
    """
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")

    interval_seconds = interval_minutes * 60

    class _Bucket:
        __slots__ = ("close", "high", "low", "open", "start_at", "volume")

        def __init__(
            self,
            start_at: datetime,
            open_: float,
            high: float,
            low: float,
            close: float,
            volume: float,
        ) -> None:
            self.start_at = start_at
            self.open = open_
            self.high = high
            self.low = low
            self.close = close
            self.volume = volume

    buckets: dict[datetime, _Bucket] = {}
    for c in candles:
        key = floor_to_interval(c.start_at, interval_seconds)
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = _Bucket(key, c.open, c.high, c.low, c.close, c.volume)
        else:
            bucket.high = max(bucket.high, c.high)
            bucket.low = min(bucket.low, c.low)
            bucket.close = c.close  # candles are time-sorted, so this ends as the last close
            bucket.volume += c.volume

    result: list[Candle] = []
    for key in sorted(buckets):
        b = buckets[key]
        result.append(
            Candle(
                security_id=security_id,
                instrument=instrument or security_id,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=round(b.volume),
                start_at=b.start_at,
                end_at=b.start_at + timedelta(minutes=interval_minutes),
            )
        )
    return result


def _prior_trading_day(session: MarketSession, day: date, sessions_back: int) -> date:
    """The trading date ``sessions_back`` sessions before ``day`` (exclusive).

    Delegates to :meth:`~common.engine.session.MarketSession.prior_trading_day`
    — the calendar walk-back now lives on the class that owns the
    weekend/holiday rules, since :mod:`common.warmup.manager` needs the same
    primitive for its own completeness check and must not reach into this
    module's private helper for it. Kept here, under the original name and
    signature, purely so every existing caller/test in this module continues
    to work unchanged.
    """
    return session.prior_trading_day(day, sessions_back)


def fetch_warmup_candles_range(
    client: DhanHistoricalDataClient,
    *,
    security_id: str,
    exchange_segment: str,
    instrument_type: str,
    session: MarketSession,
    timeframe_minutes: int,
    lookback_sessions: int = 1,
    now: datetime | None = None,
    underlying_label: str = "",
) -> list[Candle]:
    """Warm-up candles spanning ``lookback_sessions`` prior sessions + today.

    Reaches back over the overnight gap so a *session-spanning* indicator
    (SuperTrend/EMA/ATR) is fully established before today's first live
    candle — matching a chart that has run continuously. This is the only
    fetch function ported from the reference (see the module docstring for
    why the today-only variant and ``fetch_previous_close`` are not).

    Returns ``[]`` without calling ``client`` when it isn't a trading day.
    Unlike a today-only fetch, this one is useful *before* today's open too —
    it naturally returns the requested prior session(s), and the
    still-forming-bucket cutoff below excludes today's not-yet-open candles on
    its own.

    Raises on transport/parse failure; the caller
    (:meth:`~common.warmup.manager.WarmupManager.warm`) falls back to a cold
    start.
    """
    tz_name = session.timezone
    current = now if now is not None else now_tz(tz_name)

    if not session.is_trading_day(current):
        return []

    from_day = _prior_trading_day(
        session, current.astimezone(get_tz(tz_name)).date(), max(1, lookback_sessions)
    )
    from_at = combine(from_day, time(0, 0), tz_name)

    resp = client.fetch_intraday(
        security_id=security_id,
        exchange_segment=exchange_segment,
        instrument_type=instrument_type,
        from_at=from_at,
        to_at=current,
        interval_minutes=1,
    )

    one_min = parse_intraday_response(resp, tz_name)
    aggregated = aggregate_candles(
        one_min,
        timeframe_minutes,
        security_id=security_id,
        instrument=underlying_label or security_id,
    )

    # Drop the still-forming current bucket (and anything at/after it), and
    # anything outside the session window, so warm-up and the live feed never
    # double-count or gap at the boundary. is_applicable_session_bucket is the
    # same shared check WarmupManager's own completeness verification uses --
    # it checks the candle's *full interval* against [session.start,
    # session.square_off), not just its start (a start-only check would admit
    # a terminal bucket like 15:15-15:30 when square_off is 15:20).
    current_bucket = floor_to_interval(current, timeframe_minutes * 60)
    return [
        c
        for c in aggregated
        if is_applicable_session_bucket(session, c.start_at, timeframe_minutes)
        and c.start_at < current_bucket
    ]
