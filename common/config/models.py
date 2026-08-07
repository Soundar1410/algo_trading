"""Typed configuration models and the live-execution safety gate.

Execution mode belongs to the **individual strategy** (spec section 3). Global
and runtime flags are *permissions*: they can block live execution, but they can
never convert a live strategy into a paper one. There is deliberately no
``mode: disabled`` — a strategy is turned off with ``enabled: false``, so there
is exactly one way to disable something.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from common.risk.squareoff import ExpiryPolicy


class ExecutionMode(StrEnum):
    """How one strategy's orders are executed. Immutable for a worker session."""

    PAPER = "paper"
    LIVE = "live"


class EngineKind(StrEnum):
    """Which preserved custom engine a strategy runs on.

    These stay separate on purpose: single-leg, basket and fixed-strike
    strategies have genuinely different candle, position and risk models
    (spec section 7). They are not to be merged into one generic engine.
    """

    TRADING_ENGINE = "trading_engine"
    MULTI_LEG_ENGINE = "multi_leg_engine"
    FIXED_STRIKE_ENGINE = "fixed_strike_engine"
    STOCK_PORTFOLIO_ENGINE = "stock_portfolio_engine"


class _StrictModel(BaseModel):
    """Reject unknown keys everywhere.

    A silently-ignored typo in a risk limit or a live-approval flag is a safety
    problem, not a convenience. Fail at load time instead.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class GlobalConfig(_StrictModel):
    """Account-wide settings. The live kill switch defaults to off."""

    live_trading_enabled: bool = False
    timezone: str = "Asia/Kolkata"


class HealthConfig(_StrictModel):
    """Operational tuning for one runtime group's health reporting.

    Spec line 2482: "every 5 to 15 seconds is enough, and the interval is
    configurable." Before Phase 7 Part 1 this was a constructor default
    (``common.health.heartbeat.DEFAULT_INTERVAL_SECONDS``) that both the
    supervisor and every worker silently accepted — nothing in config ever
    reached it, so "configurable" was true of the code but not of the system.
    """

    heartbeat_interval_seconds: float = Field(default=10.0, gt=0)


class RuntimeConfig(_StrictModel):
    """One strategy group: lifecycle and live permission, never a shared mode."""

    runtime_id: str
    enabled: bool = False
    live_execution_allowed: bool = False
    shared_market_feed: bool = True
    database: str | None = None
    health: HealthConfig = Field(default_factory=HealthConfig)

    @field_validator("runtime_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("runtime_id must not be empty")
        return v


class StrategyConfig(_StrictModel):
    """One strategy instance: its own mode, approval, engine and parameters.

    ``expiry_policy``/``square_off_before_expiry_days`` are typed top-level
    fields rather than ``risk`` keys (Phase 6 Part 4, spec section 11):
    ``risk`` is an untyped ``dict[str, Any]``, so ``_StrictModel``'s
    ``extra="forbid"`` cannot catch a typo inside it — exactly the silent-typo
    failure this class's own docstring warns about for the top-level fields.
    """

    strategy_id: str
    enabled: bool = False
    mode: ExecutionMode = ExecutionMode.PAPER
    live_approved: bool = False
    engine: EngineKind = EngineKind.TRADING_ENGINE
    risk: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    expiry_policy: ExpiryPolicy = ExpiryPolicy.FORCE_SQUARE_OFF_BEFORE_EXPIRY
    square_off_before_expiry_days: int = Field(default=0, ge=0)

    @field_validator("strategy_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("strategy_id must not be empty")
        return v

    @field_validator("expiry_policy")
    @classmethod
    def _no_settlement_simulation_yet(cls, v: ExpiryPolicy) -> ExpiryPolicy:
        if v is ExpiryPolicy.SIMULATE_EXCHANGE_SETTLEMENT:
            raise ValueError(
                "expiry_policy: simulate_exchange_settlement is refused: exchange-"
                "settlement simulation is not implemented. Spec section 11 requires "
                "a versioned settlement policy covering the expiry calendar and "
                "last-trading-day handling, final settlement price capture, ITM/OTM "
                "determination, index-option cash settlement, exercise/assignment "
                "event recording, effective-dated exercise STT and other charges, "
                "T+1 settlement timing, and stock-option physical-settlement "
                "obligations/delivery margin/assignment risk — and states this value "
                "may be used only after settlement tests pass. Until then "
                "force_square_off_before_expiry is the only permitted value; stock "
                "options must remain force-square-off regardless."
            )
        return v


class ResolvedConfig(_StrictModel):
    """One strategy's fully layered configuration, as a worker sees it."""

    global_config: GlobalConfig
    runtime: RuntimeConfig
    strategy: StrategyConfig


# --------------------------------------------------------------- live gate
@dataclass(frozen=True)
class LiveGateDecision:
    """Outcome of the live-execution gate.

    ``allowed`` is only ever True when every condition in the spec's AND-chain
    holds. When it is False the caller must **refuse to start that strategy** —
    it must not fall back to :class:`~common.broker.paper` execution. Silently
    demoting a live strategy to paper would make the operator believe real
    orders are being placed when they are not.
    """

    allowed: bool
    blocked_reasons: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.allowed


#: Phase 0 has no authentication, no broker connectivity check, no static-IP
#: preflight and no reconciliation — so the preflight input below can only ever
#: be False in this phase. It becomes a real check in Phase 10.
PREFLIGHT_NOT_IMPLEMENTED = (
    "live preflight checks are not implemented (deferred to Phase 10: token "
    "validity, broker connectivity, static IP, reconciliation, locks)"
)


def effective_live_gate(
    cfg: ResolvedConfig,
    *,
    preflight_passed: bool = False,
) -> LiveGateDecision:
    """Decide whether one strategy may route orders to the live broker.

    Implements the spec's AND-chain. Every failing condition is reported, not
    just the first, so an operator fixes one config in one pass instead of
    rediscovering the next blocker on each restart.

    Args:
        cfg: the strategy's fully resolved configuration.
        preflight_passed: whether live preflight checks have run *and* passed.
            Defaults to False, so the gate is fail-closed by construction: a
            caller that forgets to run preflight gets a block, not an allow.

    Returns:
        A :class:`LiveGateDecision`. ``allowed=False`` means "do not run this
        strategy", never "run it in paper mode instead".
    """
    reasons: list[str] = []

    if cfg.strategy.mode is not ExecutionMode.LIVE:
        # Not a live strategy at all. Not an error, but not a live allowance.
        return LiveGateDecision(False, (f"strategy mode is {cfg.strategy.mode.value}",))

    if not cfg.global_config.live_trading_enabled:
        reasons.append("global.live_trading_enabled is false")
    if not cfg.runtime.enabled:
        reasons.append(f"runtime {cfg.runtime.runtime_id!r} is not enabled")
    if not cfg.runtime.live_execution_allowed:
        reasons.append(f"runtime {cfg.runtime.runtime_id!r} does not allow live execution")
    if not cfg.strategy.enabled:
        reasons.append(f"strategy {cfg.strategy.strategy_id!r} is not enabled")
    if not cfg.strategy.live_approved:
        reasons.append(f"strategy {cfg.strategy.strategy_id!r} is not live_approved")
    if not preflight_passed:
        reasons.append(PREFLIGHT_NOT_IMPLEMENTED)

    if reasons:
        return LiveGateDecision(False, tuple(reasons))
    return LiveGateDecision(True)
