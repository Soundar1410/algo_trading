"""Intraday square-off.

Two spec rules shape this:

* **New entries stop at the cutoff, before square-off itself.** They are
  separate times so a strategy cannot open a position at 15:14 that the 15:15
  square-off immediately closes at a loss.
* **A restart must not reset square-off state or re-open entries after the
  cutoff.** State therefore lives in ``strategy_state``, not in memory, and the
  decision is a pure function of (clock, persisted state) so a recovering worker
  reaches the same conclusion as the one that died.

The trigger is driven by the candle clock rather than wall time. In Phase 1 that
makes the whole slice deterministic — square-off happens because the tape
crossed 15:15, not because a test was slow — and in production it keeps
square-off aligned with the same exchange timestamps that drive signals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Kolkata"


class SquareOffState(StrEnum):
    """Persisted square-off progress for one strategy-day."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SquareOffTrigger(StrEnum):
    """Why the policy says to act now."""

    NONE = "NONE"
    BLOCK_NEW_ENTRIES = "BLOCK_NEW_ENTRIES"
    SQUARE_OFF = "SQUARE_OFF"


@dataclass(frozen=True)
class SquareOffPolicy:
    """Cutoff and square-off times for one intraday strategy."""

    entry_cutoff: time = time(15, 0)
    square_off_at: time = time(15, 15)
    timezone: str = DEFAULT_TIMEZONE

    def __post_init__(self) -> None:
        if self.square_off_at < self.entry_cutoff:
            raise ValueError(
                f"square_off_at {self.square_off_at} must not precede "
                f"entry_cutoff {self.entry_cutoff}"
            )

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def _local_time(self, moment: datetime) -> time:
        """Delegates to the shared helper, which this method used to be.

        Phase 4 Part 3 promoted this conversion into
        :func:`common.utils.timeutils.local_time_in` because it was the only
        place in the repository getting it right, while `MarketSession` and
        `SessionSquareOffAuthority` compared unconverted wall times. Behaviour
        here is unchanged in one respect and tightened in another: a naive
        ``moment`` used to be silently read as system-local and is now refused.
        """
        from common.utils.timeutils import local_time_in

        return local_time_in(moment, self.timezone, argument="moment")

    def entries_allowed(self, moment: datetime) -> bool:
        """False once the cutoff has passed — and it never becomes True again."""
        return self._local_time(moment) < self.entry_cutoff

    def trigger_at(self, moment: datetime, *, state: SquareOffState) -> SquareOffTrigger:
        """What should happen at this moment, given persisted progress.

        Pure: the same inputs always give the same answer, which is what lets a
        restarted worker resume without re-deciding the day.
        """
        if state in {SquareOffState.COMPLETED, SquareOffState.IN_PROGRESS}:
            return SquareOffTrigger.NONE

        local = self._local_time(moment)
        if local >= self.square_off_at:
            return SquareOffTrigger.SQUARE_OFF
        if local >= self.entry_cutoff:
            return SquareOffTrigger.BLOCK_NEW_ENTRIES
        return SquareOffTrigger.NONE
