"""The weekly run's configuration, in code until Phase 5 (spec 12, 13).

Binding these values to ``config/runtimes/positional_stocks.yaml`` and
``config/strategies/positional_stocks/wsr1_weekly_stochrsi.yaml`` is Phase 5,
and those two files must land in one commit: ``resolve_runtime_strategies``
scans ``config/strategies/**`` and raises for a strategy whose runtime file is
missing, which would stop both paper runtimes. Until then nothing is read from
YAML, and nothing is added under ``config/strategies/`` or ``config/runtimes/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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


class RunRefused(RuntimeError):
    """The run must not start: not paper, or a safety check failed."""


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

    def check_paper(self) -> None:
        if self.mode != "paper":
            raise RunRefused(
                f"mode {self.mode!r} refused: this spec version runs {self.strategy_id} "
                "in paper mode only (spec 10.2 step 1)"
            )
