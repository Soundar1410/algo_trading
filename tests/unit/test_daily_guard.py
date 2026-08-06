"""``DailyRiskGuard.restore`` — Phase 6 Part 1.

No test file existed for this class before this one; the properties added here
are what a restarting worker actually depends on, so they are pinned precisely
rather than through the full worker (which cannot control a fill's exact realised
P&L against broker slippage and costs).

The property under test throughout: ``restore`` must put the guard in **exactly
the state it would be in had the process never restarted** — same halt decision,
same remaining headroom, same trade count — never a fresh full cap and never a
silent reset of what a kill switch already decided.
"""

from __future__ import annotations

from common.engine.daily_guard import DailyRiskConfig, DailyRiskGuard, DailyRiskRecovery
from common.models import ExitReason


def _guard(**cfg: object) -> DailyRiskGuard:
    return DailyRiskGuard(DailyRiskConfig(**cfg))  # type: ignore[arg-type]


def test_restore_seeds_realised_pnl_and_trade_count() -> None:
    guard = _guard(daily_max_loss=1000.0)
    result = guard.restore(DailyRiskRecovery(realised_pnl=-200.0, trade_count=2))

    assert result is None
    assert guard.halted is False
    assert guard.state.realised_pnl == -200.0
    assert guard.state.trade_count == 2


def test_restore_latches_halted_when_the_recovered_pnl_is_already_past_the_cap() -> None:
    """The gap this whole mechanism exists to close.

    Without ``restore``, a worker that restarts after a loss the cap should have
    stopped begins the day at zero — this is what proves it no longer does.
    """
    guard = _guard(daily_max_loss=1000.0)
    result = guard.restore(DailyRiskRecovery(realised_pnl=-1500.0, trade_count=1))

    assert result is not None
    assert guard.halted is True
    assert guard.halt_reason == result
    assert guard.square_off_reason is ExitReason.DAILY_LOSS_LIMIT


def test_restore_trips_at_the_remaining_headroom_not_a_fresh_cap() -> None:
    """A restart must not hand back a full cap's worth of room to lose again."""
    guard = _guard(daily_max_loss=1000.0)
    guard.restore(DailyRiskRecovery(realised_pnl=-600.0, trade_count=1))
    assert guard.halted is False, "a partial loss must not halt by itself"

    # A fresh full cap would tolerate another -1000 from here. Only -400 of
    # headroom actually remains (-600 restored, -1000 cap) -- so -500 more must
    # already trip it.
    halt_reason = guard.register_trade(-500.0)

    assert halt_reason is not None
    assert guard.halted is True
    assert guard.state.realised_pnl == -1100.0


def test_restore_continues_the_trade_count_rather_than_restarting_it() -> None:
    guard = _guard(max_trades=3)
    guard.restore(DailyRiskRecovery(realised_pnl=0.0, trade_count=2))
    assert guard.halted is False

    halt_reason = guard.register_trade(0.0)

    assert halt_reason is not None
    assert guard.state.trade_count == 3
    assert "3/3" in halt_reason


def test_restore_is_a_noop_once_the_kill_switch_has_already_latched() -> None:
    """A recovered count must never overwrite what the kill switch already decided."""
    guard = _guard(kill_switch=True)
    assert guard.halted is True  # latched by reset() in __init__, before restore

    result = guard.restore(DailyRiskRecovery(realised_pnl=-50.0, trade_count=1))

    assert result is None
    # Untouched -- still the reset() state, not the recovered one.
    assert guard.state.realised_pnl == 0.0
    assert guard.state.trade_count == 0


def test_a_fresh_day_reset_after_restore_returns_to_zero() -> None:
    """The no-leak rule: restore must not survive a subsequent reset()."""
    guard = _guard(daily_max_loss=1000.0)
    guard.restore(DailyRiskRecovery(realised_pnl=-1500.0, trade_count=3))
    assert guard.halted is True

    guard.reset()

    assert guard.halted is False
    assert guard.state.realised_pnl == 0.0
    assert guard.state.trade_count == 0


def test_restore_of_a_headroom_still_available_lets_a_further_loss_register_normally() -> None:
    """The mirror of the headroom test: restoring well inside the cap changes nothing
    about ordinary trading afterwards."""
    guard = _guard(daily_max_loss=1000.0)
    guard.restore(DailyRiskRecovery(realised_pnl=-100.0, trade_count=1))

    assert guard.register_trade(50.0) is None
    assert guard.halted is False
    assert guard.state.realised_pnl == -50.0
    assert guard.state.trade_count == 2
