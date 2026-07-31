"""The ported single-leg trading engine and the collaborators it was written against.

Phase 3 Part 2b-i. See :mod:`common.engine.engine` for the orchestration itself and
the runbook (section 8) for what Part 2b-ii still owes: the hub tick channel, the
worker wiring and the ``OrderLifecycle``-backed execution gateway.

``MultiLegEngine`` and ``FixedStrikeEngine`` are deliberately **not** here. The
spec schedules each for "when the first consumer is scheduled", and there is none.
"""

from __future__ import annotations

from .config import EngineConfig, SessionConfig
from .daily_guard import DailyRiskConfig, DailyRiskGuard, DailyRiskState
from .engine import TradingEngine
from .feed import MarketDataFeed, MarketDataStatus, SimulatedFeed
from .gateway import GatewayExecutionError, LifecycleGateway
from .hub_feed import HubTickFeed
from .models import (
    ExitReason,
    Moneyness,
    OpenPosition,
    OptionContract,
    OptionSelection,
    OptionType,
    OrderSide,
    SignalAction,
    StrategySignal,
    Trade,
)
from .positions import ExecutionGateway, FillOutcome, InMemoryGateway, PositionManager
from .regime import RegimeLabel, RegimeTagger, SessionTag, build_regime_tagger
from .reporting import DailySummary, EngineReporter, NullReporter, summarise
from .risk import RiskManager, opt_float, register_risk_manager
from .selection import (
    OptionChainResolver,
    OptionSelector,
    SimulatedOptionChainResolver,
    resolve_strike,
)
from .session import MarketSession
from .square_off import (
    PersistedSquareOffAuthority,
    SessionSquareOffAuthority,
    SquareOffAuthority,
)
from .strategy import BaseStrategy, available_strategies, get_strategy, register_strategy

__all__ = [
    "BaseStrategy",
    "DailyRiskConfig",
    "DailyRiskGuard",
    "DailyRiskState",
    "DailySummary",
    "EngineConfig",
    "EngineReporter",
    "ExecutionGateway",
    "ExitReason",
    "FillOutcome",
    "GatewayExecutionError",
    "HubTickFeed",
    "InMemoryGateway",
    "LifecycleGateway",
    "MarketDataFeed",
    "MarketDataStatus",
    "MarketSession",
    "Moneyness",
    "NullReporter",
    "OpenPosition",
    "OptionChainResolver",
    "OptionContract",
    "OptionSelection",
    "OptionSelector",
    "OptionType",
    "OrderSide",
    "PersistedSquareOffAuthority",
    "PositionManager",
    "RegimeLabel",
    "RegimeTagger",
    "RiskManager",
    "SessionConfig",
    "SessionSquareOffAuthority",
    "SessionTag",
    "SignalAction",
    "SimulatedFeed",
    "SimulatedOptionChainResolver",
    "SquareOffAuthority",
    "StrategySignal",
    "Trade",
    "TradingEngine",
    "available_strategies",
    "build_regime_tagger",
    "get_strategy",
    "opt_float",
    "register_risk_manager",
    "register_strategy",
    "resolve_strike",
    "summarise",
]
