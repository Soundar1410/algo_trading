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
from datetime import datetime
from typing import TYPE_CHECKING, Any

from common.engine.session import MarketSession
from common.logging import get_logger

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
        return WarmupResult(
            "WARMED",
            replayed,
            f"replayed {replayed} candle(s) up to {last:%Y-%m-%d %H:%M} "
            f"(lookback {lookback} session(s))",
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
