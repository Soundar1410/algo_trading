"""Unexplained price gaps and their acknowledgements (spec 6.1).

Dhan's daily history is back-adjusted, but not uniformly (D94): a corporate
action it failed to apply shows up as a close-to-close move no market event
explains. A move of **30% or more is flagged** and blocks new entries until the
operator acknowledges it; a move of **15-30% is reported** only. The measure is
close to previous close, the same one D94 and the Phase 2 full-history scan
used.

* **Keyed acknowledgements (v1.2b).** An acknowledgement names the symbol, the
  gap session and the ratio. A new gap, or the same session restated to a
  different ratio, is not covered by it.
* **Only the most recent 520 weekly bars block (v1.2e).** An older break is
  reported but never blocks: a 1:1 bonus left unadjusted 520 weeks ago moves
  EMA200 by about 0.3%, inside parity tolerance, and every other indicator here
  looks back far less.

Pure: no I/O and no clock. The operator's CSV is parsed by
:func:`~.inputs.load_gap_acknowledgements`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise

from .iso_weeks import monday_of
from .models import DailyBar, GapAcknowledgement, WeeklyBar

#: Spec 6.1: flag (and block) at 30%, report at 15%.
FLAG_MOVE = 0.30
REPORT_MOVE = 0.15
#: Spec 6.1 v1.2e: only gaps inside the most recent 520 weekly bars block.
BLOCK_WINDOW_BARS = 520
#: Acknowledgements match a gap on its ratio to this precision.
RATIO_PLACES = Decimal("0.0001")


def gap_ratio(previous_close: float, close: float) -> Decimal:
    """close / previous close, to 4 decimals — the acknowledgement key."""
    return (Decimal(str(close)) / Decimal(str(previous_close))).quantize(
        RATIO_PLACES, rounding=ROUND_HALF_UP
    )


@dataclass(frozen=True, slots=True)
class Gap:
    """One close-to-close move of 15% or more."""

    symbol: str
    session: date
    previous_session: date
    previous_close: float
    close: float
    ratio: Decimal

    @property
    def move(self) -> float:
        return self.close / self.previous_close - 1.0

    @property
    def flagged(self) -> bool:
        """30% or more: blocks new entries until acknowledged (if recent)."""
        return abs(self.move) >= FLAG_MOVE


def scan(symbol: str, daily: Iterable[DailyBar]) -> list[Gap]:
    """Every close-to-close move of 15% or more in ``daily``, oldest first."""
    ordered = sorted(daily, key=lambda bar: bar.session)
    gaps: list[Gap] = []
    for before, after in pairwise(ordered):
        if abs(after.close / before.close - 1.0) >= REPORT_MOVE:
            gaps.append(
                Gap(
                    symbol=symbol,
                    session=after.session,
                    previous_session=before.session,
                    previous_close=before.close,
                    close=after.close,
                    ratio=gap_ratio(before.close, after.close),
                )
            )
    return gaps


def block_window_start(weekly: Sequence[WeeklyBar], bars: int = BLOCK_WINDOW_BARS) -> date:
    """The Monday of the oldest of the most recent ``bars`` weekly bars.

    A gap on or after this date can block; an older one is reported only. With
    fewer bars than that, the whole history is inside the window.
    """
    if not weekly:
        return date.min
    first = weekly[-bars] if len(weekly) >= bars else weekly[0]
    return monday_of(first.iso_key)


def is_acknowledged(gap: Gap, acknowledgements: Iterable[GapAcknowledgement]) -> bool:
    return any(
        ack.symbol == gap.symbol and ack.gap_session == gap.session and ack.ratio == gap.ratio
        for ack in acknowledgements
    )


def blocking_gaps(
    gaps: Iterable[Gap],
    acknowledgements: Iterable[GapAcknowledgement],
    *,
    window_start: date,
) -> list[Gap]:
    """Flagged gaps inside the block window that no acknowledgement covers."""
    acks = tuple(acknowledgements)
    return [
        gap
        for gap in gaps
        if gap.flagged and gap.session >= window_start and not is_acknowledged(gap, acks)
    ]
