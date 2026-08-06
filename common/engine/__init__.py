"""The ported single-leg trading engine and the collaborators it was written against.

Phase 3 Part 2b-i, completed through Part 2b-ii-B-2. See :mod:`common.engine.engine`
for the orchestration itself; the hub tick channel (:mod:`common.engine.hub_feed`),
the ``OrderLifecycle``-backed gateway (:mod:`common.engine.gateway`) and the worker
wiring (:mod:`runtimes.intraday_options.engine_worker`) all landed in Part 2b-ii.

:mod:`common.engine.reporting_bindings` is deliberately **not** re-exported here: it
needs ``HealthState`` at runtime, and pulling ``common.execution`` into every import
of this package would undo the ``TYPE_CHECKING``-only discipline ``gateway.py`` and
``square_off.py`` keep on purpose. Import it directly.

``MultiLegEngine`` and ``FixedStrikeEngine`` are deliberately **not** here. The
spec schedules each for "when the first consumer is scheduled", and there is none.
"""

from __future__ import annotations

from .config import EngineConfig, SessionConfig
from .daily_guard import DailyRiskConfig, DailyRiskGuard, DailyRiskRecovery, DailyRiskState
from .engine import TradingEngine
from .feed import MarketDataFeed, MarketDataStatus, SimulatedFeed
from .gateway import GatewayExecutionError, LifecycleGateway
from .hub_feed import HubTickFeed
from .models import (
    AdoptedPosition,
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
from .state_payload import (
    DAY_SUMMARY_KEY,
    OPEN_POSITION_KEY,
    merge_payload,
    read_payload,
)
from .strategy import BaseStrategy, available_strategies, get_strategy, register_strategy

__all__ = [
    "DAY_SUMMARY_KEY",
    "OPEN_POSITION_KEY",
    "AdoptedPosition",
    "BaseStrategy",
    "DailyRiskConfig",
    "DailyRiskGuard",
    "DailyRiskRecovery",
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
    "merge_payload",
    "opt_float",
    "read_payload",
    "register_risk_manager",
    "register_strategy",
    "resolve_strike",
    "summarise",
]
