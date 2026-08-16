"""Positional lifecycle policy: new-trading-date detection, daily-counter
reset, and expiry-day timing (spec section 8) — evaluated against the
persisted ``resolved_expiry_date``, never against the word "Tuesday" or any
other weekday assumption.

Generic to any positional multi-leg strategy; contains no
``weekly_delta_neutral`` branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum

from common.utils.timeutils import combine, local_date_in, local_time_in

__all__ = [
    "ExpiryDayPhase",
    "PositionalLifecyclePolicy",
    "expiry_settlement_at",
]


def expiry_settlement_at(resolved_expiry_date: str, *, timezone: str = "Asia/Kolkata") -> datetime:
    """The actual settlement instant for one resolved expiry date — NSE
    index-option expiry settles at session close (15:30 local) — the
    Greeks model's time-to-expiry anchor (spec section 4.2: "time to expiry
    uses ... the actual resolved expiry"), never any exit-timing decision
    (those all go through :meth:`PositionalLifecyclePolicy.expiry_day_phase`'s
    own boundary clock instead). A free function, not a method, so both the
    generic engine (which owns a ``PositionalLifecyclePolicy``) and a
    strategy (which does not) can compute the identical value from nothing
    but the persisted expiry string — one definition, never duplicated.
    """
    expiry_date = date.fromisoformat(str(resolved_expiry_date)[:10])
    return combine(expiry_date, time(15, 30), timezone).astimezone(UTC)


class ExpiryDayPhase(StrEnum):
    """Which expiry-day regime ``now`` falls in — ``NOT_EXPIRY_DAY`` on every
    other session. Ordered; each later phase implies every earlier
    restriction still applies (spec section 8)."""

    #: Not the resolved expiry date at all — none of the section 8 rules
    #: apply; normal entry/adjustment/exit evaluation proceeds.
    NOT_EXPIRY_DAY = "NOT_EXPIRY_DAY"
    #: Before 12:00 on the expiry day — normal monitoring.
    NORMAL = "NORMAL"
    #: >= 12:00: tighten monitoring, no aggressive inward rolls.
    TIGHTEN = "TIGHTEN"
    #: >= 14:30: no normal delta adjustment; exit instead when intervention
    #: is required.
    NO_ADJUSTMENT = "NO_ADJUSTMENT"
    #: >= 15:05: begin planned complete exit.
    PLANNED_EXIT = "PLANNED_EXIT"
    #: >= 15:15: hard complete-exit deadline.
    HARD_EXIT = "HARD_EXIT"


@dataclass(frozen=True)
class PositionalLifecyclePolicy:
    """Pure, restart-safe timing computation — every answer is a function of
    ``now`` and the persisted ``resolved_expiry_date`` alone, so it never
    needs to be itself persisted or restored.

    Times are configurable (spec configuration contract); defaults match the
    spec's own numbers exactly.
    """

    timezone: str = "Asia/Kolkata"
    tighten_at: time = time(12, 0)
    no_adjustment_at: time = time(14, 30)
    planned_exit_at: time = time(15, 5)
    hard_exit_at: time = time(15, 15)

    def __post_init__(self) -> None:
        if not (
            self.tighten_at < self.no_adjustment_at < self.planned_exit_at < self.hard_exit_at
        ):
            raise ValueError(
                "expiry-day timing must satisfy tighten_at < no_adjustment_at < "
                "planned_exit_at < hard_exit_at"
            )

    def is_new_trading_date(self, *, last_seen_trading_date: str | None, now: datetime) -> bool:
        """True the first time ``now``'s local date differs from the last one
        this cycle observed — the sole trigger for resetting
        ``adjustments_today`` (never at cycle creation, never alongside
        ``adjustments_this_cycle``)."""
        today = local_date_in(now, self.timezone, argument="now").isoformat()
        return last_seen_trading_date is None or last_seen_trading_date != today

    def trading_date(self, now: datetime) -> str:
        return local_date_in(now, self.timezone, argument="now").isoformat()

    def expiry_day_phase(self, *, resolved_expiry_date: str, now: datetime) -> ExpiryDayPhase:
        today = local_date_in(now, self.timezone, argument="now")
        try:
            expiry_day = date.fromisoformat(str(resolved_expiry_date)[:10])
        except ValueError:
            # An unparseable expiry is a configuration/data defect the caller
            # must already have refused before this is ever reached (spec
            # section 3.4: block entry on an ambiguous/stale expiry) — here,
            # fail toward the safest reading rather than raise mid-evaluation.
            return ExpiryDayPhase.NOT_EXPIRY_DAY
        if today != expiry_day:
            return ExpiryDayPhase.NOT_EXPIRY_DAY

        clock = local_time_in(now, self.timezone, argument="now")
        if clock >= self.hard_exit_at:
            return ExpiryDayPhase.HARD_EXIT
        if clock >= self.planned_exit_at:
            return ExpiryDayPhase.PLANNED_EXIT
        if clock >= self.no_adjustment_at:
            return ExpiryDayPhase.NO_ADJUSTMENT
        if clock >= self.tighten_at:
            return ExpiryDayPhase.TIGHTEN
        return ExpiryDayPhase.NORMAL

    def adjustment_permitted_by_expiry_phase(self, phase: ExpiryDayPhase) -> bool:
        """Spec section 8: no normal delta adjustment from 14:30 on the
        expiry day — exit instead when intervention is required."""
        return phase not in (
            ExpiryDayPhase.NO_ADJUSTMENT,
            ExpiryDayPhase.PLANNED_EXIT,
            ExpiryDayPhase.HARD_EXIT,
        )

    def aggressive_inward_rolls_permitted(self, phase: ExpiryDayPhase) -> bool:
        """Spec section 8: from 12:00 on the expiry day, no aggressive inward
        rolls — a strictly-delta-improving roll is still permitted; the
        caller (the strategy's own adjustment-candidate ranking) is what
        actually distinguishes "aggressive/inward" from "safe", this is only
        the phase gate."""
        return phase not in (
            ExpiryDayPhase.TIGHTEN,
            ExpiryDayPhase.NO_ADJUSTMENT,
            ExpiryDayPhase.PLANNED_EXIT,
            ExpiryDayPhase.HARD_EXIT,
        )

    def planned_exit_due(self, phase: ExpiryDayPhase) -> bool:
        return phase in (ExpiryDayPhase.PLANNED_EXIT, ExpiryDayPhase.HARD_EXIT)

    def hard_exit_due(self, phase: ExpiryDayPhase) -> bool:
        return phase is ExpiryDayPhase.HARD_EXIT
