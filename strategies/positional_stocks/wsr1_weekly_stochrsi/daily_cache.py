"""Symbol-keyed on-disk cache of daily bars, and the staleness check (spec 6.2, 10.3).

This is what makes the Monday decision run offline. Spec 10.3 records the three
verified blockers, and the first one is a cache-key decision: ``ScripMasterCache``
keys its file by the IST date, so a Monday call to it would download. So this
cache is **keyed by symbol** — never by instrument id, never by run date — and
``--mode decide`` reads it having resolved nothing and authenticated nowhere.
``security_id`` is stored as provenance only; nothing reads it back to find a file.

Written atomically (temp file in the same directory, ``fsync``, ``os.replace``),
the same way :meth:`~common.market_data.scrip_master.ScripMasterCache._write`
writes the instrument master and for the same reason: a truncated series parses
cleanly and produces *some* weekly bars, so the failure would surface as an
inexplicable indicator value rather than as a read error. No ``0600`` mode —
these are public daily prices, not a credential.

Corporate actions and the overlap check
---------------------------------------
Dhan's daily history is **back-adjusted**: verified on 2026-09-21 across 2,160
sessions for RELIANCE, HDFCBANK and NIFTY 50 spanning both the Reliance 1:1
(Oct 2024) and HDFC Bank 1:1 (Aug 2025) bonuses, with no close-to-close move of
30% or more anywhere.

Back-adjustment is what makes a naive tail-only refetch unsafe. When a symbol
splits, every historical price Dhan serves is restated — so bars cached before
the event and bars fetched after it are on *different scales*, and appending one
to the other produces a series with an invented 50% gap in it. Spec 6.1 says
"refetch only the missing tail"; that is **superseded here**, because it is only
correct in the absence of corporate actions.

So :func:`merge_tail` refetches with an overlap of
:data:`OVERLAP_SESSIONS` already-cached sessions, always takes the freshly
fetched values for the overlapping dates, and compares closes. Agreement within
:data:`MAX_OVERLAP_DRIFT` means the scale is unchanged and the merge is sound.
Any larger disagreement means the series was restated, and the whole cached
history for that symbol is discarded and refetched from scratch — reported, not
silently patched.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from common.logging import get_logger
from common.utils.timeutils import DEFAULT_TZ

from .models import BarError, DailyBar
from .weekly_bars import iso_key

if TYPE_CHECKING:  # pragma: no cover - import cycle: trading_calendar imports iso_key
    from .trading_calendar import TradingCalendar

_log = get_logger(__name__)

#: Bumped whenever the on-disk shape changes. A file written by an older
#: version is treated as absent rather than parsed hopefully.
SCHEMA_VERSION = 1

#: How many already-cached sessions a tail refetch overlaps. Ten sessions is
#: two trading weeks — comfortably more than any settlement or publication lag,
#: and small enough that the extra payload is free.
OVERLAP_SESSIONS = 10

#: Relative close difference across the overlap that still counts as "the same
#: series". Dhan's closes are published to two decimals, so ordinary
#: representation noise is orders of magnitude below this; anything above it is
#: a restatement, not rounding.
MAX_OVERLAP_DRIFT = 0.005

#: NSE symbols are upper-case alphanumerics plus these three punctuation marks
#: (``M&M``, ``BAJAJ-AUTO``, ``NIFTY.NS``-style suffixes). Anything else is
#: refused rather than sanitised: silently mangling a symbol into a filename
#: would make two symbols share a cache file.
_SAFE_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&.\-]{0,31}$")


class UnsafeSymbolError(ValueError):
    """A symbol cannot be used as a cache filename."""


class MergeStatus(Enum):
    """What :func:`merge_tail` concluded about a refetched tail."""

    #: The overlap agreed; the merged series is usable.
    MERGED = "merged"
    #: The overlap disagreed beyond :data:`MAX_OVERLAP_DRIFT`. The cached
    #: history is on a pre-adjustment scale and must be discarded entirely.
    RE_ADJUSTED = "re_adjusted"


@dataclass(frozen=True, slots=True)
class OverlapDrift:
    """One overlapping session whose close was restated."""

    session: date
    cached_close: float
    fetched_close: float

    @property
    def relative(self) -> float:
        if self.cached_close == 0:
            return float("inf")
        return abs(self.fetched_close - self.cached_close) / self.cached_close


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """The result of folding a refetched tail into a cached series."""

    status: MergeStatus
    bars: tuple[DailyBar, ...]
    compared: int
    drifts: tuple[OverlapDrift, ...] = ()

    @property
    def needs_full_refetch(self) -> bool:
        return self.status is MergeStatus.RE_ADJUSTED

    def describe(self, symbol: str) -> str:
        if self.status is MergeStatus.MERGED:
            return (
                f"{symbol}: merged tail, {self.compared} overlapping session(s) agreed, "
                f"{len(self.bars)} bars cached"
            )
        worst = max((d.relative for d in self.drifts), default=0.0)
        sessions = ", ".join(d.session.isoformat() for d in self.drifts[:3])
        return (
            f"{symbol}: series was restated (worst overlap drift {worst:.1%} on {sessions}) — "
            "cached history is on a pre-adjustment scale and must be refetched in full"
        )


def check_symbol(symbol: str) -> str:
    """Normalise a symbol and confirm it is safe as a filename.

    Raises:
        UnsafeSymbolError: the symbol is empty, over-long, or carries a
            character that has meaning in a path.
    """
    candidate = (symbol or "").strip().upper()
    if not _SAFE_SYMBOL.match(candidate):
        raise UnsafeSymbolError(
            f"Refusing to use {symbol!r} as a cache filename: expected upper-case "
            "alphanumerics with '&', '.' or '-' only."
        )
    return candidate


def refetch_from(cached: Sequence[DailyBar], *, overlap: int = OVERLAP_SESSIONS) -> date | None:
    """The date a tail refetch should start at, to overlap ``overlap`` sessions.

    ``None`` when there is nothing cached — the caller fetches full history.
    """
    if not cached:
        return None
    return cached[max(0, len(cached) - overlap)].session


def merge_tail(
    cached: Sequence[DailyBar],
    fetched: Sequence[DailyBar],
    *,
    max_drift: float = MAX_OVERLAP_DRIFT,
) -> MergeOutcome:
    """Fold a refetched tail into a cached series, checking the overlap first.

    The fetched values always win for any date they cover — they are the more
    recently restated view, and preferring the cached copy would preserve
    precisely the stale scale this check exists to detect.

    An overlap of zero sessions (the cached series and the fetched one do not
    touch) merges without a verdict, because there is nothing to compare; the
    caller is responsible for not creating that gap, which
    :func:`refetch_from` is what prevents.
    """
    cached_by_session = {bar.session: bar for bar in cached}
    fetched_by_session = {bar.session: bar for bar in fetched}

    drifts = [
        OverlapDrift(
            session=session,
            cached_close=cached_by_session[session].close,
            fetched_close=fetched_by_session[session].close,
        )
        for session in sorted(cached_by_session.keys() & fetched_by_session.keys())
    ]
    breached = tuple(drift for drift in drifts if drift.relative > max_drift)

    if breached:
        # Deliberately returns no bars: a partially-rescaled series is worse
        # than none, because it parses and computes.
        return MergeOutcome(
            status=MergeStatus.RE_ADJUSTED,
            bars=(),
            compared=len(drifts),
            drifts=breached,
        )

    merged = {**cached_by_session, **fetched_by_session}
    return MergeOutcome(
        status=MergeStatus.MERGED,
        bars=tuple(merged[session] for session in sorted(merged)),
        compared=len(drifts),
    )


class DailyBarCache:
    """One JSON file per symbol, under ``<cache_root>/positional_stocks/daily``.

    ``data/cache/`` is already gitignored, so nothing here can reach a commit.
    """

    def __init__(self, root: Path, *, tz_name: str = DEFAULT_TZ) -> None:
        self._root = Path(root)
        self._tz_name = tz_name

    @classmethod
    def under(cls, cache_root: Path, *, tz_name: str = DEFAULT_TZ) -> DailyBarCache:
        """Build from :attr:`~common.config.paths.ProjectPaths.cache_root`."""
        return cls(Path(cache_root) / "positional_stocks" / "daily", tz_name=tz_name)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, symbol: str) -> Path:
        return self._root / f"{check_symbol(symbol)}.json"

    # ------------------------------------------------------------------ read
    def read(self, symbol: str) -> list[DailyBar]:
        """Cached sessions for ``symbol``, oldest first. Empty when absent.

        A file that is missing, unreadable, of an unknown schema version or
        internally inconsistent is treated as **absent**, never as an error:
        the answer to a bad cache file is to refetch the symbol, and in
        ``decide`` mode an empty series is caught by the staleness check, which
        already fails closed with a clear reason.
        """
        path = self.path_for(symbol)
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("ignoring unreadable daily cache %s: %s", path, exc)
            return []
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            _log.warning("ignoring daily cache %s: unknown schema version", path)
            return []

        bars: list[DailyBar] = []
        for row in payload.get("bars") or []:
            try:
                session, open_, high, low, close, volume = row
                bars.append(
                    DailyBar(
                        session=date.fromisoformat(session),
                        open=float(open_),
                        high=float(high),
                        low=float(low),
                        close=float(close),
                        volume=float(volume),
                    )
                )
            except (TypeError, ValueError, BarError):
                _log.warning("ignoring daily cache %s: a row is unusable", path)
                return []
        bars.sort(key=lambda bar: bar.session)
        return bars

    def metadata(self, symbol: str) -> dict[str, object]:
        """Everything the file records apart from the bars. ``{}`` when absent."""
        path = self.path_for(symbol)
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {key: value for key, value in payload.items() if key != "bars"}

    def symbols(self) -> tuple[str, ...]:
        if not self._root.is_dir():
            return ()
        return tuple(sorted(path.stem for path in self._root.glob("*.json")))

    # ----------------------------------------------------------------- write
    def write(
        self,
        symbol: str,
        bars: Iterable[DailyBar],
        *,
        fetched_at: datetime,
        security_id: str = "",
        exchange_segment: str = "",
        instrument: str = "",
    ) -> Path:
        """Replace ``symbol``'s cached series, atomically.

        ``security_id`` and friends are **provenance**, recorded so a stale or
        mis-resolved file can be recognised later. They are never part of the
        key, because the decision run resolves no instrument ids at all.
        """
        name = check_symbol(symbol)
        ordered = sorted(bars, key=lambda bar: bar.session)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "symbol": name,
            "security_id": str(security_id),
            "exchange_segment": exchange_segment,
            "instrument": instrument,
            "fetched_at": fetched_at.isoformat(),
            "first_session": ordered[0].session.isoformat() if ordered else None,
            "last_session": ordered[-1].session.isoformat() if ordered else None,
            "bars": [
                [
                    bar.session.isoformat(),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                ]
                for bar in ordered
            ],
        }
        return self._atomic_write(self.path_for(name), payload)

    def _atomic_write(self, path: Path, payload: dict[str, object]) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self._root, prefix=".daily_", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                # Without fsync the rename can be durable while the contents
                # are not, which on a power loss yields a valid-looking
                # truncated series.
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return path

    def discard(self, symbol: str) -> bool:
        """Delete ``symbol``'s cache file. Returns whether one was there.

        Called when :func:`merge_tail` reports a restated series: the whole
        history is wrong, so none of it is kept.
        """
        path = self.path_for(symbol)
        existed = path.is_file()
        path.unlink(missing_ok=True)
        return existed


# ------------------------------------------------------------------ staleness
@dataclass(frozen=True, slots=True)
class StalenessVerdict:
    """Whether one series covers the week being decided (spec 6.2)."""

    symbol: str
    required_session: date
    last_session: date | None

    @property
    def is_current(self) -> bool:
        return self.last_session is not None and self.last_session >= self.required_session

    @property
    def reason(self) -> str:
        if self.is_current:
            return ""
        if self.last_session is None:
            return f"{self.symbol}: no cached sessions for the week being decided"
        return (
            f"{self.symbol}: last cached session {self.last_session.isoformat()} is before the "
            f"week's last session {self.required_session.isoformat()}"
        )


@dataclass(frozen=True, slots=True)
class IndexPublicationVerdict:
    """Has the index series published the week's expected last session?

    This replaces Phase 1's ``reference_last_session``, which asked NIFTY 50's
    own data what the week's last session was. That is the defect the Phase 1
    review found: on a Friday when Dhan has not yet published the candle, the
    data answers "Thursday", every series in the universe agrees, and a
    Monday-Thursday bar is decided on. The expected session now comes from
    :meth:`~.trading_calendar.TradingCalendar.expected_last_session` and the
    index is checked *against* it rather than asked to supply it.

    ``unlisted_sessions`` carries sessions the exchange held on days the
    calendar calls non-trading — the Muhurat session, a budget Saturday. Spec
    6.2 requires them reported; they never move the expected session.
    """

    week: tuple[int, int]
    expected_session: date
    last_session: date | None
    unlisted_sessions: tuple[date, ...] = ()

    @property
    def is_published(self) -> bool:
        return self.last_session is not None and self.last_session >= self.expected_session

    @property
    def reason(self) -> str:
        if self.is_published:
            return ""
        iso_year, iso_week = self.week
        had = "none at all" if self.last_session is None else self.last_session.isoformat()
        return (
            f"NIFTY 50 has no session on {self.expected_session.isoformat()}, the last "
            f"session the calendar expects for {iso_year}-W{iso_week:02d} (latest present: "
            f"{had}). Either Dhan has not published it yet or the exchange closed on a day "
            "the verified holiday list does not carry. The week is NOT built from the "
            "sessions that are present; the run fails closed and the next attempt retries."
        )


def assess_index_publication(
    index_daily: Sequence[DailyBar],
    *,
    week: tuple[int, int],
    expected_session: date,
    calendar: TradingCalendar | None = None,
) -> IndexPublicationVerdict:
    """Spec 6.2's index gate. **If this is not published, the whole run stops.**

    A stale symbol is skipped and reported; a stale *index* stops everything,
    because the regime is then unknown and every entry decision depends on it.

    ``calendar`` is optional only because the unlisted-session list it produces
    is for the report, not for the verdict.
    """
    in_week = [bar.session for bar in index_daily if iso_key(bar.session) == week]
    unlisted = tuple(calendar.unlisted_sessions(in_week, week)) if calendar else ()
    return IndexPublicationVerdict(
        week=week,
        expected_session=expected_session,
        last_session=max(in_week) if in_week else None,
        unlisted_sessions=unlisted,
    )


def assess_staleness(
    series_by_symbol: dict[str, Sequence[DailyBar]],
    *,
    required_session: date,
) -> list[StalenessVerdict]:
    """One verdict per symbol, in symbol order.

    This reports; it does not decide. Spec 6.2's policy — skip a stale symbol,
    but stop the whole run if NIFTY 50 is stale because the regime is then
    unknown — belongs to the run, which is Phase 4.
    """
    return [
        StalenessVerdict(
            symbol=symbol,
            required_session=required_session,
            last_session=max((bar.session for bar in bars), default=None),
        )
        for symbol, bars in sorted(series_by_symbol.items())
    ]


def week_of(session: date) -> tuple[int, int]:
    """``(iso_year, iso_week)`` — re-exported so callers need one import."""
    return iso_key(session)
