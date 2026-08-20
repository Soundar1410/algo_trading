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
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from common.risk.squareoff import ExpiryPolicy
from common.utils.timeutils import parse_hhmm


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
    #: The cross-session sibling of MULTI_LEG_ENGINE (spec: weekly_delta_neutral)
    #: — common.engine.positional.positional_engine.PositionalMultiLegEngine.
    POSITIONAL_MULTI_LEG_ENGINE = "positional_multi_leg_engine"


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
    #: Human-readable label only (e.g. for dashboards). Phase 10: deliberately
    #: NOT the partition key for any live-risk/rate-limit table — an operator
    #: typo here must never be able to split one real Dhan account's limits
    #: into two independently-enforced buckets. The authoritative partition
    #: key is derived from the authenticated broker account itself; see
    #: common.broker.live_preflight.check_account_identity.
    live_account_display_name: str | None = None


class AutoStartConfig(_StrictModel):
    """Unattended pre-market startup (``auto_start:`` in ``global.yaml``).

    Account-wide rather than per-runtime: one controller establishes the
    trading-day/auth ordering once, then starts *whichever* runtimes are
    enabled. Deliberately lists **no strategy or runtime ids** — runtime and
    strategy YAML ``enabled:`` flags stay the only authority on what runs, so
    this block can never enable something the safety config disabled.

    ``enabled`` is false in committed config and stays that way: installing the
    LaunchAgent and flipping this are two separate, deliberate operator steps.
    """

    enabled: bool = False
    timezone: str = "Asia/Kolkata"
    #: ``StartCalendarInterval`` fires in the Mac's *local* zone, so a Mac that
    #: is not on :attr:`timezone` fires at the wrong instant. Every decision
    #: here is made in :attr:`timezone` regardless, but a mismatched Mac can
    #: still fire hours early or late — so the default is to refuse rather than
    #: trade on an unverifiable clock. See ``orchestration.auto_start.gate``.
    require_system_timezone_match: bool = True
    #: Earliest eligible start. A login before this exits 0 and defers to the
    #: scheduled trigger; it never starts trading early.
    startup_time: str = "09:00"
    #: Latest eligible start for a *new* automatic session. Defaults to the
    #: session deadline on purpose: a positional runtime with an open cycle
    #: must still start at 11:00 to recover and manage that exposure. Whether a
    #: new *entry* is allowed is decided by the strategy/engine entry gates
    #: (``MarketSession.can_enter``, ``SquareOffPolicy.entry_cutoff``), never by
    #: refusing to start the runtime at all. An operator may narrow this.
    latest_start_time: str = "15:15"
    #: Hard stop for the whole attempt, including every retry.
    session_deadline_time: str = "15:15"
    retry_interval_seconds: float = Field(default=30.0, gt=0)
    retry_backoff_multiplier: float = Field(default=1.5, ge=1.0)
    retry_max_interval_seconds: float = Field(default=300.0, gt=0)
    #: How long a spawned runtime has to take its own supervisor lock before
    #: the launch is reported as a failed startup handshake.
    runtime_handshake_seconds: float = Field(default=120.0, gt=0)
    #: Grace period a child gets after SIGTERM before SIGKILL.
    shutdown_grace_seconds: float = Field(default=30.0, gt=0)
    #: The dashboard is started by its own RunAtLoad LaunchAgent, never by the
    #: trading controller — one owner. This flag is read by
    #: ``scripts.start_dashboard``.
    dashboard_auto_start: bool = True
    telegram_retry_attempts: int = Field(default=3, gt=0)
    telegram_retry_delay_seconds: float = Field(default=5.0, gt=0)
    #: ISO dates, fed straight to ``common.engine.config.SessionConfig`` so the
    #: weekend/holiday rules are ``MarketSession``'s, not a second copy.
    holidays: tuple[str, ...] = ()

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as exc:
            raise ValueError(f"Unknown IANA timezone {v!r}") from exc
        return v

    @field_validator("startup_time", "latest_start_time", "session_deadline_time")
    @classmethod
    def _valid_hhmm(cls, v: str) -> str:
        parse_hhmm(v)  # raises ValueError on anything unparseable
        return v

    @model_validator(mode="after")
    def _window_is_ordered(self) -> AutoStartConfig:
        start = parse_hhmm(self.startup_time)
        latest = parse_hhmm(self.latest_start_time)
        deadline = parse_hhmm(self.session_deadline_time)
        if not (start <= latest <= deadline):
            raise ValueError(
                "auto_start times must satisfy "
                "startup_time <= latest_start_time <= session_deadline_time "
                f"(got {self.startup_time}, {self.latest_start_time}, "
                f"{self.session_deadline_time})."
            )
        if self.retry_max_interval_seconds < self.retry_interval_seconds:
            raise ValueError(
                "auto_start.retry_max_interval_seconds must be >= retry_interval_seconds "
                f"(got {self.retry_max_interval_seconds} < {self.retry_interval_seconds})."
            )
        return self

    @property
    def holiday_dates(self) -> tuple[str, ...]:
        """The holiday calendar, as ``SessionConfig`` wants it."""
        return self.holidays


class HealthConfig(_StrictModel):
    """Operational tuning for one runtime group's health reporting.

    Spec line 2482: "every 5 to 15 seconds is enough, and the interval is
    configurable." Before Phase 7 Part 1 this was a constructor default
    (``common.health.heartbeat.DEFAULT_INTERVAL_SECONDS``) that both the
    supervisor and every worker silently accepted — nothing in config ever
    reached it, so "configurable" was true of the code but not of the system.
    """

    heartbeat_interval_seconds: float = Field(default=10.0, gt=0)


class RetentionConfig(_StrictModel):
    """Storage-growth bounds for one runtime group (Phase 7 Part 5, spec
    section 12: "Do not allow storage growth to remain unbounded").

    Applied once per controlled startup by
    :func:`common.retention.run_retention` (plus
    :func:`common.retention.backup_database`, called separately and earlier,
    before migration). Every default below must match
    ``common.retention.policy``'s constant of the same name — the same
    discipline :class:`HealthConfig` already follows for its heartbeat
    default against ``common.health.heartbeat.DEFAULT_INTERVAL_SECONDS``; see
    ``tests/unit/test_config_loader.py``'s check of the other direction.
    """

    log_max_age_days: int = Field(default=30, gt=0)
    log_compress_after_days: int = Field(default=1, gt=0)
    db_row_max_age_days: int = Field(default=90, gt=0)
    db_delete_batch_limit: int = Field(default=5000, gt=0)
    backup_retain_count: int = Field(default=7, gt=0)
    scrip_cache_retain_count: int = Field(default=3, gt=0)


class RateLimitCallClass(StrEnum):
    """Dhan order-endpoint call categories (spec section 14): a shared limiter
    must distinguish these rather than throttling everything identically."""

    NEW_ORDER = "new_order"
    MODIFY = "modify"
    CANCEL = "cancel"
    READ = "read"


class RateLimitRule(_StrictModel):
    """One call class's limit within one rolling window.

    No default is set here deliberately (see ``LiveOrderRateLimitConfig``'s
    own docstring: an unconfigured call class has zero permits, not
    unlimited) — but a recommended, cited value exists for an operator
    configuring ``new_order`` for real:

    **Dhan's own current documented Order API limit is 10 requests/second**
    (``https://dhanhq.co/docs/v2/releases/``, Version 2.3, 08 Sep 2025:
    "changing rate limit for Order APIs to 10 order per second, in
    accordance with regulations"; cross-confirmed by
    ``https://dhan.co/support/platforms/dhanhq-api/what-are-the-api-rate-
    limits-for-dhan/``: "Order APIs: Up to 10 requests per second"). The
    older `dhanhq.co/docs/v1` support article's "25 requests/second" is
    superseded by this regulatory change and must not be used.

    The recommended configured value is ``limit=5, window_seconds=1`` — a
    50% margin below Dhan's documented 10/second, sized specifically
    against this limiter's own fixed-window mechanic
    (``common.broker.live_rate_limiter``, ``_floor_to_window``): a
    fixed-window limiter can legally admit up to ``limit`` requests at the
    tail of one window and another ``limit`` at the head of the next, so
    the true worst-case burst near a window boundary is ``2 x limit``
    within a short real span. At ``limit=5`` that worst case is exactly 10
    — Dhan's own documented ceiling, not over it — with nothing left over
    to also absorb clock skew between this process's ``datetime.now()`` and
    Dhan's own request-timestamp enforcement. A larger ``window_seconds``
    at a proportionally larger ``limit`` (e.g. ``limit=300,
    window_seconds=60`` for the same 5/s average) does **not** carry the
    same guarantee — it widens the real-time span the ``2 x limit`` worst
    case can occur within, which is why this recommendation keeps
    ``window_seconds=1`` matching Dhan's own per-second granularity exactly.
    """

    call_class: RateLimitCallClass
    limit: int = Field(gt=0)
    window_seconds: int = Field(gt=0)


class LiveOrderRateLimitConfig(_StrictModel):
    """Account-wide, cross-process live order-call limits (spec section 14).

    Deliberately a strict, closed-vocabulary model rather than a loose
    ``dict[str, tuple[int, int]]``: a free-form mapping cannot reject an
    unknown call class, a non-positive limit or a non-positive window at load
    time, and a financial rate limit is exactly the kind of value that must
    fail loudly on a typo rather than silently accept it.

    An unconfigured call class has **zero** permits — not unlimited. Empty
    ``rules`` therefore blocks every live order/modify/cancel/read call until
    the operator explicitly configures a limit for it.
    """

    rules: tuple[RateLimitRule, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _one_rule_per_call_class(self) -> LiveOrderRateLimitConfig:
        call_classes = [rule.call_class for rule in self.rules]
        if len(call_classes) != len(set(call_classes)):
            raise ValueError("rate_limits.rules contains a duplicate call_class")
        return self


class AccountRiskConfig(_StrictModel):
    """Account-wide live-risk ceilings (spec section 13, account-level controls).

    Every field is optional at the type level but required in substance
    whenever any strategy in the group is live — enforced by
    :class:`ResolvedConfig`'s cross-field validator below, not by a default
    here, so "forgot to set a limit" fails closed instead of silently
    permitting unlimited exposure.
    """

    max_daily_loss: float | None = Field(default=None, gt=0)
    max_open_positions: int | None = Field(default=None, gt=0)
    max_open_legs: int | None = Field(default=None, gt=0)
    max_deployed_capital: float | None = Field(default=None, gt=0)
    max_mtm_age_seconds: int | None = Field(default=None, gt=0)


class LivePreflightConfig(_StrictModel):
    """Every input the Phase 10 live preflight needs (spec sections 10/13/14).

    Absent by default (every field ``None``/empty) so that omitting this
    block entirely on a paper-only runtime group costs nothing, while a
    runtime group that enables even one live strategy must fill in every
    field or fail closed (:class:`ResolvedConfig`'s validator).
    """

    #: The IP Dhan is expected to have whitelisted for this account. Checked
    #: against Dhan's own registered list *and* an independently observed
    #: current egress IP — see common.broker.live_preflight.check_static_ip.
    expected_static_ip: str | None = None
    #: Named, approved egress-IP provider key. ``None`` (the shipped default)
    #: means no provider is configured, so static-IP preflight is BLOCKED
    #: rather than trusting the Dhan whitelist alone — no provider ships
    #: without separate operator approval (a new outbound dependency).
    egress_ip_provider: str | None = None
    #: How long a preflight result stays authoritative before it must be
    #: re-run. No default: a live runtime group must set this explicitly.
    max_preflight_age_seconds: int | None = Field(default=None, gt=0)
    rate_limits: LiveOrderRateLimitConfig = Field(default_factory=LiveOrderRateLimitConfig)
    account_risk: AccountRiskConfig = Field(default_factory=AccountRiskConfig)


class RuntimeConfig(_StrictModel):
    """One strategy group: lifecycle and live permission, never a shared mode."""

    runtime_id: str
    enabled: bool = False
    live_execution_allowed: bool = False
    shared_market_feed: bool = True
    database: str | None = None
    health: HealthConfig = Field(default_factory=HealthConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    live_preflight: LivePreflightConfig = Field(default_factory=LivePreflightConfig)

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
    #: Which runtime group this strategy belongs to. **Required and
    #: non-empty for every real strategy configuration** — never optional,
    #: never inherited from a defaults block, never left to a naming
    #: convention. ``common.config.loader.discover_strategies`` filters on an
    #: exact match against this field; a strategy file that omits it, leaves
    #: it blank, or names a runtime with no ``config/runtimes/<id>.yaml`` on
    #: disk fails to load rather than being silently included in or excluded
    #: from a runtime group's strategy set. Before this field existed, a
    #: second runtime group could not safely call ``discover_strategies``
    #: against the shared ``config/strategies/`` directory at all — see that
    #: function's own former docstring, now closed by this field.
    runtime_id: str
    enabled: bool = False
    mode: ExecutionMode = ExecutionMode.PAPER
    live_approved: bool = False
    engine: EngineKind = EngineKind.TRADING_ENGINE
    risk: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)
    expiry_policy: ExpiryPolicy = ExpiryPolicy.FORCE_SQUARE_OFF_BEFORE_EXPIRY
    square_off_before_expiry_days: int = Field(default=0, ge=0)
    #: The controlled-live minimum-quantity cap, in lots — deliberately a
    #: *separate* field from ``parameters.strategy_kwargs.lots_per_trade``
    #: (the paper sizing field), never read on the live path and never
    #: inherited by it. ``None`` is fine for a paper strategy; a live one
    #: must set this explicitly (validator below) so the existing 10-lot
    #: paper default can never accidentally become the initial live size.
    live_quantity_lots: int | None = Field(default=None, gt=0)

    @field_validator("strategy_id")
    @classmethod
    def _non_empty_strategy_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("strategy_id must not be empty")
        return v

    @field_validator("runtime_id")
    @classmethod
    def _non_empty_runtime_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("runtime_id must not be empty")
        return v

    @model_validator(mode="after")
    def _live_requires_its_own_quantity(self) -> StrategyConfig:
        if self.mode is ExecutionMode.LIVE and self.live_quantity_lots is None:
            raise ValueError(
                f"strategy {self.strategy_id!r} is mode: live but sets no "
                "live_quantity_lots. A live strategy must declare its own "
                "controlled-live quantity explicitly — it never inherits "
                "parameters.strategy_kwargs.lots_per_trade (paper-only sizing)."
            )
        return self

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

    @model_validator(mode="after")
    def _strategy_runtime_id_matches_the_resolved_runtime(self) -> ResolvedConfig:
        """Defense in depth alongside the loader's own check
        (``common.config.loader.load_strategy_config``): a ``ResolvedConfig``
        assembled any other way (tests, a future caller) still cannot pair a
        strategy with a runtime it does not declare itself to belong to."""
        if self.strategy.runtime_id != self.runtime.runtime_id:
            raise ValueError(
                f"strategy {self.strategy.strategy_id!r} declares runtime_id="
                f"{self.strategy.runtime_id!r}, which does not match the resolved "
                f"runtime {self.runtime.runtime_id!r}."
            )
        return self

    @model_validator(mode="after")
    def _live_requires_a_complete_preflight_contract(self) -> ResolvedConfig:
        """Every new live-only config field must be present, or fail closed.

        A runtime group that never enables a live strategy pays nothing for
        this (every ``LivePreflightConfig`` field may stay ``None``/empty).
        The moment any strategy in the group is ``mode: live``, silence on
        any of these is refused rather than treated as "unlimited" or
        "skip this check" — spec 2393: "No strategy should infer a
        permissive default when required risk configuration is missing."
        """
        if self.strategy.mode is not ExecutionMode.LIVE:
            return self

        preflight = self.runtime.live_preflight
        missing: list[str] = []
        if not preflight.expected_static_ip:
            missing.append("runtime.live_preflight.expected_static_ip")
        if not preflight.egress_ip_provider:
            missing.append("runtime.live_preflight.egress_ip_provider")
        if preflight.max_preflight_age_seconds is None:
            missing.append("runtime.live_preflight.max_preflight_age_seconds")
        configured_call_classes = {
            rule.call_class for rule in preflight.rate_limits.rules
        }
        missing_call_classes = set(RateLimitCallClass) - configured_call_classes
        if missing_call_classes:
            missing.append(
                "runtime.live_preflight.rate_limits.rules missing "
                + ", ".join(sorted(item.value for item in missing_call_classes))
            )
        account_risk = preflight.account_risk
        for field in (
            "max_daily_loss",
            "max_open_positions",
            "max_open_legs",
            "max_deployed_capital",
            "max_mtm_age_seconds",
        ):
            if getattr(account_risk, field) is None:
                missing.append(f"runtime.live_preflight.account_risk.{field}")

        if missing:
            raise ValueError(
                f"strategy {self.strategy.strategy_id!r} is mode: live but "
                f"runtime {self.runtime.runtime_id!r} is missing required live "
                f"configuration: {', '.join(missing)}. Absence is refused, not "
                "treated as unlimited or skipped."
            )
        return self


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


PREFLIGHT_NOT_PASSED = (
    "live preflight did not pass (token/account/static-IP/database/"
    "connectivity/confirmation checks)"
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
        reasons.append(PREFLIGHT_NOT_PASSED)

    if reasons:
        return LiveGateDecision(False, tuple(reasons))
    return LiveGateDecision(True)
