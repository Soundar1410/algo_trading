"""Time helpers.

Ported from the reference repository's ``framework/utils/timeutils.py`` in
Phase 3 Part 2a. Only :func:`parse_hhmm` is brought across — the single function
the exit registry needs (``time_exit`` parses its configured square-off time from
a ``"HH:MM"`` string). The rest of that module (``now_ist``, ``combine``,
``parse_timeframe_minutes`` and friends) belongs with the engine port in Part 2b,
where it actually has callers; porting it early would land untested code.

Note that this repository already anchors session times differently:
:class:`~common.risk.squareoff.SquareOffPolicy` holds ``datetime.time`` objects
directly rather than parsing strings, so there was no existing helper to reuse.
"""

from __future__ import annotations

from datetime import datetime, time


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
