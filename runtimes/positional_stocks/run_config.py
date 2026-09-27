"""The weekly run's configuration (spec 10.3, 12, 13).

:class:`RunConfig`'s defaults are the code's own values, **fail closed**:
both ``enabled`` flags default to ``False``, so a run built without the
committed YAML refuses (D115). The real run loads
``config/runtimes/positional_stocks.yaml`` and
``config/strategies/positional_stocks/wsr1_weekly_stochrsi.yaml`` through
:meth:`RunConfig.from_config` (Phase 5, Part B).

**Both flags gate the job (D115, supersedes spec 12's "only the strategy
flag").** ``fetch`` and any writing ``decide`` refuse while either the
runtime's or the strategy's ``enabled`` is false; ``--dry-run`` and the replay
harness still run. Go-live is one operator commit setting both to ``true``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from strategies.positional_stocks.wsr1_weekly_stochrsi.models import RulesParameters
from strategies.positional_stocks.wsr1_weekly_stochrsi.pacing import (
    DEFAULT_DECIDE_DEADLINE_MINUTES,
)

from .database import RUNTIME_ID

STRATEGY_ID = "wsr1_weekly_stochrsi"
#: The index series in the daily cache (spec 4.3: NIFTY 50).
INDEX_SYMBOL = "NIFTY"
#: Snapshots of positional_stocks.db kept in data/backups/ (D104).
BACKUPS_KEPT = 12

_WEEKDAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")


class RunRefused(RuntimeError):
    """The run must not start: not paper, or a safety check failed."""


@dataclass(frozen=True, slots=True)
class Slot:
    """One weekly time, e.g. ``SATURDAY 08:00`` (IST). ``weekday`` is
    Python's: Monday 0 … Sunday 6."""

    weekday: int
    at: time

    @classmethod
    def parse(cls, text: str) -> Slot:
        """``"SATURDAY 08:00"`` -> ``Slot(5, 08:00)``. Raises ``ValueError``."""
        try:
            day, clock = text.split()
            hour, minute = clock.split(":")
            return cls(_WEEKDAYS.index(day.upper()), time(int(hour), int(minute)))
        except (ValueError, IndexError) as exc:
            raise ValueError(f"schedule slot must be like 'SATURDAY 08:00', got {text!r}") from exc

    def text(self) -> str:
        return f"{_WEEKDAYS[self.weekday]} {self.at:%H:%M}"

    def first_on_or_after(self, day: date) -> date:
        return day + timedelta(days=(self.weekday - day.weekday()) % 7)


@dataclass(frozen=True, slots=True)
class Schedule:
    """Spec 10.3 v1.3: fetch Saturday 08:00, retried Saturday 14:00 and
    Sunday 10:00; decide Monday 08:30 — ``parameters.schedule``. The plists
    are generated from the same values."""

    fetch_attempts: tuple[Slot, ...] = (
        Slot(5, time(8, 0)),
        Slot(5, time(14, 0)),
        Slot(6, time(10, 0)),
    )
    decide: Slot = Slot(0, time(8, 30))

    def decide_after(self, week_ending: date, tz: object) -> datetime:
        """The first decide slot after ``week_ending`` (the week's last session)."""
        day = self.decide.first_on_or_after(week_ending + timedelta(days=1))
        return datetime.combine(day, self.decide.at, tz)  # type: ignore[arg-type]

    def is_final_fetch_attempt(self, now: datetime, week_ending: date) -> bool:
        """Spec 10.3 v1.3: a fetch run is the **final attempt** for the week
        ending ``week_ending`` when no scheduled fetch attempt lies strictly
        after ``now`` and before that week's decide slot — the Sunday 10:00
        run, or any run after it. Derived from the schedule and the clock;
        no flag. Only a final attempt alerts "not yet published"."""
        tz = now.tzinfo
        decide_at = self.decide_after(week_ending, tz)
        day = week_ending + timedelta(days=1)
        while day < decide_at.date() + timedelta(days=1):
            for slot in self.fetch_attempts:
                if slot.weekday == day.weekday():
                    at = datetime.combine(day, slot.at, tz)
                    if now < at < decide_at:
                        return False
            day += timedelta(days=1)
        return True


@dataclass(frozen=True, slots=True)
class RunConfig:
    strategy_id: str = STRATEGY_ID
    runtime_id: str = RUNTIME_ID
    #: Spec 10.2 step 1: anything but paper is refused in this spec version.
    mode: str = "paper"
    params: RulesParameters = field(default_factory=RulesParameters)
    index_symbol: str = INDEX_SYMBOL
    decide_deadline_minutes: float = DEFAULT_DECIDE_DEADLINE_MINUTES
    backups_kept: int = BACKUPS_KEPT
    schedule: Schedule = field(default_factory=Schedule)
    #: D115: ``config/runtimes/positional_stocks.yaml``'s ``enabled``.
    runtime_enabled: bool = False
    #: D115: the strategy YAML's ``enabled``.
    strategy_enabled: bool = False

    def check_paper(self) -> None:
        if self.mode != "paper":
            raise RunRefused(
                f"mode {self.mode!r} refused: this spec version runs {self.strategy_id} "
                "in paper mode only (spec 10.2 step 1)"
            )

    def disabled_reason(self) -> str | None:
        """D115: why the job may not fetch or write, or ``None``."""
        if not self.runtime_enabled:
            return f"runtime {self.runtime_id} is disabled in config"
        if not self.strategy_enabled:
            return f"strategy {self.strategy_id} is disabled in config"
        return None
