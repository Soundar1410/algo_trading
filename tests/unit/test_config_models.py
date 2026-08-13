"""Phase 10 config models: strict, validated, fail-closed by omission.

Every new live-only field here has the same property: a runtime group that
never enables a live strategy pays nothing (every field may stay unset), and
the moment any strategy in the group is ``mode: live``, silence on any of
them is refused rather than treated as permissive. This is the same
philosophy :class:`~common.config.models.StrategyConfig`'s own docstring
already states for the pre-existing fields — these tests just extend it to
the Phase 10 additions.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from common.config.models import (
    AccountRiskConfig,
    ExecutionMode,
    GlobalConfig,
    LiveOrderRateLimitConfig,
    LivePreflightConfig,
    RateLimitCallClass,
    RateLimitRule,
    ResolvedConfig,
    RuntimeConfig,
    StrategyConfig,
)


def _complete_live_preflight() -> LivePreflightConfig:
    """A fully-populated live-preflight block — every required field set."""
    return LivePreflightConfig(
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
    mode: ExecutionMode,
    live_quantity_lots: int | None = None,
    live_preflight: LivePreflightConfig | None = None,
) -> ResolvedConfig:
    return ResolvedConfig(
        global_config=GlobalConfig(),
        runtime=RuntimeConfig(
            runtime_id="intraday_options",
            enabled=True,
            live_execution_allowed=True,
            live_preflight=live_preflight if live_preflight is not None else LivePreflightConfig(),
        ),
        strategy=StrategyConfig(
            strategy_id="st01",
            enabled=True,
            mode=mode,
            live_approved=True,
            live_quantity_lots=live_quantity_lots,
        ),
    )


# --------------------------------------------------------- live_quantity_lots
def test_a_paper_strategy_needs_no_live_quantity_lots():
    cfg = _resolved(mode=ExecutionMode.PAPER)
    assert cfg.strategy.live_quantity_lots is None


def test_live_mode_without_live_quantity_lots_is_refused():
    with pytest.raises(ValidationError, match="live_quantity_lots"):
        _resolved(mode=ExecutionMode.LIVE, live_preflight=_complete_live_preflight())


def test_live_quantity_lots_must_be_positive():
    with pytest.raises(ValidationError):
        StrategyConfig(strategy_id="st01", mode=ExecutionMode.LIVE, live_quantity_lots=0)


def test_live_quantity_lots_is_a_field_distinct_from_paper_sizing():
    """The live cap lives on StrategyConfig itself, never inside the
    untyped ``parameters`` bag paper sizing uses — so a live strategy's
    quantity can never silently inherit ``parameters.strategy_kwargs
    .lots_per_trade`` by sharing a key name."""
    cfg = _resolved(
        mode=ExecutionMode.LIVE,
        live_quantity_lots=1,
        live_preflight=_complete_live_preflight(),
    )
    assert cfg.strategy.live_quantity_lots == 1
    assert "live_quantity_lots" not in cfg.strategy.parameters


# --------------------------------------------------- live preflight contract
def test_a_paper_runtime_group_needs_no_live_preflight_config():
    cfg = _resolved(mode=ExecutionMode.PAPER)
    assert cfg.runtime.live_preflight.expected_static_ip is None


@pytest.mark.parametrize(
    "make_preflight",
    [
        lambda base: base.model_copy(update={"expected_static_ip": None}),
        lambda base: base.model_copy(update={"egress_ip_provider": None}),
        lambda base: base.model_copy(update={"max_preflight_age_seconds": None}),
        lambda base: base.model_copy(update={"rate_limits": LiveOrderRateLimitConfig()}),
        lambda base: base.model_copy(update={"account_risk": AccountRiskConfig()}),
    ],
)
def test_live_mode_with_any_missing_preflight_field_is_refused(make_preflight):
    incomplete = make_preflight(_complete_live_preflight())
    with pytest.raises(ValidationError, match="missing required live configuration"):
        _resolved(mode=ExecutionMode.LIVE, live_quantity_lots=1, live_preflight=incomplete)


def test_live_mode_with_every_preflight_field_present_is_accepted():
    cfg = _resolved(
        mode=ExecutionMode.LIVE,
        live_quantity_lots=1,
        live_preflight=_complete_live_preflight(),
    )
    assert cfg.runtime.live_preflight.expected_static_ip == "203.0.113.10"


# ------------------------------------------------------- rate limit strictness
def test_rate_limit_config_rejects_unknown_call_class():
    with pytest.raises(ValidationError):
        RateLimitRule(call_class="not_a_real_class", limit=1, window_seconds=1)  # type: ignore[arg-type]


def test_rate_limit_config_rejects_non_positive_limit():
    with pytest.raises(ValidationError):
        RateLimitRule(call_class=RateLimitCallClass.NEW_ORDER, limit=0, window_seconds=60)


def test_rate_limit_config_rejects_non_positive_window():
    with pytest.raises(ValidationError):
        RateLimitRule(call_class=RateLimitCallClass.NEW_ORDER, limit=5, window_seconds=0)


def test_rate_limit_config_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        LiveOrderRateLimitConfig(rules=(), extra_field=True)  # type: ignore[call-arg]


def test_an_unconfigured_call_class_has_no_rule_at_all():
    """Empty rules means every call class has zero permits — not unlimited.
    The rate limiter itself (Phase 10 broker layer) is what enforces "no
    rule = no permit"; this test only pins the config-level contract that
    makes that enforceable: there is nothing here to interpret as
    "unlimited"."""
    cfg = LiveOrderRateLimitConfig()
    assert cfg.rules == ()


# ----------------------------------------------------------- account risk
def test_account_risk_config_rejects_non_positive_limits():
    with pytest.raises(ValidationError):
        AccountRiskConfig(max_daily_loss=0)


def test_account_risk_config_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        AccountRiskConfig(max_daily_loss=100.0, made_up_field=1)  # type: ignore[call-arg]


# --------------------------------------------------------- display name alias
def test_live_account_display_name_is_purely_informational():
    """Setting it must not affect gate/validator behaviour at all — it is
    never read as a partition key anywhere in this module."""
    cfg = _resolved(
        mode=ExecutionMode.LIVE,
        live_quantity_lots=1,
        live_preflight=_complete_live_preflight(),
    )
    cfg = cfg.model_copy(
        update={"global_config": GlobalConfig(live_account_display_name="dhan_primary")}
    )
    assert cfg.global_config.live_account_display_name == "dhan_primary"
