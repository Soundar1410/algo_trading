"""Broker routing: paper works, live refuses — and never falls back.

This is the D5 deviation under test. The reference repository's factory builds a
live broker from ``mode: live`` alone; this one must consult the live gate and
refuse. The most important assertion in this file is the *negative* one: a
blocked live strategy must not receive a PaperBroker.
"""

from __future__ import annotations

import pytest

from common.broker import (
    DhanLiveBroker,
    LiveBrokerDependencies,
    LiveExecutionBlocked,
    PaperBroker,
    build_broker,
)
from common.broker.paper import InstrumentRules
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

#: Phase 10: a mode: live StrategyConfig requires its own live_quantity_lots
#: and a complete runtime.live_preflight block (ResolvedConfig's own
#: validator) purely to construct — unrelated to what most tests in this
#: file check (the pre-existing global/runtime/strategy permission gate), so
#: supplied unconditionally here.
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


class _FakeDhanOrderClient:
    """The minimal DhanOrderClient double this file needs — never a real
    dhanhq object, never a network call."""

    def get_order_list(self):  # type: ignore[no-untyped-def]
        return {"status": "success", "data": []}


class _AllowingGuard:
    def before_call(self, call_class: RateLimitCallClass, *, risk_reducing: bool = False) -> None:
        del call_class, risk_reducing


def _config(
    *,
    mode: ExecutionMode,
    live_trading_enabled: bool = False,
    runtime_enabled: bool = True,
    live_execution_allowed: bool = False,
    strategy_enabled: bool = True,
    live_approved: bool = False,
) -> ResolvedConfig:
    return ResolvedConfig(
        global_config=GlobalConfig(live_trading_enabled=live_trading_enabled),
        runtime=RuntimeConfig(
            runtime_id="intraday_options",
            enabled=runtime_enabled,
            live_execution_allowed=live_execution_allowed,
            live_preflight=(
                _COMPLETE_LIVE_PREFLIGHT if mode is ExecutionMode.LIVE else LivePreflightConfig()
            ),
        ),
        strategy=StrategyConfig(
            strategy_id="st01",
            runtime_id="intraday_options",
            enabled=strategy_enabled,
            mode=mode,
            live_approved=live_approved,
            live_quantity_lots=1 if mode is ExecutionMode.LIVE else None,
        ),
    )


def test_a_paper_strategy_gets_a_paper_broker():
    broker = build_broker(_config(mode=ExecutionMode.PAPER))
    assert isinstance(broker, PaperBroker)
    assert broker.name == "paper"


def test_a_blocked_live_strategy_raises():
    with pytest.raises(LiveExecutionBlocked):
        build_broker(_config(mode=ExecutionMode.LIVE))


def test_a_blocked_live_strategy_is_never_rerouted_to_paper():
    """The single most important assertion in this file.

    Silently demoting a live strategy would leave the operator believing real
    orders are being placed. The strategy must refuse to start instead.
    """
    with pytest.raises(LiveExecutionBlocked) as caught:
        build_broker(_config(mode=ExecutionMode.LIVE))

    assert "NOT rerouted to paper" in str(caught.value)


def test_the_refusal_names_every_failing_condition():
    with pytest.raises(LiveExecutionBlocked) as caught:
        build_broker(_config(mode=ExecutionMode.LIVE))

    message = str(caught.value)
    assert "global.live_trading_enabled is false" in message
    assert "does not allow live execution" in message
    assert "not live_approved" in message


def test_a_fully_approved_live_strategy_without_live_dependencies_still_cannot_trade():
    """Phase 10: even with every gate open, a passing gate alone is not
    enough — build_broker must never guess a Dhan client, exchange segment
    or product type. This is a deliberate Phase 10 change from the earlier
    (Phase 1-9) behaviour, where a passing gate always raised unconditionally
    because DhanLiveBroker did not exist at all yet; the assertion below
    updates to match the real refusal reason now that it does."""
    with pytest.raises(LiveExecutionBlocked, match="no LiveBrokerDependencies"):
        build_broker(
            _config(
                mode=ExecutionMode.LIVE,
                live_trading_enabled=True,
                live_execution_allowed=True,
                live_approved=True,
            ),
            preflight_passed=True,
        )


def test_a_fully_approved_live_strategy_now_gets_a_dhan_live_broker():
    """Requirement #2: all gates true plus valid preflight (and now, the
    live-construction dependencies a worker supplies) selects live
    execution — the strategy-wise routing the mixed-mode gate requires."""
    client = _FakeDhanOrderClient()
    broker = build_broker(
        _config(
            mode=ExecutionMode.LIVE,
            live_trading_enabled=True,
            live_execution_allowed=True,
            live_approved=True,
        ),
        preflight_passed=True,
        live_dependencies=LiveBrokerDependencies(
            client=client,
            exchange_segment="NSE_FNO",
            product_type="INTRADAY",
            call_guard=_AllowingGuard(),
        ),
        instrument_rules=lambda security_id: InstrumentRules(lot_size=75, tick_size=0.05),
    )
    assert isinstance(broker, DhanLiveBroker)
    assert broker.name == "dhan_live"


def test_preflight_defaults_to_failed_so_a_forgetful_caller_is_blocked():
    with pytest.raises(LiveExecutionBlocked, match="preflight"):
        build_broker(
            _config(
                mode=ExecutionMode.LIVE,
                live_trading_enabled=True,
                live_execution_allowed=True,
                live_approved=True,
            )
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"live_trading_enabled": False},
        {"runtime_enabled": False},
        {"live_execution_allowed": False},
        {"strategy_enabled": False},
        {"live_approved": False},
    ],
)
def test_every_single_gate_alone_is_enough_to_block(kwargs: dict[str, bool]):
    base = {
        "live_trading_enabled": True,
        "runtime_enabled": True,
        "live_execution_allowed": True,
        "strategy_enabled": True,
        "live_approved": True,
    }
    base.update(kwargs)
    with pytest.raises(LiveExecutionBlocked):
        build_broker(_config(mode=ExecutionMode.LIVE, **base), preflight_passed=True)


def test_paper_broker_receives_its_configuration():
    broker = build_broker(
        _config(mode=ExecutionMode.PAPER),
        paper_execution={"slippage": {"options": {"mode": "ticks", "market_order_ticks": 2}}},
    )
    assert isinstance(broker, PaperBroker)
    assert broker.config.slippage.market_order_ticks == 2


def test_paper_broker_receives_the_quote_book():
    """Without it the fill model has no post-latency quote to select and no way to
    settle a resting order, so it must not be silently droppable."""
    from common.broker import QuoteBook

    quotes = QuoteBook()
    broker = build_broker(_config(mode=ExecutionMode.PAPER), quotes=quotes)
    assert isinstance(broker, PaperBroker)
    assert broker.quotes is quotes
