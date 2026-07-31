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
