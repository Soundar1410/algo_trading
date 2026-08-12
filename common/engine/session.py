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

Timezone rule (Phase 4 Part 3)
------------------------------
Every predicate here resolves its argument **into the session's timezone** before
comparing, through :func:`common.utils.timeutils.local_time_in`. Until Part 3 they
read ``.time()`` straight off the argument, which is correct only when the caller
happens to pass an IST-offset datetime. Live ticks are UTC-aware, so a 10:00 IST
tick arrived as ``04:30+00:00`` and :meth:`is_open` returned False for the whole
session — the engine built no candles and placed no orders, silently. A naive
datetime is now refused rather than guessed at.

``square_off`` survives as an *attribute* rather than a decision: :meth:`is_open`
needs an upper bound and :attr:`fingerprint` must hash every value that could move
a boundary. It is derived from the policy at wiring time — see
:meth:`~common.engine.config.SessionConfig.from_square_off_policy` — so the two
configured times cannot drift apart.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from common.utils.timeutils import combine, local_date_in, local_time_in, now_tz, parse_hhmm

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
    def timezone(self) -> str:
        """The session's IANA zone. Public because every predicate's answer is
        relative to it, so a caller resolving a timestamp itself must use the
        same one — see :class:`~common.engine.square_off.SessionSquareOffAuthority`."""
        return self._tz

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

    def _local(self, at: datetime | None, argument: str) -> tuple[date, time]:
        """The session-local date and time of ``at``. **The only clock reading.**

        Every predicate below goes through here so none of them can drift back
        into reading ``.time()`` off an argument whose offset is not the
        session's — see :func:`common.utils.timeutils.local_time_in` for what
        that cost before Part 3.
        """
        moment = self._resolve(at)
        return (
            local_date_in(moment, self._tz, argument=argument),
            local_time_in(moment, self._tz, argument=argument),
        )

    def is_holiday(self, at: datetime | None = None) -> bool:
        """True if ``at`` falls on a configured trading holiday, in session time."""
        day, _ = self._local(at, "at")
        return day.isoformat() in self._holidays

    def is_trading_day(self, at: datetime | None = None) -> bool:
        """True on weekdays that are not configured holidays, in session time."""
        day, _ = self._local(at, "at")
        return self._is_trading_day(day)

    def is_open(self, at: datetime | None = None) -> bool:
        """True if ``at`` is a trading day within [start_time, square_off_time]."""
        day, clock = self._local(at, "at")
        return self._is_trading_day(day) and self.start <= clock <= self.square_off

    def can_enter(self, at: datetime | None = None) -> bool:
        """True if a *new* position may be opened (trading day, within [start, end))."""
        day, clock = self._local(at, "at")
        return self._is_trading_day(day) and self.start <= clock < self.end

    def _is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day.isoformat() not in self._holidays

    def prior_trading_day(self, day: date, sessions_back: int) -> date:
        """The trading date ``sessions_back`` sessions before ``day`` (exclusive).

        ``sessions_back == 1`` -> the previous trading day, skipping weekends
        and configured holidays. Bounded so a long holiday run can't loop
        forever. The shared calendar-walk primitive: any caller that needs
        "N trading days before this one" (warm-up history range, warm-up
        completeness verification) goes through this rather than
        reimplementing weekend/holiday rules against :meth:`is_trading_day`
        itself. Originally lived as a module-private free function in
        :mod:`common.warmup.historical` (``_prior_trading_day``, which now
        delegates here) — moved onto the class that already owns the
        calendar rules, so a second consumer outside that module (the
        warm-up manager's own completeness check) doesn't have to reach into
        another module's private helper for a rule it doesn't own.
        """
        d = day
        remaining = max(1, sessions_back)
        for _ in range(sessions_back * 5 + 14):  # generous upper bound on calendar hops
            d = d - timedelta(days=1)
            probe = combine(d, time(0, 0), self._tz)
            if self.is_trading_day(probe):
                remaining -= 1
                if remaining == 0:
                    break
        return d

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"MarketSession(start={self.start}, end={self.end}, "
            f"square_off={self.square_off}, tz={self._tz!r})"
        )
