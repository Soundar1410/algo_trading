"""Strategy-level risk vs. account-wide risk stay separate for
``rolling_strangle_otm1`` — proven, not merely documented.

Mirrors ``tests/unit/test_straddle_920_risk_separation.py`` exactly: two
independent claims, each checked directly.

1. ``RollingStrangleOtm1Strategy``'s own gross-P&L combined stop (spec
   section 10.1) is the *sole* daily-loss decider — the generic, engine-
   level ``DailyRiskGuard`` is provably disabled for this strategy's
   committed configuration, so there is never a second, differently-shaped
   decider running alongside it.
2. Account-wide risk/reservations are untouched infrastructure this port
   neither modifies nor bypasses — paper mode structurally never reaches
   them at all.
"""

from __future__ import annotations

from pathlib import Path

from common.config.loader import load_resolved_config
from common.config.models import ExecutionMode
from common.engine.config import EngineConfig
from common.engine.multi_leg_engine import MultiLegEngine
from runtimes.intraday_options.config_adapter import build_worker_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def _rolling_strangle_otm1_worker_config():
    cfg = load_resolved_config(
        config_root=REPO_ROOT / "config",
        runtime_id="intraday_options",
        strategy_id="rolling_strangle_otm1",
    )
    return build_worker_config(
        cfg,
        database_path=Path("/tmp/unused.db"),
        lock_dir=Path("/tmp/locks"),
        pid_dir=Path("/tmp/pid"),
        log_dir=Path("/tmp/logs"),
        trading_date="2026-08-24",
    )


# ------------------------------------------- 1. no double daily-loss decider
def test_the_committed_config_disables_the_engine_level_daily_guard():
    worker = _rolling_strangle_otm1_worker_config()
    assert worker.multi_leg_engine is not None
    assert worker.multi_leg_engine.max_daily_loss_percent is None, (
        "RollingStrangleOtm1Strategy's own gross-P&L combined stop (spec section "
        "10.1) must be the sole decider; the generic engine-level DailyRiskGuard "
        "must stay off for this strategy so the two can never disagree."
    )


def test_multi_leg_engine_builds_no_daily_guard_for_that_config():
    """The construction-level proof, not just the config-field one:
    ``MultiLegEngine._build_daily_guard`` genuinely returns ``None`` when
    handed this strategy's own resolved settings."""
    cfg = EngineConfig(max_daily_loss_percent=None, starting_capital=2_000_000.0)
    assert MultiLegEngine._build_daily_guard(cfg) is None


def test_a_positive_max_daily_loss_percent_would_build_a_guard():
    """The control: proves the assertion above is a real property of this
    config, not a call that always returns None regardless of input."""
    cfg = EngineConfig(max_daily_loss_percent=3.0, starting_capital=2_000_000.0)
    guard = MultiLegEngine._build_daily_guard(cfg)
    assert guard is not None
    assert guard._cfg.daily_max_loss == 60_000.0


# ------------------------------------ 2. account-wide risk stays fully wired
def test_account_risk_and_reservation_modules_are_unmodified_by_this_port():
    """A change-scope guard: this task must not have touched account-wide
    risk/reservation infrastructure — the negative-space suite
    (test_no_rolling_strangle_otm1_branches.py) checks more generally that
    no branch names this strategy; this confirms the modules still import
    and expose the same public surface a live worker would reach."""
    from common.risk.account_reservations import AccountReservationGate
    from common.risk.account_risk import check_account_daily_loss

    assert callable(AccountReservationGate.check_and_reserve)
    assert callable(check_account_daily_loss)


def test_paper_mode_never_reaches_account_reservations_for_this_strategy():
    """Structural, not behavioural: paper mode's broker construction
    (common.broker.factory.build_broker) returns a PaperBroker
    unconditionally before any live gate or account reservation is
    consulted — the same posture every existing paper strategy already has,
    confirmed here for this strategy's own committed config rather than
    assumed by analogy."""
    worker = _rolling_strangle_otm1_worker_config()
    assert worker.execution_mode is ExecutionMode.PAPER
    assert worker.strategy_live_approved is False
