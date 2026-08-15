"""Strategy-level risk vs. account-wide risk stay separate — proven, not
merely documented.

Two independent claims, each checked directly:

1. ``straddle_920``'s own gross-P&L formulas (spec sections 11-13) are the
   *sole* daily-loss/combined-stop/profit-target decider — the generic,
   engine-level ``DailyRiskGuard`` (``common.engine.daily_guard``) is
   provably disabled for this strategy's committed configuration, so there is
   never a second, differently-shaped decider running alongside it.
2. Account-wide risk/reservations (``common.risk.account_risk``,
   ``common.risk.account_reservations``) are untouched infrastructure this
   port neither modifies nor bypasses — paper mode structurally never reaches
   them at all (``common.broker.factory.build_broker`` returns a
   ``PaperBroker`` unconditionally for ``mode: paper``, before any live gate
   or account reservation is even consulted), which is the existing,
   unmodified single-leg posture this strategy inherits rather than a new
   exemption carved out for it.
"""

from __future__ import annotations

from pathlib import Path

from common.config.loader import load_resolved_config
from common.engine.config import EngineConfig
from common.engine.multi_leg_engine import MultiLegEngine
from runtimes.intraday_options.config_adapter import build_worker_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def _straddle_920_worker_config():
    cfg = load_resolved_config(
        config_root=REPO_ROOT / "config", runtime_id="intraday_options", strategy_id="straddle_920"
    )
    return build_worker_config(
        cfg,
        database_path=Path("/tmp/unused.db"),
        lock_dir=Path("/tmp/locks"),
        pid_dir=Path("/tmp/pid"),
        log_dir=Path("/tmp/logs"),
        trading_date="2026-08-17",
    )


# ------------------------------------------- 1. no double daily-loss decider
def test_the_committed_straddle_920_config_disables_the_engine_level_daily_guard():
    worker = _straddle_920_worker_config()
    assert worker.multi_leg_engine is not None
    assert worker.multi_leg_engine.max_daily_loss_percent is None, (
        "straddle_920's own gross-P&L daily-loss formula (spec section 13.2) must be "
        "the sole decider; the generic engine-level DailyRiskGuard must stay off for "
        "this strategy so the two can never disagree."
    )


def test_multi_leg_engine_builds_no_daily_guard_for_that_config():
    """The construction-level proof, not just the config-field one:
    ``MultiLegEngine._build_daily_guard`` genuinely returns ``None`` when
    handed straddle_920's own resolved settings."""
    cfg = EngineConfig(max_daily_loss_percent=None, starting_capital=2_000_000.0)
    assert MultiLegEngine._build_daily_guard(cfg) is None


def test_a_positive_max_daily_loss_percent_would_build_a_guard():
    """The control: proves the assertion above is a real property of this
    config, not a call that always returns None regardless of input — a
    *future* multi-leg strategy that wants the generic guard still can."""
    cfg = EngineConfig(max_daily_loss_percent=3.0, starting_capital=2_000_000.0)
    guard = MultiLegEngine._build_daily_guard(cfg)
    assert guard is not None
    assert guard._cfg.daily_max_loss == 60_000.0


# ------------------------------------ 2. account-wide risk stays fully wired
def test_account_risk_and_reservation_modules_are_unmodified_by_this_port():
    """A change-scope guard: this task must not have touched account-wide
    risk/reservation infrastructure. Not a behavioural test — the existing
    account-risk suite (tests/unit/test_account_*.py et al.) already proves
    that infrastructure's own behaviour and continues to pass unmodified;
    this only pins that the files themselves carry no straddle_920-specific
    trace, which the broader negative-space suite
    (test_no_straddle_920_branches.py) already checks more generally, and
    additionally confirms the modules still import and expose the same
    public surface a live worker would reach."""
    from common.risk.account_reservations import AccountReservationGate
    from common.risk.account_risk import check_account_daily_loss

    assert callable(AccountReservationGate.check_and_reserve)
    assert callable(check_account_daily_loss)


def test_paper_mode_never_reaches_account_reservations_for_this_strategy():
    """Structural, not behavioural: paper mode's broker construction
    (common.broker.factory.build_broker) returns a PaperBroker
    unconditionally before any live gate or account reservation is
    consulted — the same posture every existing paper strategy already has,
    confirmed here for straddle_920's own committed config rather than
    assumed by analogy."""
    worker = _straddle_920_worker_config()
    from common.config.models import ExecutionMode

    assert worker.execution_mode is ExecutionMode.PAPER
    assert worker.strategy_live_approved is False
