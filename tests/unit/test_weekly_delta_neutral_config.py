"""Config validation boundaries for ``weekly_delta_neutral``:
``WeeklyDeltaNeutralParameters``'s own ``model_validator``s, the strategy
constructor's worker-only-key tolerance (a genuine typo must still fail),
and ``runtimes.positional_options.config_adapter.build_worker_config``'s
fail-closed checks."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from common.config import (
    ConfigError,
    EngineKind,
    GlobalConfig,
    ResolvedConfig,
    RuntimeConfig,
    StrategyConfig,
)
from runtimes.positional_options.config_adapter import build_worker_config
from strategies.positional_options.weekly_delta_neutral.config import (
    AdjustmentConfig,
    ExecutionConfig,
    ExitsConfig,
    OpeningFilterConfig,
    ScheduleConfig,
    SelectionConfig,
    VolatilityGateConfig,
    WeeklyDeltaNeutralParameters,
)
from strategies.positional_options.weekly_delta_neutral.strategy import WeeklyDeltaNeutralStrategy


# --------------------------------------------------------- SelectionConfig
def test_hedge_call_delta_must_be_farther_otm_than_short_call() -> None:
    with pytest.raises(ValidationError, match="hedge_call_delta"):
        SelectionConfig(short_call_delta=0.20, hedge_call_delta=0.25)


def test_hedge_put_delta_must_be_farther_otm_than_short_put() -> None:
    with pytest.raises(ValidationError, match="hedge_put_delta"):
        SelectionConfig(short_put_delta=-0.20, hedge_put_delta=-0.25)


def test_minimum_hedge_width_must_not_exceed_maximum() -> None:
    with pytest.raises(ValidationError, match="minimum_hedge_width_points"):
        SelectionConfig(minimum_hedge_width_points=600, maximum_hedge_width_points=500)


# --------------------------------------------------------- AdjustmentConfig
def test_adjustment_thresholds_must_be_strictly_ordered() -> None:
    with pytest.raises(ValidationError, match="target_delta_per_lot"):
        AdjustmentConfig(
            target_delta_per_lot=10.0, warning_delta_per_lot=8.0, trigger_delta_per_lot=12.0
        )


# -------------------------------------------------------------- ExitsConfig
def test_loss_multiples_must_be_strictly_increasing() -> None:
    with pytest.raises(ValidationError, match="soft_loss_credit_multiple"):
        ExitsConfig(
            soft_loss_credit_multiple=1.5, hard_loss_credit_multiple=1.5,
            emergency_loss_credit_multiple=1.75,
        )


# ------------------------------------------------------------ ScheduleConfig
def test_entry_day_must_be_wednesday() -> None:
    with pytest.raises(ValidationError, match="WEDNESDAY"):
        ScheduleConfig(entry_day="THURSDAY")


def test_entry_window_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="entry_window_start"):
        ScheduleConfig(entry_window_start="09:40", entry_window_end="09:25")


def test_expiry_day_phases_must_be_strictly_ordered() -> None:
    with pytest.raises(ValidationError, match="expiry-day timing"):
        ScheduleConfig(expiry_adjustment_cutoff="12:00", expiry_tighten_at="12:00")


def test_adjustment_end_must_not_be_scheduled_after_hard_exit() -> None:
    with pytest.raises(ValidationError, match="adjustment_end"):
        ScheduleConfig(adjustment_end="15:20", hard_exit="15:15")


# ------------------------------------------------------- VolatilityGateConfig
def test_volatility_gate_method_atr_is_rejected_with_the_plumbing_reason() -> None:
    with pytest.raises(ValidationError, match="TICKS"):
        VolatilityGateConfig(method="atr")


def test_volatility_gate_method_rejects_an_unknown_value() -> None:
    with pytest.raises(ValidationError, match="must be 'displacement' or 'realized'"):
        VolatilityGateConfig(method="ema_slope")


def test_volatility_gate_lookback_must_be_at_least_three() -> None:
    # 2 would yield exactly 1 return -- a standard deviation needs at
    # least 2 data points, so lookback: 2 could never confirm "normal".
    with pytest.raises(ValidationError):
        VolatilityGateConfig(lookback=2)


def test_volatility_gate_confirmations_required_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        VolatilityGateConfig(confirmations_required=0)


def test_volatility_gate_defaults_are_the_new_realized_method() -> None:
    cfg = VolatilityGateConfig()
    assert cfg.method == "realized"
    assert cfg.gap_veto_enabled is False


# ---------------------------------------- entry-window / skip_after cross-field
def test_skip_after_must_not_be_before_entry_window_end() -> None:
    with pytest.raises(ValidationError, match="skip_after"):
        WeeklyDeltaNeutralParameters(
            schedule=ScheduleConfig(entry_window_end="12:00"),
            opening_filter=OpeningFilterConfig(skip_after="11:00"),
        )


def test_skip_after_equal_to_entry_window_end_is_allowed() -> None:
    WeeklyDeltaNeutralParameters(
        schedule=ScheduleConfig(entry_window_end="12:00"),
        opening_filter=OpeningFilterConfig(skip_after="12:00"),
    )


def test_default_entry_window_is_widened_to_noon() -> None:
    schedule = ScheduleConfig()
    assert schedule.entry_window_end == "12:00"
    assert OpeningFilterConfig().skip_after == "12:00"


# ----------------------------------------------------------- ExecutionConfig
def test_order_type_must_be_limit() -> None:
    with pytest.raises(ValidationError, match="LIMIT"):
        ExecutionConfig(order_type="MARKET")


# ------------------------------------------------- no lot_size, extra="forbid"
def test_no_lot_size_field_exists_on_the_parameters_model() -> None:
    assert "lot_size" not in WeeklyDeltaNeutralParameters.model_fields


def test_an_unknown_parameter_key_fails_closed() -> None:
    with pytest.raises(ValidationError):
        WeeklyDeltaNeutralParameters(underlying="NIFTY", not_a_real_field=1)


# --------------------------------------------- strategy worker-key tolerance
def _fixture_scrip_master() -> object:
    class _Fixture:
        underlying = "NIFTY"

        def nearest_expiry(self, on=None):  # type: ignore[no-untyped-def]
            return "2026-08-26"

    return _Fixture()


def test_strategy_construction_tolerates_worker_only_keys_alongside_real_ones() -> None:
    strategy = WeeklyDeltaNeutralStrategy(
        parameters={
            "underlying": "NIFTY",
            "lots": 1,
            # Worker-only keys that live in the same YAML parameters: block
            # but are never read by WeeklyDeltaNeutralParameters itself.
            "strategy_ref": "strategies.positional_options.weekly_delta_neutral.strategy:X",
            "paper_execution": {"allow_ltp_fallback": False},
            "cost_rates": {"brokerage_flat": 20},
            "evaluation_interval_seconds": 5.0,
        },
        scrip_master=_fixture_scrip_master(),
    )
    assert strategy.lots == 1


def test_strategy_construction_still_fails_closed_on_a_genuine_typo() -> None:
    with pytest.raises(ValidationError):
        WeeklyDeltaNeutralStrategy(
            parameters={"underlying": "NIFTY", "lot_size": 75},  # never a real field
            scrip_master=_fixture_scrip_master(),
        )


# --------------------------------------------------- build_worker_config
def _resolved_config(parameters: dict[str, object], **overrides: object) -> ResolvedConfig:
    strategy = StrategyConfig(
        strategy_id="weekly_delta_neutral",
        runtime_id="positional_options",
        enabled=False,
        mode=overrides.get("mode", "paper"),
        live_approved=False,
        engine=overrides.get("engine", EngineKind.POSITIONAL_MULTI_LEG_ENGINE),
        parameters=parameters,
        # Only relevant to the mode: live test below; StrategyConfig itself
        # requires this the moment mode is live, before build_worker_config
        # ever gets a chance to refuse it a second time.
        live_quantity_lots=1 if overrides.get("mode") == "live" else None,
    )
    return ResolvedConfig(
        global_config=GlobalConfig(),
        runtime=RuntimeConfig(runtime_id="positional_options", enabled=False),
        strategy=strategy,
    )


def test_build_worker_config_rejects_a_non_positional_engine_kind() -> None:
    cfg = _resolved_config(
        {
            "underlying": "NIFTY", "index_security_id": "", "index_segment": "",
            "fno_segment": "",
        },
        engine=EngineKind.TRADING_ENGINE,
    )
    with pytest.raises(ConfigError, match="positional_multi_leg_engine"):
        build_worker_config(cfg, trading_date="2026-08-19")


def test_build_worker_config_rejects_a_missing_required_parameter() -> None:
    cfg = _resolved_config({"underlying": "NIFTY"})
    with pytest.raises(ConfigError, match="index_security_id"):
        build_worker_config(cfg, trading_date="2026-08-19")


def test_build_worker_config_rejects_a_lot_size_key() -> None:
    cfg = _resolved_config(
        {
            "underlying": "NIFTY", "index_security_id": "", "index_segment": "",
            "fno_segment": "", "lot_size": 75,
        }
    )
    with pytest.raises(ConfigError, match="lot_size"):
        build_worker_config(cfg, trading_date="2026-08-19")


def test_build_worker_config_rejects_live_mode() -> None:
    # ResolvedConfig's own validator already refuses to construct a live
    # strategy without a complete live-preflight contract — genuine defense
    # in depth, proven separately by common.config's own test suite. This
    # test isolates build_worker_config's *own*, independent live refusal
    # (relevant if a positional strategy ever shares a runtime group with a
    # legitimately live-preflight-configured one): model_copy bypasses
    # ResolvedConfig's validators entirely, on purpose, so only
    # build_worker_config's own check is exercised here.
    from common.config.models import ExecutionMode

    cfg = _resolved_config(
        {
            "underlying": "NIFTY", "index_security_id": "", "index_segment": "",
            "fno_segment": "",
        }
    )
    live_strategy = cfg.strategy.model_copy(
        update={"mode": ExecutionMode.LIVE, "live_quantity_lots": 1}
    )
    live_cfg = cfg.model_copy(update={"strategy": live_strategy})
    with pytest.raises(ConfigError, match="does not support live"):
        build_worker_config(live_cfg, trading_date="2026-08-19")


def test_build_worker_config_resolves_a_valid_config() -> None:
    cfg = _resolved_config(
        {
            "underlying": "NIFTY", "index_security_id": "", "index_segment": "",
            "fno_segment": "", "lots": 2,
        }
    )
    worker_config = build_worker_config(cfg, trading_date="2026-08-19")
    assert worker_config.runtime_id == "positional_options"
    assert worker_config.strategy_id == "weekly_delta_neutral"
    assert worker_config.lots == 2
    assert worker_config.underlying_security_id == "13"  # NIFTY's registry id
    assert worker_config.option_segment == "NSE_FNO"
