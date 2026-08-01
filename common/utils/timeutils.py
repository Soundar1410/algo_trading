"""Time helpers.

Ported from the reference repository's ``framework/utils/timeutils.py``. Part 2a
brought across only :func:`parse_hhmm` — the single function the exit registry
needs (``time_exit`` parses its configured square-off time from a ``"HH:MM"``
string) — and recorded that the rest belonged with the engine port, where it
would have callers. Part 2b-i is that port, so the remainder arrives here now:
:func:`now_ist`/:func:`now_tz`/:func:`get_tz` (the engine's clock),
:func:`parse_timeframe_minutes` (``cfg.timeframe`` → a candle interval) and
:func:`combine`.

``floor_to_interval`` is deliberately **not** ported: this repository already has
one in :mod:`common.candles.aggregator`, keyed in seconds rather than minutes.
:class:`~common.candles.builder.CandleBuilder` reuses it rather than introducing a
second flooring rule that could disagree with the hub's about where a bar starts.

Note that this repository also anchors session times differently:
:class:`~common.risk.squareoff.SquareOffPolicy` holds ``datetime.time`` objects
directly rather than parsing strings, so there was no existing helper to reuse.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

#: The exchange this project trades on. Every "now" the engine uses comes from
#: here rather than a naive ``datetime.now()``.
DEFAULT_TZ = "Asia/Kolkata"


def get_tz(tz_name: str = DEFAULT_TZ) -> ZoneInfo:
    """Return a tzinfo for the given IANA timezone name."""
    return ZoneInfo(tz_name)


def now_tz(tz_name: str = DEFAULT_TZ) -> datetime:
    """Current timezone-aware datetime in the given timezone."""
    return datetime.now(get_tz(tz_name))


def now_ist() -> datetime:
    """Convenience: current time in IST (Asia/Kolkata)."""
    return now_tz(DEFAULT_TZ)


def combine(day: date, t: time, tz_name: str = DEFAULT_TZ) -> datetime:
    """Combine a date and a time into a timezone-aware datetime."""
    return datetime.combine(day, t, tzinfo=get_tz(tz_name))


class NaiveDatetimeError(ValueError):
    """A timezone-naive datetime reached a decision that needs a real instant."""


def local_time_in(moment: datetime, tz_name: str = DEFAULT_TZ, *, argument: str = "moment") -> time:
    """Return ``moment``'s wall-clock time **in** ``tz_name``.

    The one conversion every session and square-off comparison must go through.
    It exists because Phase 4 Part 3 found the same instant being resolved two
    different ways: :class:`~common.risk.squareoff.SquareOffPolicy` converted
    before comparing, while :class:`~common.engine.session.MarketSession` and
    :class:`~common.engine.square_off.SessionSquareOffAuthority` read ``.time()``
    straight off the argument.

    That is not a cosmetic inconsistency. ``DhanMarketFeedAdapter`` produces
    **UTC-aware** ticks, so a real 10:00 IST tick arrives as ``04:30+00:00`` and
    an unconverted ``.time()`` compares ``04:30`` against an IST session opening
    at ``09:15`` — the market looks shut for the entire session and the engine
    silently trades nothing. Every fixture in the tree is IST-offset, so no test
    could see it.

    **A naive ``moment`` is refused rather than interpreted.** Python would treat
    it as system-local, which is precisely the class of bug this function exists
    to end; treating it as IST would instead hide that a caller lost its timezone
    somewhere upstream. Neither guess is safe, so the caller is told.

    Raises:
        NaiveDatetimeError: if ``moment`` has no ``tzinfo``.
    """
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise NaiveDatetimeError(
            f"{argument} must be timezone-aware to be resolved in {tz_name}; got a naive "
            f"{moment.isoformat()}. Whoever produced it dropped the timezone — fix it "
            "there rather than assuming one here."
        )
    return moment.astimezone(get_tz(tz_name)).timetz().replace(tzinfo=None)


def local_date_in(moment: datetime, tz_name: str = DEFAULT_TZ, *, argument: str = "moment") -> date:
    """Return ``moment``'s calendar date **in** ``tz_name``.

    The date half of :func:`local_time_in`, and it matters for the same reason:
    a 23:50 IST instant is still ``2026-08-03`` locally while already
    ``2026-08-03 18:20`` in UTC — and near midnight the two disagree on the day,
    which decides holiday and weekend answers.
    """
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise NaiveDatetimeError(
            f"{argument} must be timezone-aware to be resolved in {tz_name}; got a naive "
            f"{moment.isoformat()}."
        )
    return moment.astimezone(get_tz(tz_name)).date()


def parse_timeframe_minutes(timeframe: str) -> int:
    """Convert a timeframe string like ``"5m"``, ``"1h"`` to minutes.

    Supports ``m`` (minutes) and ``h`` (hours) suffixes.

    Raises:
        ValueError: if the format is unrecognised.
    """
    text = str(timeframe).strip().lower()
    if text.endswith("m"):
        return int(text[:-1])
    if text.endswith("h"):
        return int(text[:-1]) * 60
    raise ValueError(f"Unrecognised timeframe {timeframe!r}; expected e.g. '5m' or '1h'.")


def parse_hhmm(value: str) -> time:
    """Parse a ``"HH:MM"`` (or ``"HH:MM:SS"``) string into a ``time``.

    Raises:
        ValueError: if the string is not a valid time.
    """
    text = str(value).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid time string {value!r}; expected 'HH:MM' or 'HH:MM:SS'.")
