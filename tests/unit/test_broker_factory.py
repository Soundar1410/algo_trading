"""Broker routing: paper works, live refuses — and never falls back.

This is the D5 deviation under test. The reference repository's factory builds a
live broker from ``mode: live`` alone; this one must consult the live gate and
refuse. The most important assertion in this file is the *negative* one: a
blocked live strategy must not receive a PaperBroker.
"""

from __future__ import annotations

import pytest

from common.broker import LiveExecutionBlocked, PaperBroker, build_broker
from common.config.models import (
    ExecutionMode,
    GlobalConfig,
    ResolvedConfig,
    RuntimeConfig,
    StrategyConfig,
)


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
        ),
        strategy=StrategyConfig(
            strategy_id="st01",
            enabled=strategy_enabled,
            mode=mode,
            live_approved=live_approved,
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


def test_a_fully_approved_live_strategy_still_cannot_trade_in_phase_1():
    """Even with every gate open, live placement does not exist yet."""
    with pytest.raises(LiveExecutionBlocked, match="Phase 10"):
        build_broker(
            _config(
                mode=ExecutionMode.LIVE,
                live_trading_enabled=True,
                live_execution_allowed=True,
                live_approved=True,
            ),
            preflight_passed=True,
        )


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
