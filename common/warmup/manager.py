"""WarmupManager — generic, engine-agnostic indicator warm-up.

Ported from the reference repository's ``framework/warmup/manager.py``
(Phase 4 Part 4). Import paths only, plus one required field-name fix: the
reference's ``Candle`` exposes ``.start``; this repository's
:class:`common.models.Candle` is frozen with ``.start_at`` instead (Phase 3's
port, D19) — the closing detail message in :meth:`WarmupManager.warm` reads
``candles[-1].start_at`` accordingly. Everything else — gate order, the broad
``except Exception`` around the fetch and the replay loop, the
``_lookback_sessions`` arithmetic — is unchanged.

Fetches an instrument's historical candles and replays them through a
caller-supplied *sink* (which routes each candle to the strategy's normal
candle handler, discarding signals). Knows nothing about charts, legs, or
engines — the sink is the only coupling — so the same object serves every
engine. It also knows nothing about Dhan or REST: the actual fetch is
injected as ``fetch_fn``, so this class stays pure and unit-testable with a
synthetic candle source. See :mod:`common.warmup.historical` for the concrete
fetch this repository wires in production.

A spec that carries a session-local (VWAP) or live-volume-dependent
indicator is *skipped* (cold start), never replayed — a segmented,
scope-aware replay is not built here, so warming those would corrupt rather
than seed them. Any fetch or replay failure degrades to a cold start:
warm-up is a correctness nicety, never a precondition for trading, matching
the posture already proven at :meth:`common.engine.engine.TradingEngine._warm_up`'s
call site.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from common.candles.aggregator import floor_to_interval
from common.engine.session import MarketSession
from common.logging import get_logger
from common.utils.timeutils import combine, local_date_in

from .requirements import StrategyWarmupSpec
from .source import WarmupSource

if TYPE_CHECKING:
    from common.models import Candle

_log = get_logger(__name__)

# fetch_fn(source, *, session, timeframe_minutes, lookback_sessions, now) -> list[Candle]
FetchFn = Callable[..., list[Any]]
Sink = Callable[["Candle"], None]


@dataclass(frozen=True)
class WarmupResult:
    """Outcome of one warm-up attempt (for logging / observability)."""

    status: str  # WARMED | PARTIAL | COLD_START | SKIPPED_EMPTY |
    #              SKIPPED_SESSION_LOCAL | SKIPPED_VOLUME
    candles_replayed: int
    detail: str

    @property
    def warmed(self) -> bool:
        return self.status in ("WARMED", "PARTIAL")


class WarmupManager:
    """Fetch + replay historical candles to prime a strategy's indicators."""

    def __init__(
        self,
        fetch_fn: FetchFn,
        *,
        max_lookback_sessions: int = 1,
    ) -> None:
        self._fetch_fn = fetch_fn
        self._max_lookback_sessions = max(1, int(max_lookback_sessions))

    def warm(
        self,
        sink: Sink,
        source: WarmupSource,
        spec: StrategyWarmupSpec | None,
        *,
        session: MarketSession,
        timeframe_minutes: int,
        now: datetime | None = None,
    ) -> WarmupResult:
        """Warm the indicators reachable through ``sink`` from ``source``'s history.

        Returns a :class:`WarmupResult`; the caller logs it and proceeds either
        way (a non-``WARMED`` result simply means "cold start", never an error).
        """
        # --- Safety gates: refuse rather than corrupt -----------------------
        if spec is None or spec.is_empty:
            return WarmupResult("SKIPPED_EMPTY", 0, "no session-spanning indicators to warm")
        if spec.has_session_local:
            return WarmupResult(
                "SKIPPED_SESSION_LOCAL",
                0,
                "strategy holds a session-local indicator (e.g. VWAP); a segmented "
                "replay is not built here — cold-starting to avoid corruption",
            )
        if spec.requires_volume:
            return WarmupResult(
                "SKIPPED_VOLUME",
                0,
                "a session-spanning indicator needs live volume this stream does "
                "not carry — cold-starting to avoid an unmaintainable warm state",
            )

        lookback = self._lookback_sessions(spec, session, timeframe_minutes)

        # --- Fetch (best-effort) ---------------------------------------------
        try:
            candles = (
                self._fetch_fn(
                    source,
                    session=session,
                    timeframe_minutes=timeframe_minutes,
                    lookback_sessions=lookback,
                    now=now,
                )
                or []
            )
        except Exception as exc:
            return WarmupResult("COLD_START", 0, f"history fetch failed: {exc}")

        if not candles:
            return WarmupResult("COLD_START", 0, "no historical candles available to replay")

        # --- Replay (signals discarded by the sink) ---------------------------
        replayed = 0
        for candle in candles:
            try:
                sink(candle)
                replayed += 1
            except Exception as exc:
                return WarmupResult(
                    "PARTIAL",
                    replayed,
                    f"replay aborted after {replayed} candle(s): {exc}",
                )

        last = candles[-1].start_at
        base_detail = (
            f"replayed {replayed} candle(s) up to {last:%Y-%m-%d %H:%M} "
            f"(lookback {lookback} session(s))"
        )

        # --- Completeness verification ----------------------------------------
        # A successful fetch+replay above proves only that nothing raised — not
        # that the replay reached genuinely recent, complete, ordered,
        # duplicate-free coverage (Rev 3.1 gap: a stale cache or truncated
        # provider that doesn't raise could otherwise silently pass as WARMED).
        if now is None:
            return WarmupResult(
                "PARTIAL",
                replayed,
                f"{base_detail}; now was not provided, cannot verify recency/completeness",
            )

        expected = self._expected_recent_buckets(spec, session, timeframe_minutes, now)
        if expected is None:
            return WarmupResult(
                "PARTIAL",
                replayed,
                f"{base_detail}; calendar walk-back exceeded max_lookback_sessions "
                f"({self._max_lookback_sessions}) before accumulating the required "
                f"{spec.min_bars} bucket(s)",
            )

        window_start, window_end = expected[0], expected[-1]
        raw_recent = [c.start_at for c in candles if window_start <= c.start_at <= window_end]
        ordered = raw_recent == sorted(raw_recent)
        no_duplicates = len(raw_recent) == len(set(raw_recent))
        exact_coverage = set(raw_recent) == set(expected)
        if not (ordered and no_duplicates and exact_coverage):
            return WarmupResult(
                "PARTIAL",
                replayed,
                f"{base_detail}; warm-up coverage incomplete, stale, gapped, "
                f"duplicated or misordered: expected {len(expected)} bucket(s) "
                f"through {window_end:%Y-%m-%d %H:%M}, got {sorted(set(raw_recent))}",
            )

        return WarmupResult(
            "WARMED",
            replayed,
            f"{base_detail}; verified {len(expected)} required bucket(s) through "
            f"{window_end:%Y-%m-%d %H:%M}",
        )

    def _lookback_sessions(
        self, spec: StrategyWarmupSpec, session: MarketSession, timeframe_minutes: int
    ) -> int:
        """How many prior trading sessions to fetch to satisfy ``spec.min_bars``.

        One session at the strategy timeframe usually holds far more bars than
        any trend/MA indicator needs, so this is ``1`` in practice — but it
        scales up (capped by ``max_lookback_sessions``) for a long-period
        indicator. Reads ``session.start``/``.square_off`` as configured
        time-of-day bounds, not as an instant being resolved, so this is not
        subject to the timezone-conversion rule (D40) that governs comparing a
        caller-supplied ``datetime`` against those bounds.
        """
        start_m = session.start.hour * 60 + session.start.minute
        end_m = session.square_off.hour * 60 + session.square_off.minute
        per_session = max(1, (end_m - start_m) // max(1, timeframe_minutes))
        need = max(1, math.ceil(spec.min_bars / per_session))
        return min(need, self._max_lookback_sessions)

    @staticmethod
    def _session_day_buckets(
        session: MarketSession, day: date, timeframe_minutes: int
    ) -> list[datetime]:
        """All bucket-start timestamps for one trading day's full session,
        ascending: ``[session.start, session.square_off)``, stepped by
        ``timeframe_minutes`` — the same upper bound :meth:`_lookback_sessions`
        already measures against (``square_off``, not ``end``). Pure arithmetic
        over the session's configured time-of-day bounds; does not itself ask
        whether ``day`` is a trading day.
        """
        interval = timedelta(minutes=timeframe_minutes)
        start_dt = combine(day, session.start, session.timezone)
        end_dt = combine(day, session.square_off, session.timezone)
        buckets: list[datetime] = []
        cursor = start_dt
        while cursor + interval <= end_dt:
            buckets.append(cursor)
            cursor += interval
        return buckets

    def _required_latest_boundary(
        self, session: MarketSession, timeframe_minutes: int, now: datetime
    ) -> tuple[date, datetime]:
        """``(required_day, required_latest)`` — the most recent completed
        bucket-start that a genuinely current warm-up replay must reach.

        Market-open handling: if ``now`` falls on a trading day AND today
        already has at least one completed bucket, ``required_latest`` is
        today's latest completed bucket. Otherwise (before today's first
        bucket has closed, or today isn't a trading day at all) it is the
        previous trading session's final completed bucket — never today's
        unconditionally, so a run starting right at/before the open isn't
        forced to treat a perfectly valid previous-session context as
        incomplete.

        Post-market handling: once today has at least one completed bucket,
        the candidate boundary (``current_bucket - interval``) is capped at
        today's actual final session bucket. Well after ``square_off`` that
        candidate would otherwise be a timestamp that was never a real
        session bucket at all, which no fetched candle set could ever
        satisfy — the cap makes "started after the close" behave like
        "started right at the close": required coverage is today's real
        last bucket, not a nonexistent later one.
        """
        tz = session.timezone
        interval = timedelta(minutes=timeframe_minutes)
        today = local_date_in(now, tz)
        current_bucket = floor_to_interval(now, timeframe_minutes * 60)

        if session.is_trading_day(now):
            day_buckets = self._session_day_buckets(session, today, timeframe_minutes)
            if day_buckets and current_bucket - interval >= day_buckets[0]:
                required_latest = min(current_bucket - interval, day_buckets[-1])
                return today, required_latest

        prior_day = session.prior_trading_day(today, 1)
        prior_buckets = self._session_day_buckets(session, prior_day, timeframe_minutes)
        return prior_day, prior_buckets[-1]

    def _expected_recent_buckets(
        self,
        spec: StrategyWarmupSpec,
        session: MarketSession,
        timeframe_minutes: int,
        now: datetime,
    ) -> list[datetime] | None:
        """The ``spec.min_bars`` most recent completed bucket-starts required
        by handoff time ``now``, walking backward through the trading
        calendar as needed.

        The boundary day's own buckets (through :meth:`_required_latest_boundary`'s
        result) are the free base — always included, uncounted against the
        budget, matching :meth:`_lookback_sessions`'s existing
        "``lookback_sessions=1`` reaches one prior day beyond today" semantics.
        If that alone doesn't reach ``min_bars``, walk backward one
        *additional* prior trading day at a time via
        :meth:`~common.engine.session.MarketSession.prior_trading_day`,
        crossing overnight/weekend/holiday gaps legitimately — the calendar
        rule itself lives only in ``MarketSession``, nothing here
        reimplements weekend/holiday logic. Returns ``None`` if more than
        ``self._max_lookback_sessions`` *additional* prior sessions would be
        needed before accumulating ``min_bars`` buckets.
        """
        required_day, required_latest = self._required_latest_boundary(
            session, timeframe_minutes, now
        )
        accumulated = [
            b
            for b in self._session_day_buckets(session, required_day, timeframe_minutes)
            if b <= required_latest
        ]

        day = required_day
        prior_sessions_walked = 0
        while len(accumulated) < spec.min_bars:
            if prior_sessions_walked >= self._max_lookback_sessions:
                return None
            day = session.prior_trading_day(day, 1)
            accumulated = (
                self._session_day_buckets(session, day, timeframe_minutes) + accumulated
            )
            prior_sessions_walked += 1

        return accumulated[-spec.min_bars :]
