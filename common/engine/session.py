"""Trading-session helper.

Ported from the reference repository's ``framework/utils/session.py``. Answers the
time-of-day questions the engine asks on every tick:

* Is the market within the active trading window?
* May we still take a *new* signal (i.e. before ``end_time``)?
* Is today a trading day at all?

Relationship to :class:`~common.risk.squareoff.SquareOffPolicy`
--------------------------------------------------------------
Both objects used to answer "is it square-off time?", from different state, which
meant two deciders against one position. **Part 2b-ii-B-1 settled it**: the policy
owns square-off, because it is a pure function of clock *and persisted state* and
therefore reaches the same conclusion after a restart, while this class knows only
the clock. Execution §10 requires that a restart not reset square-off state, and
architecture §13 places square-off at runtime level and the entry cutoff at
strategy level.

So ``is_past_square_off`` is gone, and the engine asks a
:class:`~common.engine.square_off.SquareOffAuthority` instead. What stays here is
what the policy has no notion of: the entry window (:meth:`can_enter`), the active
window (:meth:`is_open`) and the trading-day/holiday calendar.

``square_off`` survives as an *attribute* rather than a decision: :meth:`is_open`
needs an upper bound and :attr:`fingerprint` must hash every value that could move
a boundary. It is derived from the policy at wiring time — see
:meth:`~common.engine.config.SessionConfig.from_square_off_policy` — so the two
configured times cannot drift apart.
"""

from __future__ import annotations

from datetime import datetime, time

from common.utils.timeutils import now_tz, parse_hhmm

from .config import SessionConfig


class MarketSession:
    """Answers intraday timing questions for the trading session."""

    def __init__(self, cfg: SessionConfig) -> None:
        self._tz = cfg.timezone
        self.start: time = parse_hhmm(cfg.start_time)
        self.end: time = parse_hhmm(cfg.end_time)
        self.square_off: time = parse_hhmm(cfg.square_off_time)
        self._holidays: set[str] = {str(d) for d in (cfg.holidays or ())}

        if not (self.start < self.end <= self.square_off):
            raise ValueError(
                "Session times must satisfy start < end <= square_off "
                f"(got start={cfg.start_time}, end={cfg.end_time}, "
                f"square_off={cfg.square_off_time})."
            )

    @property
    def fingerprint(self) -> str:
        """Stable identity of this session's timing + calendar.

        Two sessions with the same fingerprint resolve every date/time question
        identically, so history fetched under one is valid under the other.
        Deliberately over-inclusive: every field that could move a boundary is
        hashed. Getting this wrong costs a cache miss in one direction and *wrong
        history* in the other, so it errs toward missing.
        """
        import hashlib

        canonical = "|".join(
            [
                self._tz,
                self.start.isoformat(),
                self.end.isoformat(),
                self.square_off.isoformat(),
                ",".join(sorted(self._holidays)),
            ]
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def _resolve(self, at: datetime | None) -> datetime:
        return at if at is not None else now_tz(self._tz)

    def is_holiday(self, at: datetime | None = None) -> bool:
        """True if ``at`` falls on a configured trading holiday."""
        return self._resolve(at).strftime("%Y-%m-%d") in self._holidays

    def is_trading_day(self, at: datetime | None = None) -> bool:
        """True on weekdays that are not configured holidays."""
        d = self._resolve(at)
        return d.weekday() < 5 and not self.is_holiday(d)

    def is_open(self, at: datetime | None = None) -> bool:
        """True if ``at`` is a trading day within [start_time, square_off_time]."""
        d = self._resolve(at)
        return self.is_trading_day(d) and self.start <= d.time() <= self.square_off

    def can_enter(self, at: datetime | None = None) -> bool:
        """True if a *new* position may be opened (trading day, within [start, end))."""
        d = self._resolve(at)
        return self.is_trading_day(d) and self.start <= d.time() < self.end

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"MarketSession(start={self.start}, end={self.end}, "
            f"square_off={self.square_off}, tz={self._tz!r})"
        )
