"""ResolvedConfig -> WorkerConfig: the adapter Phase 5 adds."""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest

from common.config import ConfigError, EngineKind, ExecutionMode, ExpiryPolicy, ResolvedConfig
from common.config.models import (
    AccountRiskConfig,
    GlobalConfig,
    LiveOrderRateLimitConfig,
    LivePreflightConfig,
    RateLimitCallClass,
    RateLimitRule,
    RuntimeConfig,
    StrategyConfig,
)
from runtimes.intraday_options.config_adapter import build_worker_config

#: Phase 10: a ``mode: live`` StrategyConfig requires a complete
#: ``live_preflight`` block and its own ``live_quantity_lots`` (see
#: ResolvedConfig's validator). Not what test_execution_mode_is_carried_through
#: is about, so supplied unconditionally here rather than per-test.
_COMPLETE_LIVE_PREFLIGHT = LivePreflightConfig(
    expected_static_ip="203.0.113.10",
    egress_ip_provider="test",
    max_preflight_age_seconds=300,
    rate_limits=LiveOrderRateLimitConfig(
        rules=tuple(
            RateLimitRule(call_class=call_class, limit=5, window_seconds=1)
            for call_class in RateLimitCallClass
        )
    ),
    account_risk=AccountRiskConfig(
        max_daily_loss=5000.0,
        max_open_positions=2,
        max_open_legs=2,
        max_deployed_capital=100_000.0,
        max_mtm_age_seconds=30,
    ),
)


def _cfg(**strategy_overrides) -> ResolvedConfig:
    strategy_kwargs = {
        "strategy_id": "io_fixture_v1",
        "enabled": True,
        "engine": EngineKind.TRADING_ENGINE,
        "parameters": {"instrument": "NIFTY", "security_id": "99926000"},
        "risk": {},
    }
    strategy_kwargs.update(strategy_overrides)
    is_live = strategy_kwargs.get("mode") is ExecutionMode.LIVE
    if is_live:
        strategy_kwargs.setdefault("live_quantity_lots", 1)
    return ResolvedConfig(
        global_config=GlobalConfig(),
        runtime=RuntimeConfig(
            runtime_id="intraday_options",
            enabled=True,
            live_preflight=_COMPLETE_LIVE_PREFLIGHT if is_live else LivePreflightConfig(),
        ),
        strategy=StrategyConfig(**strategy_kwargs),
    )


def _build(cfg: ResolvedConfig, tmp_path: Path):
    return build_worker_config(
        cfg,
        database_path=tmp_path / "db.sqlite",
        lock_dir=tmp_path / "locks",
        pid_dir=tmp_path / "pid",
        log_dir=tmp_path / "logs",
        trading_date="2026-08-06",
    )


def test_required_parameters_are_mapped(tmp_path: Path):
    worker = _build(_cfg(), tmp_path)
    assert worker.strategy_id == "io_fixture_v1"
    assert worker.instrument == "NIFTY"
    assert worker.security_id == "99926000"
    assert worker.runtime_id == "intraday_options"
    assert worker.trading_date == "2026-08-06"


def test_missing_security_id_raises_config_error(tmp_path: Path):
    cfg = _cfg(parameters={"instrument": "NIFTY"})
    with pytest.raises(ConfigError, match="security_id"):
        _build(cfg, tmp_path)


def test_missing_instrument_raises_config_error(tmp_path: Path):
    cfg = _cfg(parameters={"security_id": "99926000"})
    with pytest.raises(ConfigError, match="instrument"):
        _build(cfg, tmp_path)


def test_optional_parameters_default_like_workerconfig(tmp_path: Path):
    worker = _build(_cfg(), tmp_path)
    assert worker.quantity == 50
    assert worker.entry_on_candle == 1
    assert worker.exit_on_candle == 3
    assert worker.paper_execution == {}


def test_optional_parameters_are_honoured_when_present(tmp_path: Path):
    cfg = _cfg(
        parameters={
            "instrument": "NIFTY",
            "security_id": "99926000",
            "quantity": 75,
            "entry_on_candle": 2,
            "exit_on_candle": 5,
            "paper_execution": {"submission_latency_ms": 250},
        }
    )
    worker = _build(cfg, tmp_path)
    assert worker.quantity == 75
    assert worker.entry_on_candle == 2
    assert worker.exit_on_candle == 5
    assert worker.paper_execution == {"submission_latency_ms": 250}


def test_risk_times_become_the_square_off_policy(tmp_path: Path):
    cfg = _cfg(risk={"entry_cutoff": "15:00", "square_off_at": "15:15"})
    worker = _build(cfg, tmp_path)
    assert worker.square_off_policy.entry_cutoff == time(15, 0)
    assert worker.square_off_policy.square_off_at == time(15, 15)


def test_bad_risk_time_format_raises_config_error(tmp_path: Path):
    cfg = _cfg(risk={"entry_cutoff": "not-a-time"})
    with pytest.raises(ConfigError, match="entry_cutoff"):
        _build(cfg, tmp_path)


def test_execution_mode_is_carried_through(tmp_path: Path):
    cfg = _cfg(mode=ExecutionMode.LIVE, live_approved=True)
    worker = _build(cfg, tmp_path)
    assert worker.execution_mode is ExecutionMode.LIVE


def test_engine_is_none_without_a_strategy_ref_regardless_of_engine_kind(tmp_path: Path):
    """EngineKind alone is not the discriminator (Phase 9): every strategy,
    fixture included, defaults ``engine: trading_engine`` — see
    config_adapter.py's module docstring. Only ``parameters.strategy_ref``
    (absent here) switches on the ported-engine path."""
    cfg = _cfg(engine=EngineKind.MULTI_LEG_ENGINE)
    worker = _build(cfg, tmp_path)
    assert worker.engine is None


def test_config_fingerprint_is_set(tmp_path: Path):
    worker = _build(_cfg(), tmp_path)
    assert worker.config_fingerprint
    assert isinstance(worker.config_fingerprint, str)


# --------------------------------------------- expiry_policy (Phase 6 Part 4)
def test_expiry_policy_defaults_reach_the_square_off_policy(tmp_path: Path):
    worker = _build(_cfg(), tmp_path)
    assert worker.square_off_policy.expiry_policy is ExpiryPolicy.FORCE_SQUARE_OFF_BEFORE_EXPIRY
    assert worker.square_off_policy.square_off_before_expiry_days == 0


def test_a_configured_expiry_lead_reaches_the_square_off_policy(tmp_path: Path):
    cfg = _cfg(square_off_before_expiry_days=3)
    worker = _build(cfg, tmp_path)
    assert worker.square_off_policy.square_off_before_expiry_days == 3
