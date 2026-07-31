"""Daily risk circuit-breaker, shared across a whole trading day.

Ported from the reference repository's ``framework/risk/daily_guard.py``
(Phase 3 Part 2b-i), unchanged in substance.

The per-position :class:`~common.engine.risk.RiskManager` answers "should I close
*this* trade now?" on every tick. This is the separate, strategy-wide, day-level
latch:

* **Daily maximum loss** — once P&L for the day falls to ``-daily_max_loss``,
  stop trading and square off.
* **Daily profit target** — optionally, once realised P&L reaches
  ``daily_profit_target``, bank the day and stop.
* **Maximum trades per day** — once ``max_trades`` round-trips are booked, take
  no further entries.
* **Emergency kill switch** — a config flag that halts all new entries
  immediately (and squares off) without waiting for any P&L threshold.

It is intentionally *not* forced into the ``RiskManager`` interface: that one is a
per-position, per-tick P&L exit decider, whereas this is a per-day,
realised-P&L-since-open latch.

Rupee thresholds here are absolute (already scaled for size by the caller):
realised P&L scales with quantity = ``lots_per_trade * lot_size``, so a strategy
that lets a user configure *per-lot* amounts must multiply them by
``lots_per_trade`` before constructing this guard.
"""

from __future__ import annotations

from dataclasses import dataclass

from common.logging import get_logger
from common.models import ExitReason

log = get_logger(__name__)


@dataclass(frozen=True)
class DailyRiskConfig:
    """Day-level limits. Rupee amounts are absolute (post per-lot scaling)."""

    #: > 0 => halt once realised P&L <= -this. 0 disables.
    daily_max_loss: float = 0.0
    #: None => no profit-based halt.
    daily_profit_target: float | None = None
    #: > 0 => halt new entries once this many round-trips are booked. 0 disables.
    max_trades: int = 0
    #: True => halted from the start (emergency stop).
    kill_switch: bool = False


@dataclass(frozen=True)
class DailyRiskState:
    """Current day-level risk picture for logging/inspection."""

    realised_pnl: float
    trade_count: int
    halted: bool
    halt_reason: str | None


class DailyRiskGuard:
    """Tracks realised P&L and trade count for the day; latches "halted".

    Fed a completed trade's **net** P&L via :meth:`register_trade` (called by the
    engine each time a position closes). Once any limit is crossed the guard
    latches ``halted=True`` and stays that way until :meth:`reset` at the start of
    the next day — the engine stops taking new entries and squares off anything
    still open, using :attr:`square_off_reason`.
    """

    def __init__(self, cfg: DailyRiskConfig) -> None:
        self._cfg = cfg
        self._realised = 0.0
        self._count = 0
        self._halt_reason: str | None = None
        self._square_off_reason = ExitReason.STRATEGY_EXIT
        self.reset()

    def reset(self) -> None:
        """Clear all day state (called at the start of each trading day)."""
        self._realised = 0.0
        self._count = 0
        # Emergency kill switch halts immediately, before any trade.
        self._halt_reason = "kill switch" if self._cfg.kill_switch else None
        self._square_off_reason = (
            ExitReason.MANUAL if self._cfg.kill_switch else ExitReason.STRATEGY_EXIT
        )

    @property
    def halted(self) -> bool:
        return self._halt_reason is not None

    @property
    def halt_reason(self) -> str | None:
        return self._halt_reason

    @property
    def square_off_reason(self) -> ExitReason:
        """Which exit reason to force-close open positions with when halting."""
        return self._square_off_reason

    def register_trade(self, net_pnl: float) -> str | None:
        """Book a completed round-trip's net P&L.

        Returns a halt reason if this trade tripped a day-level limit (and latches
        ``halted``), else ``None``. A no-op once already halted (the day is over).
        """
        if self.halted:
            return None
        self._realised += float(net_pnl)
        self._count += 1
        return self._evaluate()

    def check_open_mtm(self, open_pnl: float) -> str | None:
        """Live mark-to-market loss check for an *open* position.

        Evaluates realised-so-far **plus** the open position's unrealised P&L
        against ``daily_max_loss`` on every tick, so the day's loss cap can trip
        mid-trade (and latch ``halted``) rather than only when a trade closes.
        Does not book anything or touch the trade count. A no-op once already
        halted or when the loss cap is disabled.
        """
        if self.halted or self._cfg.daily_max_loss <= 0:
            return None
        total = self._realised + float(open_pnl)
        if total <= -self._cfg.daily_max_loss:
            self._halt(
                f"daily max loss hit on live MTM (realised ₹{self._realised:.0f} "
                f"+ open ₹{open_pnl:.0f} = ₹{total:.0f} "
                f"<= -₹{self._cfg.daily_max_loss:.0f})",
                ExitReason.DAILY_LOSS_LIMIT,
            )
        return self._halt_reason

    def _evaluate(self) -> str | None:
        cfg = self._cfg
        if cfg.daily_max_loss > 0 and self._realised <= -cfg.daily_max_loss:
            self._halt(
                f"daily max loss hit (realised ₹{self._realised:.0f} "
                f"<= -₹{cfg.daily_max_loss:.0f})",
                ExitReason.DAILY_LOSS_LIMIT,
            )
        elif cfg.daily_profit_target is not None and self._realised >= cfg.daily_profit_target:
            self._halt(
                f"daily profit target hit (realised ₹{self._realised:.0f} "
                f">= ₹{cfg.daily_profit_target:.0f})",
                ExitReason.TARGET_PROFIT,
            )
        elif cfg.max_trades > 0 and self._count >= cfg.max_trades:
            self._halt(
                f"max trades per day reached ({self._count}/{cfg.max_trades})",
                ExitReason.STRATEGY_EXIT,
            )
        return self._halt_reason

    def _halt(self, reason: str, square_off_reason: ExitReason) -> None:
        self._halt_reason = reason
        self._square_off_reason = square_off_reason
        log.warning("daily risk guard tripped: %s. No further entries today.", reason)

    @property
    def state(self) -> DailyRiskState:
        return DailyRiskState(
            realised_pnl=self._realised,
            trade_count=self._count,
            halted=self.halted,
            halt_reason=self._halt_reason,
        )
