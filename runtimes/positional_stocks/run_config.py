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

from dataclasses import dataclass, field, fields
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from strategies.positional_stocks.wsr1_weekly_stochrsi.models import RulesParameters
from strategies.positional_stocks.wsr1_weekly_stochrsi.pacing import (
    DEFAULT_DECIDE_DEADLINE_MINUTES,
    DEFAULT_FETCH_DEADLINE_MINUTES,
    DEFAULT_REQUESTS_PER_SECOND,
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
    fetch_deadline_minutes: float = DEFAULT_FETCH_DEADLINE_MINUTES
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

    @classmethod
    def from_config(cls, config_root: Path) -> RunConfig:
        """The committed configuration (spec 12), bound strictly.

        ``config/runtimes/positional_stocks.yaml`` and the strategy file are
        loaded through the existing ``common.config`` loaders (the same strict
        models every runtime uses). Each ``parameters`` key maps onto
        :class:`RulesParameters`, the deadlines or the schedule. A key the
        code does not make configurable — the stoch lengths, ``rs_weeks``, the
        regime index, the input and report paths — must equal the code's own
        value. Anything else refuses the run: an unknown key, a wrong type, a
        different fixed value.

        Raises:
            RunRefused: the configuration cannot be loaded or bound.
        """
        from common.config import load_runtime_config, load_strategy_config

        try:
            runtime = load_runtime_config(config_root, RUNTIME_ID)
            strategy = load_strategy_config(config_root, STRATEGY_ID, runtime_id=RUNTIME_ID)
        except Exception as exc:  # ConfigError or a validation error: refuse either way
            raise RunRefused(f"configuration: {exc}") from exc
        binder = _Binder(dict(strategy.parameters))
        params = binder.params()
        schedule = binder.schedule()
        deadlines = binder.section("deadlines")
        decide_minutes = binder.number(deadlines, "decide_minutes", "deadlines")
        fetch_minutes = binder.number(deadlines, "fetch_minutes", "deadlines")
        binder.fixed(
            deadlines, "fetch_requests_per_second", DEFAULT_REQUESTS_PER_SECOND, "deadlines"
        )
        binder.done(deadlines, "deadlines")
        binder.finish()
        return cls(
            strategy_id=strategy.strategy_id,
            runtime_id=runtime.runtime_id,
            mode=str(strategy.mode.value),
            params=params,
            decide_deadline_minutes=decide_minutes,
            fetch_deadline_minutes=fetch_minutes,
            schedule=schedule,
            runtime_enabled=runtime.enabled,
            strategy_enabled=strategy.enabled,
        )


#: Keys whose values the code fixes (spec 4.4, 4.3, 6.1, 11); the YAML must
#: repeat them exactly, so it never claims a setting the code ignores.
_FIXED: dict[str, dict[str, object]] = {
    "stoch": {"rsi_length": 14, "stoch_length": 14, "k": 3, "d": 3},
    "filters": {"rs_weeks": 26},
    "adds": {"require_close_above_prior_high": True},
    "regime": {"index": "NIFTY 50"},
    "paths": {"reports": "data/reports/positional_stocks"},
    "inputs": {
        "universe": "config/positional_stocks/universe.csv",
        "quality_gate": "config/positional_stocks/quality_gate.csv",
        "results_calendar": "config/positional_stocks/results_calendar.csv",
        "require_results_calendar": False,
    },
}

#: ``parameters`` key -> (section or None for top level, RulesParameters field).
_RULES: dict[str, tuple[str | None, str]] = {
    "capital": (None, "capital"),
    "base_allocation": (None, "base_allocation"),
    "max_positions": (None, "max_positions"),
    "committed_cap_pct": (None, "committed_cap_pct"),
    "buffer_pct": (None, "buffer_pct"),
    "max_per_sector": (None, "max_per_sector"),
    "max_per_group": (None, "max_per_group"),
    "stoch.arm_level": ("stoch", "arm_level"),
    "stoch.arm_window_weeks": ("stoch", "arm_window_weeks"),
    "stoch.max_k_at_cross": ("stoch", "max_k_at_cross"),
    "stoch.overbought": ("stoch", "overbought"),
    "filters.ema_trend": ("filters", "ema_trend"),
    "filters.min_pct_of_52w_high": ("filters", "min_pct_of_52w_high"),
    "filters.max_atr_pct": ("filters", "max_atr_pct"),
    "filters.min_history_weeks": ("filters", "min_history_weeks"),
    "filters.min_traded_value_cr": ("filters", "min_traded_value_cr"),
    "filters.repeat_lookback_weeks": ("filters", "repeat_lookback_weeks"),
    "sizing.spacing_floor_pct": ("sizing", "spacing_floor_pct"),
    "sizing.spacing_atr_mult": ("sizing", "spacing_atr_mult"),
    "sizing.tranches_pct": ("sizing", "tranches_pct"),
    "sizing.stop_spacing_mult": ("sizing", "stop_spacing_mult"),
    "sizing.event_risk_alloc_mult": ("sizing", "event_risk_alloc_mult"),
    "adds.require_close_above_ema": ("adds", "add_ema"),
    "exits.trail_ema": ("exits", "trail_ema"),
    "exits.time_exit_weeks": ("exits", "time_exit_weeks"),
    "exits.trail_time_weeks": ("exits", "trail_time_weeks"),
    "reentry.cooling_off_weeks": ("reentry", "cooling_off_weeks"),
    "regime.ema": ("regime", "regime_ema"),
    "regime.slope_lookback_weeks": ("regime", "regime_slope_lookback_weeks"),
    "regime.red_max_entries": ("regime", "red_max_entries"),
    "regime.normal_max_entries": ("regime", "normal_max_entries"),
    "brakes.dd1_pct": ("brakes", "dd1_pct"),
    "brakes.dd1_pause_weeks": ("brakes", "dd1_pause_weeks"),
    "brakes.dd2_pct": ("brakes", "dd2_pct"),
    "brakes.brake_2_cleared_on": ("brakes", "brake_2_cleared_on"),
    "costs.cost_bps_buy": ("costs", "cost_bps_buy"),
    "costs.cost_bps_sell": ("costs", "cost_bps_sell"),
    "costs.fixed_cost_per_sell_rs": ("costs", "fixed_cost_per_sell_rs"),
}


class _Binder:
    """Consumes ``parameters`` key by key; anything left over is refused."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw
        self._sections: dict[str, dict[str, Any]] = {}

    def section(self, name: str) -> dict[str, Any]:
        if name not in self._sections:
            value = self._raw.pop(name, None)
            if not isinstance(value, dict):
                raise RunRefused(f"configuration: parameters.{name} must be a mapping")
            self._sections[name] = dict(value)
        return self._sections[name]

    def number(self, where: dict[str, Any], key: str, label: str) -> float:
        value = where.pop(key, None)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise RunRefused(f"configuration: parameters.{label}.{key} must be a number")
        return float(value)

    def fixed(self, where: dict[str, Any], key: str, expected: object, label: str) -> None:
        value = where.pop(key, None)
        if value != expected or type(value) is not type(expected):
            raise RunRefused(
                f"configuration: parameters.{label}.{key} is {value!r}; this spec version "
                f"implements only {expected!r} (not configurable)"
            )

    def done(self, where: dict[str, Any], label: str) -> None:
        if where:
            raise RunRefused(
                f"configuration: unknown key(s) in parameters.{label}: {sorted(where)}"
            )

    def params(self) -> RulesParameters:
        for name, values in _FIXED.items():
            section = self.section(name)
            for key, expected in values.items():
                self.fixed(section, key, expected, name)
        types = {f.name: f.type for f in fields(RulesParameters)}
        defaults = RulesParameters()
        chosen: dict[str, object] = {}
        for dotted, (section_name, attribute) in _RULES.items():
            key = dotted.rsplit(".", 1)[-1]
            where = self._raw if section_name is None else self.section(section_name)
            if key not in where:
                raise RunRefused(f"configuration: parameters.{dotted} is missing")
            chosen[attribute] = _coerce(
                where.pop(key), getattr(defaults, attribute), types[attribute], dotted
            )
        try:
            return RulesParameters(**chosen)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise RunRefused(f"configuration: {exc}") from exc

    def schedule(self) -> Schedule:
        raw = self.section("schedule")
        attempts = raw.pop("fetch_attempts", None)
        decide = raw.pop("decide", None)
        self.done(raw, "schedule")
        if not isinstance(attempts, list) or not attempts or not isinstance(decide, str):
            raise RunRefused(
                "configuration: parameters.schedule needs fetch_attempts (a list) and decide"
            )
        try:
            return Schedule(tuple(Slot.parse(str(a)) for a in attempts), Slot.parse(decide))
        except ValueError as exc:
            raise RunRefused(f"configuration: {exc}") from exc

    def finish(self) -> None:
        for name, section in self._sections.items():
            self.done(section, name)
        if self._raw:
            raise RunRefused(f"configuration: unknown parameters key(s): {sorted(self._raw)}")


def _coerce(value: object, default: object, annotation: object, dotted: str) -> object:
    """``value`` as the type of the ``RulesParameters`` field it replaces."""
    try:
        if isinstance(default, tuple):
            if not isinstance(value, list):
                raise TypeError("a list")
            return tuple(Decimal(str(v)) for v in value)
        if isinstance(default, Decimal):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError("a number")
            return Decimal(str(value))
        if isinstance(default, bool):
            raise TypeError("unexpected")
        if isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("a whole number")
            return value
        if isinstance(default, float):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError("a number")
            return float(value)
        if default is None and "date" in str(annotation):
            if value is None:
                return None
            if isinstance(value, date):
                return value
            return date.fromisoformat(str(value))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise RunRefused(f"configuration: parameters.{dotted} must be {exc}") from exc
    raise RunRefused(f"configuration: parameters.{dotted} has an unsupported type")
