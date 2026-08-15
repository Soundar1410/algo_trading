"""Layered config resolution, strict validation and the live safety gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from common.config import (
    AccountRiskConfig,
    ConfigError,
    EngineKind,
    ExecutionMode,
    ExpiryPolicy,
    GlobalConfig,
    HealthConfig,
    LiveOrderRateLimitConfig,
    LivePreflightConfig,
    RateLimitCallClass,
    RateLimitRule,
    ResolvedConfig,
    RetentionConfig,
    RuntimeConfig,
    Settings,
    StrategyConfig,
    apply_env_overrides,
    deep_merge,
    discover_enabled_strategies,
    discover_strategies,
    effective_live_gate,
    fingerprint,
    load_resolved_config,
    load_runtime_config,
    load_strategy_config,
)

GLOBAL_YAML = """
global:
  live_trading_enabled: false
  timezone: Asia/Kolkata
runtime_defaults:
  enabled: false
  live_execution_allowed: false
  shared_market_feed: true
strategy_defaults:
  enabled: false
  mode: paper
  live_approved: false
  risk:
    max_lots: 1
    max_daily_loss: 5000
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def populated_config(config_root: Path) -> Path:
    _write(config_root / "global.yaml", GLOBAL_YAML)
    _write(
        config_root / "runtimes" / "intraday_options.yaml",
        "runtime_id: intraday_options\nenabled: true\n",
    )
    _write(
        config_root / "strategies" / "io_fixture_v1.yaml",
        "strategy_id: io_fixture_v1\nruntime_id: intraday_options\n"
        "enabled: true\nengine: trading_engine\n",
    )
    return config_root


# ------------------------------------------------------------- deep_merge
def test_deep_merge_recurses_into_nested_mappings():
    base = {"risk": {"max_lots": 1, "max_loss": 100}, "keep": True}
    override = {"risk": {"max_loss": 250}}
    assert deep_merge(base, override) == {
        "risk": {"max_lots": 1, "max_loss": 250},
        "keep": True,
    }


def test_deep_merge_replaces_non_mapping_values_wholesale():
    assert deep_merge({"legs": [1, 2, 3]}, {"legs": [9]}) == {"legs": [9]}


def test_deep_merge_does_not_mutate_its_inputs():
    base = {"risk": {"max_lots": 1}}
    deep_merge(base, {"risk": {"max_lots": 5}})
    assert base == {"risk": {"max_lots": 1}}


# ----------------------------------------------------------------- layering
def test_runtime_inherits_global_defaults_and_overrides_them(populated_config: Path):
    runtime = load_runtime_config(populated_config, "intraday_options")
    assert runtime.enabled is True  # from the runtime file
    assert runtime.live_execution_allowed is False  # inherited from global defaults
    assert runtime.shared_market_feed is True


def test_a_runtime_with_no_health_block_gets_the_documented_default(populated_config: Path):
    """common.health.heartbeat.DEFAULT_INTERVAL_SECONDS and this default must
    never silently drift apart — see tests/unit/test_heartbeat.py's own check
    of the other direction."""
    runtime = load_runtime_config(populated_config, "intraday_options")
    assert runtime.health == HealthConfig()
    assert runtime.health.heartbeat_interval_seconds == 10.0


def test_a_runtime_yaml_can_override_the_heartbeat_interval(populated_config: Path):
    _write(
        populated_config / "runtimes" / "intraday_options.yaml",
        "runtime_id: intraday_options\nenabled: true\nhealth:\n  heartbeat_interval_seconds: 5\n",
    )
    runtime = load_runtime_config(populated_config, "intraday_options")
    assert runtime.health.heartbeat_interval_seconds == 5.0


def test_a_non_positive_heartbeat_interval_is_rejected():
    with pytest.raises(Exception, match="greater than 0"):
        HealthConfig(heartbeat_interval_seconds=0)


def test_a_runtime_with_no_retention_block_gets_the_documented_default(populated_config: Path):
    """common.retention.policy's DEFAULT_* constants and this default must
    never silently drift apart — see tests/unit/test_retention.py's own check
    of the other direction."""
    runtime = load_runtime_config(populated_config, "intraday_options")
    assert runtime.retention == RetentionConfig()
    assert runtime.retention.log_max_age_days == 30
    assert runtime.retention.log_compress_after_days == 1
    assert runtime.retention.db_row_max_age_days == 90
    assert runtime.retention.db_delete_batch_limit == 5000
    assert runtime.retention.backup_retain_count == 7
    assert runtime.retention.scrip_cache_retain_count == 3


def test_a_runtime_yaml_can_override_a_retention_setting(populated_config: Path):
    _write(
        populated_config / "runtimes" / "intraday_options.yaml",
        "runtime_id: intraday_options\nenabled: true\nretention:\n  backup_retain_count: 2\n",
    )
    runtime = load_runtime_config(populated_config, "intraday_options")
    assert runtime.retention.backup_retain_count == 2
    assert runtime.retention.log_max_age_days == 30  # untouched defaults stay the defaults


def test_a_non_positive_retention_setting_is_rejected():
    with pytest.raises(Exception, match="greater than 0"):
        RetentionConfig(db_delete_batch_limit=0)


def test_an_unknown_key_inside_the_retention_block_is_rejected(populated_config: Path):
    _write(
        populated_config / "runtimes" / "intraday_options.yaml",
        "runtime_id: intraday_options\nenabled: true\nretention:\n  backup_retain_count: 2\n"
        "  bakcup_retain_count: 2\n",
    )
    with pytest.raises(ConfigError):
        load_runtime_config(populated_config, "intraday_options")


def test_an_unknown_key_inside_the_health_block_is_rejected(populated_config: Path):
    _write(
        populated_config / "runtimes" / "intraday_options.yaml",
        "runtime_id: intraday_options\nenabled: true\nhealth:\n  heartbeat_interval_seconds: 5\n"
        "  hearbeat_interval_seconds: 5\n",
    )
    with pytest.raises(ConfigError):
        load_runtime_config(populated_config, "intraday_options")


def test_strategy_inherits_global_then_runtime_then_own_file(populated_config: Path):
    _write(
        populated_config / "runtimes" / "intraday_options.yaml",
        "runtime_id: intraday_options\nenabled: true\n"
        "strategy_defaults:\n  risk:\n    max_daily_loss: 2000\n",
    )
    strategy = load_strategy_config(
        populated_config, "io_fixture_v1", runtime_id="intraday_options"
    )
    # global default survives where nothing overrode it
    assert strategy.risk["max_lots"] == 1
    # runtime layer beats the global layer
    assert strategy.risk["max_daily_loss"] == 2000
    # the strategy's own file wins outright
    assert strategy.enabled is True


def test_strategy_file_overrides_runtime_defaults(populated_config: Path):
    _write(
        populated_config / "runtimes" / "intraday_options.yaml",
        "runtime_id: intraday_options\nenabled: true\n"
        "strategy_defaults:\n  risk:\n    max_daily_loss: 2000\n",
    )
    _write(
        populated_config / "strategies" / "io_fixture_v1.yaml",
        "strategy_id: io_fixture_v1\nruntime_id: intraday_options\n"
        "enabled: true\nrisk:\n  max_daily_loss: 750\n",
    )
    strategy = load_strategy_config(
        populated_config, "io_fixture_v1", runtime_id="intraday_options"
    )
    assert strategy.risk["max_daily_loss"] == 750


def test_resolved_config_carries_all_three_layers(populated_config: Path):
    cfg = load_resolved_config(
        populated_config, "intraday_options", "io_fixture_v1", settings=Settings()
    )
    assert cfg.global_config.live_trading_enabled is False
    assert cfg.runtime.runtime_id == "intraday_options"
    assert cfg.strategy.strategy_id == "io_fixture_v1"
    assert cfg.strategy.mode is ExecutionMode.PAPER
    assert cfg.strategy.engine is EngineKind.TRADING_ENGINE


# -------------------------------------------------- strict / invalid config
def test_unknown_key_is_rejected_not_ignored(populated_config: Path):
    """A typo in a safety flag must fail loudly, not read as False."""
    _write(
        populated_config / "strategies" / "io_fixture_v1.yaml",
        "strategy_id: io_fixture_v1\nruntime_id: intraday_options\n"
        "enabled: true\nlive_aproved: true\n",
    )
    with pytest.raises(ConfigError, match="Invalid configuration"):
        load_strategy_config(populated_config, "io_fixture_v1")


def test_invalid_execution_mode_is_rejected(populated_config: Path):
    _write(
        populated_config / "strategies" / "io_fixture_v1.yaml",
        "strategy_id: io_fixture_v1\nruntime_id: intraday_options\nmode: simulated\n",
    )
    with pytest.raises(ConfigError):
        load_strategy_config(populated_config, "io_fixture_v1")


def test_missing_file_raises_config_error(config_root: Path):
    _write(config_root / "global.yaml", GLOBAL_YAML)
    with pytest.raises(ConfigError, match="not found"):
        load_runtime_config(config_root, "does_not_exist")


def test_malformed_yaml_raises_config_error(config_root: Path):
    _write(config_root / "global.yaml", "global: {this is: not: valid")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        load_runtime_config(config_root, "intraday_options")


def test_id_mismatch_between_filename_and_body_is_rejected(populated_config: Path):
    _write(
        populated_config / "strategies" / "io_fixture_v1.yaml",
        "strategy_id: something_else\nruntime_id: intraday_options\nenabled: true\n",
    )
    with pytest.raises(ConfigError, match="mismatch"):
        load_strategy_config(populated_config, "io_fixture_v1")


# --------------------------------------------- expiry_policy (Phase 6 Part 4)
def test_expiry_policy_defaults_to_force_square_off_before_expiry(populated_config: Path):
    strategy = load_strategy_config(populated_config, "io_fixture_v1")
    assert strategy.expiry_policy is ExpiryPolicy.FORCE_SQUARE_OFF_BEFORE_EXPIRY
    assert strategy.square_off_before_expiry_days == 0


def test_simulate_exchange_settlement_is_refused_at_load(populated_config: Path):
    """Spec section 11: only permitted 'after settlement tests pass'. None exist."""
    _write(
        populated_config / "strategies" / "io_fixture_v1.yaml",
        "strategy_id: io_fixture_v1\nruntime_id: intraday_options\n"
        "enabled: true\nexpiry_policy: simulate_exchange_settlement\n",
    )
    with pytest.raises(ConfigError, match="settlement tests pass"):
        load_strategy_config(populated_config, "io_fixture_v1")


def test_an_unknown_expiry_policy_value_is_rejected(populated_config: Path):
    _write(
        populated_config / "strategies" / "io_fixture_v1.yaml",
        "strategy_id: io_fixture_v1\nruntime_id: intraday_options\n"
        "enabled: true\nexpiry_policy: hold_forever\n",
    )
    with pytest.raises(ConfigError):
        load_strategy_config(populated_config, "io_fixture_v1")


def test_a_negative_square_off_before_expiry_days_is_rejected(populated_config: Path):
    _write(
        populated_config / "strategies" / "io_fixture_v1.yaml",
        "strategy_id: io_fixture_v1\nruntime_id: intraday_options\n"
        "enabled: true\nsquare_off_before_expiry_days: -1\n",
    )
    with pytest.raises(ConfigError):
        load_strategy_config(populated_config, "io_fixture_v1")


def test_a_configured_expiry_lead_is_loaded(populated_config: Path):
    _write(
        populated_config / "strategies" / "io_fixture_v1.yaml",
        "strategy_id: io_fixture_v1\nruntime_id: intraday_options\n"
        "enabled: true\nsquare_off_before_expiry_days: 2\n",
    )
    strategy = load_strategy_config(populated_config, "io_fixture_v1")
    assert strategy.square_off_before_expiry_days == 2


# ------------------------------------------------------------ env overrides
def test_env_override_can_force_live_trading_off():
    enabled = GlobalConfig(live_trading_enabled=True)
    settings = Settings(algo_live_trading_enabled="false")
    assert apply_env_overrides(enabled, settings).live_trading_enabled is False


def test_env_override_can_never_turn_live_trading_on():
    """Enabling real money must be a deliberate file edit, never a stale export."""
    disabled = GlobalConfig(live_trading_enabled=False)
    settings = Settings(algo_live_trading_enabled="true")
    assert apply_env_overrides(disabled, settings).live_trading_enabled is False


def test_absent_env_override_leaves_config_untouched():
    enabled = GlobalConfig(live_trading_enabled=True)
    assert apply_env_overrides(enabled, Settings()).live_trading_enabled is True


# --------------------------------------------------------------- live gate
#: Phase 10 added two more fail-closed requirements for any ``mode: live``
#: strategy — a declared ``live_quantity_lots`` and a complete
#: ``live_preflight`` block (see ``ResolvedConfig``'s own validator). Neither
#: is what these particular tests are about (they exercise the pre-existing
#: global/runtime/strategy permission gate), so the helper below always
#: supplies both when constructing a live-mode config, keeping every
#: existing assertion in this file unchanged.
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


def _resolved(
    *,
    global_live: bool,
    runtime_enabled: bool,
    runtime_live_allowed: bool,
    strategy_enabled: bool,
    mode: ExecutionMode,
    live_approved: bool,
) -> ResolvedConfig:
    return ResolvedConfig(
        global_config=GlobalConfig(live_trading_enabled=global_live),
        runtime=RuntimeConfig(
            runtime_id="intraday_options",
            enabled=runtime_enabled,
            live_execution_allowed=runtime_live_allowed,
            live_preflight=_COMPLETE_LIVE_PREFLIGHT,
        ),
        strategy=StrategyConfig(
            strategy_id="io_fixture_v1",
            runtime_id="intraday_options",
            enabled=strategy_enabled,
            mode=mode,
            live_approved=live_approved,
            live_quantity_lots=1 if mode is ExecutionMode.LIVE else None,
        ),
    )


_ALL_PERMISSIONS_GRANTED = {
    "global_live": True,
    "runtime_enabled": True,
    "runtime_live_allowed": True,
    "strategy_enabled": True,
    "mode": ExecutionMode.LIVE,
    "live_approved": True,
}


def test_paper_strategy_is_never_granted_live():
    cfg = _resolved(**{**_ALL_PERMISSIONS_GRANTED, "mode": ExecutionMode.PAPER})
    decision = effective_live_gate(cfg, preflight_passed=True)
    assert decision.allowed is False
    assert "paper" in decision.blocked_reasons[0]


def test_gate_is_fail_closed_when_preflight_is_not_run():
    """The default must block: a caller that forgets preflight gets no live path."""
    cfg = _resolved(**_ALL_PERMISSIONS_GRANTED)
    decision = effective_live_gate(cfg)
    assert decision.allowed is False
    assert any("preflight" in reason for reason in decision.blocked_reasons)


@pytest.mark.parametrize(
    "revoked",
    ["global_live", "runtime_enabled", "runtime_live_allowed", "strategy_enabled", "live_approved"],
)
def test_every_single_revoked_permission_blocks_live(revoked: str):
    cfg = _resolved(**{**_ALL_PERMISSIONS_GRANTED, revoked: False})
    assert effective_live_gate(cfg, preflight_passed=True).allowed is False


def test_gate_reports_every_failing_condition_not_just_the_first():
    cfg = _resolved(
        global_live=False,
        runtime_enabled=False,
        runtime_live_allowed=False,
        strategy_enabled=False,
        mode=ExecutionMode.LIVE,
        live_approved=False,
    )
    decision = effective_live_gate(cfg)
    assert len(decision.blocked_reasons) == 6


def test_gate_allows_only_when_every_condition_holds():
    cfg = _resolved(**_ALL_PERMISSIONS_GRANTED)
    decision = effective_live_gate(cfg, preflight_passed=True)
    assert decision.allowed is True
    assert bool(decision) is True


def test_shipped_repository_config_cannot_reach_a_live_allow(populated_config: Path):
    """Phase 0's own config must be incapable of granting live execution."""
    repo_config = Path(__file__).resolve().parents[2] / "config"
    from common.config import load_global_config

    assert load_global_config(repo_config).live_trading_enabled is False


def test_shipped_positional_options_runtime_is_valid_and_disabled():
    """D56: the positional_options runtime file is real, inert scaffolding —
    loadable today, disabled until Phase 6/9 build something to enable."""
    repo_config = Path(__file__).resolve().parents[2] / "config"
    runtime = load_runtime_config(repo_config, "positional_options")
    assert runtime.runtime_id == "positional_options"
    assert runtime.enabled is False


# ------------------------------------------------------------- fingerprint
def test_fingerprint_is_stable_across_key_order():
    a = {"b": 2, "a": {"y": 1, "x": 2}}
    b = {"a": {"x": 2, "y": 1}, "b": 2}
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_changes_when_any_value_changes(populated_config: Path):
    cfg = load_resolved_config(
        populated_config, "intraday_options", "io_fixture_v1", settings=Settings()
    )
    changed = cfg.model_copy(
        update={"strategy": cfg.strategy.model_copy(update={"live_approved": True})}
    )
    assert fingerprint(cfg) != fingerprint(changed)


def test_fingerprint_is_deterministic_across_calls(populated_config: Path):
    cfg = load_resolved_config(
        populated_config, "intraday_options", "io_fixture_v1", settings=Settings()
    )
    assert fingerprint(cfg) == fingerprint(cfg)


# --------------------------------------------------------- strategy discovery
def test_discovery_returns_only_enabled_strategies(populated_config: Path):
    _write(
        populated_config / "strategies" / "io_disabled_v1.yaml",
        "strategy_id: io_disabled_v1\nruntime_id: intraday_options\nenabled: false\n",
    )
    resolved = discover_enabled_strategies(
        populated_config, "intraday_options", settings=Settings()
    )
    assert [cfg.strategy.strategy_id for cfg in resolved] == ["io_fixture_v1"]


def test_discovery_resolves_every_enabled_strategy_against_the_given_runtime(
    populated_config: Path,
):
    _write(
        populated_config / "strategies" / "io_second_v1.yaml",
        "strategy_id: io_second_v1\nruntime_id: intraday_options\nenabled: true\n",
    )
    resolved = discover_enabled_strategies(
        populated_config, "intraday_options", settings=Settings()
    )
    # Sorted filename order: deterministic across runs.
    assert [cfg.strategy.strategy_id for cfg in resolved] == ["io_fixture_v1", "io_second_v1"]
    assert all(cfg.runtime.runtime_id == "intraday_options" for cfg in resolved)


def test_discovery_returns_empty_when_no_strategies_directory_exists(tmp_path: Path):
    assert discover_enabled_strategies(tmp_path / "config", "intraday_options") == []


def test_discovery_propagates_a_broken_strategy_file(populated_config: Path):
    """A malformed strategy file is an operator error — the group does not
    start short one strategy nobody noticed."""
    _write(
        populated_config / "strategies" / "io_broken_v1.yaml",
        "strategy_id: io_broken_v1\nruntime_id: intraday_options\n"
        "enabled: true\nlive_aproved: true\n",
    )
    with pytest.raises(ConfigError, match="Invalid configuration"):
        discover_enabled_strategies(populated_config, "intraday_options", settings=Settings())


# ------------------------------------------------- runtime_id, required and exact
def test_a_strategy_file_missing_runtime_id_fails_to_load(populated_config: Path):
    _write(
        populated_config / "strategies" / "io_fixture_v1.yaml",
        "strategy_id: io_fixture_v1\nenabled: true\n",
    )
    with pytest.raises(ConfigError, match="runtime_id"):
        load_strategy_config(populated_config, "io_fixture_v1")


def test_a_strategy_file_with_a_blank_runtime_id_fails_to_load(populated_config: Path):
    _write(
        populated_config / "strategies" / "io_fixture_v1.yaml",
        "strategy_id: io_fixture_v1\nruntime_id: ''\nenabled: true\n",
    )
    with pytest.raises(ConfigError, match="runtime_id"):
        load_strategy_config(populated_config, "io_fixture_v1")


def test_a_strategy_file_naming_an_unknown_runtime_fails_to_load(populated_config: Path):
    _write(
        populated_config / "strategies" / "io_fixture_v1.yaml",
        "strategy_id: io_fixture_v1\nruntime_id: no_such_runtime\nenabled: true\n",
    )
    with pytest.raises(ConfigError, match="no_such_runtime"):
        load_strategy_config(populated_config, "io_fixture_v1")


def test_a_strategy_config_object_with_a_blank_runtime_id_is_rejected():
    with pytest.raises(Exception, match="runtime_id"):
        StrategyConfig(strategy_id="x", runtime_id="   ")


def test_a_resolved_config_whose_strategy_declares_a_different_runtime_is_rejected():
    """Defense in depth (models.py's own cross-field validator), independent
    of the loader's own check."""
    with pytest.raises(Exception, match="does not match"):
        ResolvedConfig(
            global_config=GlobalConfig(),
            runtime=RuntimeConfig(runtime_id="intraday_options"),
            strategy=StrategyConfig(strategy_id="x", runtime_id="positional_options"),
        )


def test_discover_strategies_returns_only_an_exact_runtime_match(populated_config: Path):
    """A strategy belonging to a different, valid runtime is not an error —
    it is simply absent from this runtime's discovery."""
    _write(
        populated_config / "runtimes" / "positional_options.yaml",
        "runtime_id: positional_options\nenabled: false\n",
    )
    _write(
        populated_config / "strategies" / "po_other.yaml",
        "strategy_id: po_other\nruntime_id: positional_options\nenabled: true\n",
    )
    resolved = discover_strategies(populated_config, "intraday_options", settings=Settings())
    assert [cfg.strategy.strategy_id for cfg in resolved] == ["io_fixture_v1"]

    resolved_other = discover_strategies(
        populated_config, "positional_options", settings=Settings()
    )
    assert [cfg.strategy.strategy_id for cfg in resolved_other] == ["po_other"]


def test_discover_strategies_fails_closed_on_any_file_with_an_unknown_runtime(
    populated_config: Path,
):
    """Even a strategy file that does not belong to the runtime being
    discovered must not be able to silently name a nonexistent runtime —
    a typo'd runtime_id must not make a strategy quietly invisible to every
    runtime group."""
    _write(
        populated_config / "strategies" / "po_other.yaml",
        "strategy_id: po_other\nruntime_id: no_such_runtime\nenabled: true\n",
    )
    with pytest.raises(ConfigError, match="no_such_runtime"):
        discover_strategies(populated_config, "intraday_options", settings=Settings())
