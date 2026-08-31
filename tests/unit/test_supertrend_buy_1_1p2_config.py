"""The committed ``config/strategies/supertrend_buy_1_1p2.yaml``, read through the
real loader and the real intraday config adapter.

Nothing here builds a config by hand: every assertion goes through
``load_resolved_config`` / ``discover_strategies`` / ``build_worker_config`` against
the file that is actually committed, because a hand-built config would pass whatever
the committed one said.

Spec sections 17 (configuration requirements) and 18.7 (architecture regression).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from common.config.loader import (
    discover_enabled_strategies,
    discover_strategies,
    load_auto_start_config,
    load_resolved_config,
)
from common.config.models import EngineKind, ExecutionMode, effective_live_gate
from common.engine.config import EngineConfig, SessionConfig
from common.engine.engine import TradingEngine
from common.engine.session import MarketSession
from common.engine.strategy import BaseStrategy
from common.warmup.session_buckets import session_bucket_count
from runtimes.intraday_options.config_adapter import build_worker_config
from runtimes.intraday_options.engine_worker import load_strategy
from runtimes.intraday_options.worker import EngineWorkerConfig
from strategies.intraday_options.supertrend_buy_1_1p2.strategy import (
    DEFAULT_WARMUP_MIN_BARS,
    SupertrendBuy1x1p2Strategy,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPO_ROOT / "config"
STRATEGY_ID = "supertrend_buy_1_1p2"
RUNTIME_ID = "intraday_options"
CONFIG_FILE = CONFIG_ROOT / "strategies" / RUNTIME_ID / f"{STRATEGY_ID}.yaml"


@pytest.fixture
def resolved():
    return load_resolved_config(CONFIG_ROOT, RUNTIME_ID, STRATEGY_ID)


@pytest.fixture
def worker(resolved, tmp_path: Path):
    return build_worker_config(
        resolved,
        database_path=tmp_path / "db.sqlite",
        lock_dir=tmp_path / "locks",
        pid_dir=tmp_path / "pid",
        log_dir=tmp_path / "logs",
        trading_date="2026-08-20",
    )


@pytest.fixture
def engine_config(worker) -> EngineWorkerConfig:
    assert worker.engine is not None
    return worker.engine


# ------------------------------------------------------------- 17. fail-closed
def test_the_committed_config_is_enabled_paper_and_not_live_approved(resolved):
    """Spec section 17's minimum. Shipped disabled at delivery; the operator
    enabled it for real, paper-only trading on 31 August 2026 (see
    docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md). The two flags that still
    keep live execution unreachable, regardless."""
    assert resolved.strategy.strategy_id == STRATEGY_ID
    assert resolved.strategy.runtime_id == RUNTIME_ID
    assert resolved.strategy.enabled is True
    assert resolved.strategy.mode is ExecutionMode.PAPER
    assert resolved.strategy.live_approved is False


def test_the_enabled_strategy_is_discovered_and_enabled(resolved):
    """Spec 18.7's discovery half, updated for the 31 August 2026 enable
    decision: ``discover_strategies`` is what the dashboard and the
    supervisor's mode-transition exposure check read; ``discover_enabled_
    strategies`` is what decides that a worker is started at all. Both now
    include this strategy."""
    discovered = [c.strategy.strategy_id for c in discover_strategies(CONFIG_ROOT, RUNTIME_ID)]
    enabled = [
        c.strategy.strategy_id for c in discover_enabled_strategies(CONFIG_ROOT, RUNTIME_ID)
    ]
    assert STRATEGY_ID in discovered
    assert STRATEGY_ID in enabled


def test_enabling_this_strategy_did_not_change_other_strategies_flags():
    """No other strategy's committed flags moved as a side effect of this
    one's enable decision. Deliberately membership checks, not an exhaustive
    equality against the full enabled set — that set is this file's business
    only for STRATEGY_ID, and hardcoding every other strategy's name here is
    exactly the coupling that made this test brittle the last time the
    committed set grew."""
    enabled = {c.strategy.strategy_id for c in discover_enabled_strategies(CONFIG_ROOT, RUNTIME_ID)}
    assert STRATEGY_ID in enabled
    assert {"c921_ema_cross_buy", "straddle_920"} <= enabled
    for other in ("c921_ema_cross_buy", "straddle_920"):
        cfg = load_resolved_config(CONFIG_ROOT, RUNTIME_ID, other)
        assert cfg.strategy.enabled is True
        assert cfg.strategy.mode is ExecutionMode.PAPER
        assert cfg.strategy.live_approved is False


def test_paper_mode_is_refused_by_the_live_gate(resolved):
    """Even with every preflight granted, a paper strategy is never live-eligible."""
    decision = effective_live_gate(resolved, preflight_passed=True)
    assert decision.allowed is False
    assert decision.blocked_reasons


def test_the_committed_file_names_no_live_enabling_value():
    text = CONFIG_FILE.read_text(encoding="utf-8")
    assert "mode: paper" in text
    assert "live_approved: false" in text
    assert "mode: live" not in text


# ------------------------------------------------------- 17. engine routing
def test_it_routes_to_the_single_leg_trading_engine_generically(resolved, worker):
    """``EngineKind.TRADING_ENGINE`` + ``parameters.strategy_ref`` — the existing
    generic discriminator. No adapter branch names this strategy."""
    assert resolved.strategy.engine is EngineKind.TRADING_ENGINE
    assert worker.engine is not None
    assert isinstance(worker.engine, EngineWorkerConfig)
    assert worker.multi_leg_engine is None


def test_the_worker_requires_a_tick_channel(worker):
    """What the supervisor's registration keys on. Proven end to end against the real
    composition root in
    ``tests/integration/test_supertrend_buy_1_1p2_supervisor_composition.py``."""
    assert worker.requires_tick_channel is True


def test_the_strategy_ref_loads_the_real_class_with_the_configured_kwargs(engine_config):
    """The dotted reference and ``strategy_kwargs`` are the whole integration seam:
    ``load_strategy`` is what the spawned worker actually calls."""
    strategy = load_strategy(engine_config.strategy_ref, engine_config.strategy_kwargs)
    assert isinstance(strategy, SupertrendBuy1x1p2Strategy)
    assert isinstance(strategy, BaseStrategy)
    assert strategy.name == STRATEGY_ID
    assert strategy._supertrend.period == 1
    assert strategy._supertrend.multiplier == 1.2
    assert strategy.quantity_lots == 10
    assert strategy._exit._trail.trail_percentage == 8.0
    assert strategy._exit._trail.min_favourable_move_percentage == 4.0
    spec = strategy.warmup_spec()
    assert spec is not None
    assert spec.min_bars == DEFAULT_WARMUP_MIN_BARS == 75
    assert spec.continuity_required is True


def test_the_configured_underlying_is_the_nifty_index(worker):
    assert worker.instrument == "NIFTY"
    assert worker.security_id == "13"


# ------------------------------------------------------ 17. sizing and contract
def test_order_sizing_is_ten_lots_and_names_no_lot_size(engine_config):
    """Spec section 8: ten lots is the only sizing number configured. The exchange
    lot size is resolved at runtime and multiplied in by the engine."""
    assert engine_config.lots == 10
    assert engine_config.strategy_kwargs["lots_per_trade"] == 10
    parsed = yaml.safe_load(CONFIG_FILE.read_text())
    assert "lot_size" not in parsed["parameters"]
    assert "lot_size" not in parsed["parameters"]["strategy_kwargs"]
    # ...and not as a real setting anywhere in the file either (prose explaining
    # *why* it is absent is fine; a non-comment line naming it is not).
    code_lines = [
        line for line in CONFIG_FILE.read_text().splitlines() if not line.strip().startswith("#")
    ]
    assert not [line for line in code_lines if "lot_size" in line]


def test_contracts_are_resolved_from_the_real_scrip_master(engine_config):
    """Spec section 8: strike, weekly expiry AND lot size come from the exchange's own
    daily instrument master. A null expiry means "the resolver's nearest listed one",
    which already carries any holiday shift."""
    assert engine_config.contract_resolver == "dhan"
    assert engine_config.strike_step == 50
    assert engine_config.expiry is None


def test_the_single_leg_adapter_would_silently_default_a_simulated_lot_size(
    resolved, tmp_path: Path
):
    """A known asymmetry in existing shared code, pinned rather than assumed away.

    ``_build_multi_leg_engine_worker_config`` raises ``ConfigError`` when
    ``contract_resolver: simulated`` carries no explicit ``lot_size``.
    ``_build_engine_worker_config`` — the single-leg branch this strategy uses —
    does **not**: it defaults to 50. So omitting ``lot_size`` (which this config does,
    correctly, because the "dhan" resolver ignores it) is *not* on its own what would
    make an accidental switch to the simulated resolver fail loudly on this path.

    Closing that gap would mean changing shared code that every existing simulated
    fixture and several committed tests rely on, which is out of scope for a strategy
    port. What guards this strategy instead is
    ``test_contracts_are_resolved_from_the_real_scrip_master`` pinning
    ``contract_resolver: dhan``. This test exists so the weaker guarantee is on the
    record and cannot be silently mistaken for the stronger one.
    """
    parameters = dict(resolved.strategy.parameters)
    parameters["contract_resolver"] = "simulated"
    mutated = resolved.model_copy(
        update={"strategy": resolved.strategy.model_copy(update={"parameters": parameters})}
    )
    worker = build_worker_config(
        mutated,
        database_path=tmp_path / "db.sqlite",
        lock_dir=tmp_path / "locks",
        pid_dir=tmp_path / "pid",
        log_dir=tmp_path / "logs",
        trading_date="2026-08-20",
    )
    assert worker.engine is not None
    assert worker.engine.lot_size == 50  # the silent default, not an error


# ------------------------------------------------------------- 5. session times
def test_the_session_window_is_0915_to_1515_with_a_1520_square_off(worker, engine_config):
    """Spec section 5. One configured pair drives both derived strings."""
    assert engine_config.session_start_time == "09:15"
    assert worker.square_off_policy.entry_cutoff.strftime("%H:%M") == "15:15"
    assert worker.square_off_policy.square_off_at.strftime("%H:%M") == "15:20"

    session = SessionConfig.from_square_off_policy(
        worker.square_off_policy,
        start_time=engine_config.session_start_time,
        holidays=tuple(engine_config.holidays),
    )
    assert (session.start_time, session.end_time, session.square_off_time) == (
        "09:15",
        "15:15",
        "15:20",
    )


def test_entries_stop_at_exactly_1515_while_exits_do_not(worker, engine_config):
    """Spec 18.1: "Signal exactly at/after 15:15 | No new entry" — a strict ``<``."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    session = MarketSession(
        SessionConfig.from_square_off_policy(
            worker.square_off_policy,
            start_time=engine_config.session_start_time,
            holidays=tuple(engine_config.holidays),
        )
    )
    day = datetime(2026, 8, 20, tzinfo=ist)  # Thursday, a trading day
    assert session.can_enter(day.replace(hour=9, minute=15)) is True
    assert session.can_enter(day.replace(hour=9, minute=14, second=59)) is False
    assert session.can_enter(day.replace(hour=15, minute=14, second=59)) is True
    assert session.can_enter(day.replace(hour=15, minute=15)) is False
    # ...but the session itself stays open through the hard square-off, so an exit
    # and the 15:20 force-close are still reachable.
    assert session.is_open(day.replace(hour=15, minute=15)) is True
    assert session.is_open(day.replace(hour=15, minute=20)) is True


# --------------------------------------------------------------- 12. daily risk
def test_the_daily_cap_resolves_to_thirty_thousand_rupees(engine_config):
    """Spec section 12: 3% of a Rs 10,00,000 reference capital."""
    assert engine_config.starting_capital == 1_000_000.0
    assert engine_config.max_daily_loss_percent == 3.0
    guard = TradingEngine._build_daily_guard(
        EngineConfig(
            starting_capital=engine_config.starting_capital,
            max_daily_loss_percent=engine_config.max_daily_loss_percent,
        )
    )
    assert guard is not None
    assert guard._cfg.daily_max_loss == 30_000.0
    # Inclusive: exactly -30,000 trips it.
    assert guard.check_open_mtm(-29_999.99) is None
    assert guard.check_open_mtm(-30_000.0) is not None
    assert guard.halted is True


# ------------------------------------------------------------------- 7. warm-up
def test_warmup_is_configured_for_a_continuity_required_indicator(engine_config):
    """Both lines are mandatory: ``TradingEngine.__init__`` raises
    ``InvalidWarmupConfig`` without them, because SuperTrend cannot be cold-started."""
    assert engine_config.warmup_from_history is True
    assert engine_config.warmup_source == "dhan"


def test_the_lookback_budget_covers_the_seventy_five_bucket_floor(engine_config):
    """A 09:15-15:20 lifecycle contributes 73 completed 5-minute buckets, so the
    75-bucket trust floor needs ceil(75/73) = 2 prior sessions plus today. The
    committed budget must be at least that."""
    session = MarketSession(
        SessionConfig(
            timezone="Asia/Kolkata",
            start_time="09:15",
            end_time="15:15",
            square_off_time="15:20",
        )
    )
    per_session = session_bucket_count(session, 5)
    assert per_session == 73
    needed = -(-DEFAULT_WARMUP_MIN_BARS // per_session)  # ceil
    assert needed == 2
    assert engine_config.warmup_max_lookback_sessions >= needed
    assert engine_config.warmup_max_lookback_sessions == 3


def test_the_configured_min_bars_is_the_approved_seventy_five(engine_config):
    assert engine_config.strategy_kwargs["warmup_min_bars"] == 75


# --------------------------------------------------------- trading calendar
def test_the_engine_session_gets_the_verified_nse_holiday_calendar(engine_config):
    """The engine's own ``MarketSession`` reads holidays from the strategy config, not
    from ``config/global.yaml`` (whose list feeds only the unattended auto-start gate).
    They must agree, or the two would disagree about what a trading day is — which
    also breaks the warm-up walk-back, since the 75-bucket floor spans sessions."""
    global_holidays = tuple(load_auto_start_config(CONFIG_ROOT).holidays)
    assert engine_config.holidays == global_holidays
    assert len(engine_config.holidays) == 20


def test_a_holiday_and_a_weekend_are_both_closed_to_this_strategy(worker, engine_config):
    """Spec 18.1: "Holiday/weekend | No entry/runtime trading action"."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    session = MarketSession(
        SessionConfig.from_square_off_policy(
            worker.square_off_policy,
            start_time=engine_config.session_start_time,
            holidays=tuple(engine_config.holidays),
        )
    )
    midday = {"hour": 11, "minute": 0, "tzinfo": ist}
    assert session.can_enter(datetime(2026, 10, 2, **midday)) is False  # Gandhi Jayanti
    assert session.can_enter(datetime(2026, 8, 22, **midday)) is False  # Saturday
    assert session.can_enter(datetime(2026, 8, 23, **midday)) is False  # Sunday
    assert session.can_enter(datetime(2026, 8, 20, **midday)) is True  # ordinary Thursday


# -------------------------------------------------------- 13. paper execution
def test_paper_execution_uses_the_current_model_not_the_legacy_zero_slippage(worker):
    """Spec section 13, the one intentional architecture-level deviation from the
    legacy simulator: legacy ran zero slippage and no latency. This is the same
    canonical intraday paper block ``c921_ema_cross_buy`` uses, so the two single-leg
    intraday strategies are filled by the same model."""
    paper = worker.paper_execution
    assert paper["slippage"] == {"options": {"mode": "ticks", "market_order_ticks": 1}}
    assert paper["submission_latency_ms"] == 250
    assert paper["tick_size"] == 0.05
    assert paper["allow_ltp_fallback"] is True
    assert paper["ltp_fallback_extra_ticks"] == 1
    assert paper["max_quote_age_ms"] == 2000

    ema = build_worker_config(
        load_resolved_config(CONFIG_ROOT, RUNTIME_ID, "c921_ema_cross_buy"),
        database_path=Path("/tmp/unused.db"),
        lock_dir=Path("/tmp"),
        pid_dir=Path("/tmp"),
        log_dir=Path("/tmp"),
        trading_date="2026-08-20",
    )
    assert paper == ema.paper_execution


def test_charges_are_the_repository_defaults_not_a_strategy_override(worker):
    """No strategy YAML sets ``cost_rates``; this one does not either, so realised
    P&L is booked against exactly the same rates the other strategies use."""
    assert worker.cost_rates == {}
