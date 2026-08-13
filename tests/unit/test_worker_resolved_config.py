"""resolved_config_from_worker: the Phase 10 fix for the wiring gap the
Stage 1 audit found — resolved_config_stub used to hard-code
GlobalConfig(live_trading_enabled=False) and enabled=True regardless of
what the operator actually configured, so flipping every real gate to
true in YAML still could not reach the broker factory's gate check. This
proves the real values now genuinely flow through."""

from __future__ import annotations

from pathlib import Path

from common.config.models import ExecutionMode, RateLimitCallClass, effective_live_gate
from runtimes.intraday_options.worker import WorkerConfig, resolved_config_from_worker


def _worker_config(**overrides) -> WorkerConfig:
    base = dict(
        runtime_id="intraday_options",
        strategy_id="st01",
        security_id="49081",
        instrument="NIFTY",
        database_path=Path("/tmp/db.sqlite"),
        lock_dir=Path("/tmp/locks"),
        pid_dir=Path("/tmp/pid"),
        log_dir=Path("/tmp/logs"),
        trading_date="2026-08-13",
        execution_mode=ExecutionMode.PAPER,
    )
    base.update(overrides)
    return WorkerConfig(**base)


def _complete_live_fields() -> dict[str, object]:
    return {
        "live_quantity_lots": 1,
        "live_expected_static_ip": "203.0.113.10",
        "live_egress_ip_provider": "test",
        "live_max_preflight_age_seconds": 300,
        "live_rate_limit_rules": tuple(
            (call_class.value, 5, 1) for call_class in RateLimitCallClass
        ),
        "live_max_daily_loss": 5000.0,
        "live_max_open_positions": 2,
        "live_max_open_legs": 2,
        "live_max_deployed_capital": 100_000.0,
        "live_max_mtm_age_seconds": 30,
    }


def test_every_new_field_defaults_to_the_fail_closed_value():
    config = _worker_config()
    assert config.global_live_trading_enabled is False
    assert config.runtime_enabled is False
    assert config.runtime_live_execution_allowed is False
    assert config.strategy_enabled is False
    assert config.strategy_live_approved is False
    assert config.live_preflight_passed is False


def test_a_default_worker_config_produces_a_fully_blocked_live_gate():
    """The committed-config-safe case: even if execution_mode were live and
    the live-preflight *config* were fully specified (as a real strategy's
    would need to be to pass ResolvedConfig's own construction-time
    validator — see config_adapter.py, which only ever builds a
    WorkerConfig from an already-valid ResolvedConfig), every gate boolean
    default stays closed."""
    config = _worker_config(execution_mode=ExecutionMode.LIVE, **_complete_live_fields())
    resolved = resolved_config_from_worker(config)
    decision = effective_live_gate(resolved, preflight_passed=config.live_preflight_passed)
    assert not decision.allowed


def test_real_global_live_trading_enabled_value_reaches_the_gate():
    """The core fix: unlike the old stub (hard-coded False no matter what),
    a genuinely-true value must actually reach effective_live_gate."""
    config = _worker_config(
        **_complete_live_fields(),
        execution_mode=ExecutionMode.LIVE,
        global_live_trading_enabled=True,
        runtime_enabled=True,
        runtime_live_execution_allowed=True,
        strategy_enabled=True,
        strategy_live_approved=True,
        live_preflight_passed=True,
    )
    resolved = resolved_config_from_worker(config)
    decision = effective_live_gate(resolved, preflight_passed=config.live_preflight_passed)
    assert decision.allowed


def test_each_gate_field_alone_still_blocks_when_the_others_are_true():
    """Mirrors test_broker_factory.py's own
    test_every_single_gate_alone_is_enough_to_block, at this layer."""
    base_true = dict(
        **_complete_live_fields(),
        execution_mode=ExecutionMode.LIVE,
        global_live_trading_enabled=True,
        runtime_enabled=True,
        runtime_live_execution_allowed=True,
        strategy_enabled=True,
        strategy_live_approved=True,
        live_preflight_passed=True,
    )
    for field in (
        "global_live_trading_enabled",
        "runtime_enabled",
        "runtime_live_execution_allowed",
        "strategy_enabled",
        "strategy_live_approved",
    ):
        kwargs = dict(base_true)
        kwargs[field] = False
        config = _worker_config(**kwargs)
        resolved = resolved_config_from_worker(config)
        decision = effective_live_gate(resolved, preflight_passed=config.live_preflight_passed)
        assert not decision.allowed, f"{field}=False alone should have blocked"


def test_a_paper_worker_config_needs_no_live_preflight_fields_at_all():
    """The ResolvedConfig cross-field validator only requires live_preflight
    completeness when mode is live — a paper WorkerConfig with every new
    field at its default must construct cleanly."""
    config = _worker_config(execution_mode=ExecutionMode.PAPER)
    resolved = resolved_config_from_worker(config)
    assert resolved.strategy.mode is ExecutionMode.PAPER
    assert resolved.strategy.live_quantity_lots is None


def test_every_rate_rule_round_trips():
    live_fields = _complete_live_fields()
    live_fields["live_rate_limit_rules"] = tuple(
        (call_class.value, 7 if call_class is RateLimitCallClass.NEW_ORDER else 5, 45)
        for call_class in RateLimitCallClass
    )
    config = _worker_config(
        **live_fields,
        execution_mode=ExecutionMode.LIVE,
        global_live_trading_enabled=True,
        runtime_enabled=True,
        runtime_live_execution_allowed=True,
        strategy_enabled=True,
        strategy_live_approved=True,
    )
    resolved = resolved_config_from_worker(config)
    rules = resolved.runtime.live_preflight.rate_limits.rules
    assert len(rules) == 4
    new_order = next(rule for rule in rules if rule.call_class is RateLimitCallClass.NEW_ORDER)
    assert new_order.limit == 7
    assert new_order.window_seconds == 45
