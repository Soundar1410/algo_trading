"""``parameters.strategy_ref`` switches config_adapter.py onto the ported-engine
path (Phase 9) — the branch ``test_config_adapter.py`` recorded as not existing
yet. See ``runtimes/intraday_options/config_adapter.py``'s module docstring.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest

from common.config import ConfigError, ExecutionMode, ResolvedConfig
from common.config.models import GlobalConfig, RuntimeConfig, StrategyConfig
from runtimes.intraday_options.config_adapter import build_worker_config

_STRATEGY_REF = (
    "strategies.intraday_options.ema_cross_9_21_buy.strategy:EmaCross9x21BuyStrategy"
)


def _cfg(**strategy_overrides) -> ResolvedConfig:
    parameters = {
        "instrument": "NIFTY",
        "security_id": "13",
        "strategy_ref": _STRATEGY_REF,
        "strategy_kwargs": {"lots_per_trade": 10},
        "capital_base": 1_000_000,
        "daily_max_loss_pct": 3.0,
        "contract_resolver": "dhan",
    }
    parameters.update(strategy_overrides.pop("parameters", {}))
    strategy_kwargs = {
        "strategy_id": "ema_cross_9_21_buy",
        "runtime_id": "intraday_options",
        "enabled": True,
        "parameters": parameters,
        "risk": {"entry_start": "09:15", "entry_cutoff": "14:45", "square_off_at": "15:15"},
    }
    strategy_kwargs.update(strategy_overrides)
    return ResolvedConfig(
        global_config=GlobalConfig(),
        runtime=RuntimeConfig(runtime_id="intraday_options", enabled=True),
        strategy=StrategyConfig(**strategy_kwargs),
    )


def _build(cfg: ResolvedConfig, tmp_path: Path):
    return build_worker_config(
        cfg,
        database_path=tmp_path / "db.sqlite",
        lock_dir=tmp_path / "locks",
        pid_dir=tmp_path / "pid",
        log_dir=tmp_path / "logs",
        trading_date="2026-08-09",
    )


def test_strategy_ref_switches_on_the_engine_branch(tmp_path: Path):
    worker = _build(_cfg(), tmp_path)
    assert worker.engine is not None
    assert worker.engine.strategy_ref == _STRATEGY_REF


def test_strategy_kwargs_travel_through_verbatim(tmp_path: Path):
    worker = _build(_cfg(), tmp_path)
    assert worker.engine is not None
    assert worker.engine.strategy_kwargs == {"lots_per_trade": 10}


def test_lots_come_from_strategy_kwargs_not_a_second_top_level_setting(tmp_path: Path):
    """lots_per_trade has exactly one configured home (strategy_kwargs) — it
    also drives EngineWorkerConfig.lots, which is what PositionManager (and
    therefore OpenPosition.quantity = lots * contract.lot_size) actually
    sizes every order from."""
    cfg = _cfg(
        parameters={"strategy_kwargs": {"lots_per_trade": 25}, "strike_step": 100}
    )
    worker = _build(cfg, tmp_path)
    assert worker.engine is not None
    assert worker.engine.lots == 25
    assert worker.engine.strike_step == 100


def test_lots_fall_back_to_a_top_level_parameter_when_absent_from_strategy_kwargs(
    tmp_path: Path,
):
    cfg = _cfg(parameters={"strategy_kwargs": {}, "lots_per_trade": 7})
    worker = _build(cfg, tmp_path)
    assert worker.engine is not None
    assert worker.engine.lots == 7


def test_capital_base_and_daily_loss_pct_map_to_engine_worker_config(tmp_path: Path):
    """The %->rupee conversion itself is NOT this adapter's job — it stays in
    TradingEngine._build_daily_guard. This only proves the two numbers reach
    EngineWorkerConfig unmultiplied."""
    worker = _build(_cfg(), tmp_path)
    assert worker.engine is not None
    assert worker.engine.starting_capital == 1_000_000.0
    assert worker.engine.max_daily_loss_percent == 3.0


def test_no_daily_loss_pct_configured_disables_the_guard(tmp_path: Path):
    cfg = _cfg(parameters={"daily_max_loss_pct": None})
    worker = _build(cfg, tmp_path)
    assert worker.engine is not None
    assert worker.engine.max_daily_loss_percent is None


def test_contract_resolver_and_lot_size_fallback_are_mapped(tmp_path: Path):
    cfg = _cfg(parameters={"contract_resolver": "simulated", "lot_size": 75})
    worker = _build(cfg, tmp_path)
    assert worker.engine is not None
    assert worker.engine.contract_resolver == "simulated"
    assert worker.engine.lot_size == 75


def test_entry_start_from_risk_becomes_session_start_time(tmp_path: Path):
    cfg = _cfg(risk={"entry_start": "09:20", "entry_cutoff": "14:45", "square_off_at": "15:15"})
    worker = _build(cfg, tmp_path)
    assert worker.engine is not None
    assert worker.engine.session_start_time == "09:20"


def test_entry_start_defaults_to_09_15_when_absent(tmp_path: Path):
    cfg = _cfg(risk={"entry_cutoff": "14:45", "square_off_at": "15:15"})
    worker = _build(cfg, tmp_path)
    assert worker.engine is not None
    assert worker.engine.session_start_time == "09:15"


def test_entry_cutoff_and_square_off_still_build_the_square_off_policy(tmp_path: Path):
    """The engine path reuses the same SquareOffPolicy plumbing as the fixture
    path — one configured pair of times, not a second one for the engine."""
    worker = _build(_cfg(), tmp_path)
    assert worker.square_off_policy.entry_cutoff == time(14, 45)
    assert worker.square_off_policy.square_off_at == time(15, 15)


def test_warmup_and_regime_flags_are_mapped(tmp_path: Path):
    cfg = _cfg(
        parameters={
            "warmup_from_history": False,
            "warmup_source": "dhan",
            "warmup_max_lookback_sessions": 5,
            "regime_enabled": True,
        }
    )
    worker = _build(cfg, tmp_path)
    assert worker.engine is not None
    assert worker.engine.warmup_from_history is False
    assert worker.engine.warmup_source == "dhan"
    assert worker.engine.warmup_max_lookback_sessions == 5
    assert worker.engine.regime_enabled is True


def test_blank_strategy_ref_raises_config_error(tmp_path: Path):
    cfg = _cfg(parameters={"strategy_ref": ""})
    with pytest.raises(ConfigError, match="strategy_ref"):
        _build(cfg, tmp_path)


def test_malformed_strategy_ref_raises_config_error(tmp_path: Path):
    cfg = _cfg(parameters={"strategy_ref": "not_a_dotted_reference"})
    with pytest.raises(ConfigError, match="strategy_ref"):
        _build(cfg, tmp_path)


def test_execution_mode_paper_is_carried_through_for_the_engine_path(tmp_path: Path):
    worker = _build(_cfg(mode=ExecutionMode.PAPER, live_approved=False), tmp_path)
    assert worker.execution_mode is ExecutionMode.PAPER


# --------------------------------------------- EngineKind routing (straddle_920)
def test_the_trading_engine_kind_is_the_default_and_unaffected_by_the_new_branch(
    tmp_path: Path,
):
    """``EngineKind`` defaults to ``TRADING_ENGINE`` on every existing strategy
    config, ``ema_cross_9_21_buy.yaml`` included — the additive multi-leg
    routing must not change one byte of what this produces."""
    from common.config import EngineKind

    worker = _build(_cfg(), tmp_path)
    assert worker.engine is not None
    assert worker.multi_leg_engine is None

    explicit = _cfg(engine=EngineKind.TRADING_ENGINE)
    worker_explicit = _build(explicit, tmp_path)
    assert worker_explicit.engine == worker.engine
    assert worker_explicit.multi_leg_engine is None


def test_multi_leg_engine_kind_builds_a_multi_leg_worker_config(tmp_path: Path):
    from common.config import EngineKind

    cfg = _cfg(
        strategy_id="straddle_920",
        engine=EngineKind.MULTI_LEG_ENGINE,
        parameters={
            "strategy_ref": (
                "strategies.intraday_options.straddle_920.strategy:Straddle920Strategy"
            ),
            "strategy_kwargs": {"lots_per_leg": 10},
            "vix_security_id": "21",
            "vix_index_segment": "IDX_I",
        },
    )
    worker = _build(cfg, tmp_path)

    assert worker.engine is None
    assert worker.multi_leg_engine is not None
    assert worker.multi_leg_engine.strategy_ref.endswith("Straddle920Strategy")
    assert worker.multi_leg_engine.lots == 10
    assert worker.multi_leg_engine.vix_security_id == "21"
    assert worker.multi_leg_engine.vix_segment == "IDX_I"


def test_multi_leg_engine_requires_a_strategy_ref(tmp_path: Path):
    from common.config import EngineKind

    cfg = _cfg(strategy_id="straddle_920", engine=EngineKind.MULTI_LEG_ENGINE, parameters={})
    # _cfg's defaults already set strategy_ref to the EMA class; strip it.
    cfg.strategy.parameters.pop("strategy_ref", None)
    with pytest.raises(ConfigError, match="strategy_ref"):
        _build(cfg, tmp_path)


def test_multi_leg_engine_rejects_an_unknown_vix_segment(tmp_path: Path):
    from common.config import EngineKind

    cfg = _cfg(
        strategy_id="straddle_920",
        engine=EngineKind.MULTI_LEG_ENGINE,
        parameters={
            "strategy_ref": (
                "strategies.intraday_options.straddle_920.strategy:Straddle920Strategy"
            ),
            "vix_security_id": "21",
            "vix_index_segment": "NOT_A_REAL_SEGMENT",
        },
    )
    with pytest.raises(ConfigError, match="exchange segment"):
        _build(cfg, tmp_path)


def test_multi_leg_engine_refuses_live_mode(tmp_path: Path):
    """Calls the builder directly rather than through a full live
    ``ResolvedConfig`` (which needs a complete live-preflight block just to
    construct) — this is specifically pinning
    ``_build_multi_leg_engine_worker_config``'s own belt-and-suspenders
    refusal, on top of (never instead of) ``ResolvedConfig``'s own
    cross-field live-preflight validator and ``effective_live_gate``."""
    from common.config import EngineKind
    from runtimes.intraday_options.config_adapter import (
        _build_multi_leg_engine_worker_config,
    )

    strategy = StrategyConfig(
        strategy_id="straddle_920",
        runtime_id="intraday_options",
        mode=ExecutionMode.LIVE,
        live_approved=True,
        live_quantity_lots=1,
        engine=EngineKind.MULTI_LEG_ENGINE,
        parameters={
            "strategy_ref": (
                "strategies.intraday_options.straddle_920.strategy:Straddle920Strategy"
            ),
        },
    )
    with pytest.raises(ConfigError, match="not supported for the multi-leg engine"):
        _build_multi_leg_engine_worker_config(strategy, strategy.parameters)


def test_an_unsupported_engine_kind_fails_construction_rather_than_falling_back(
    tmp_path: Path,
):
    """Spec section 14.1: unsupported engine types must fail validation, never
    silently fall back to the single-leg engine."""
    from common.config import EngineKind

    cfg = _cfg(engine=EngineKind.FIXED_STRIKE_ENGINE)
    with pytest.raises(ConfigError, match="no construction path"):
        _build(cfg, tmp_path)
