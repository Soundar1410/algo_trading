"""What the engine reads from configuration — and nothing else.

The reference engine takes an ``AppConfig`` (``framework/config/schema.py``, 294
lines) and reads exactly six things out of it: the session block, the strategy
timeframe, the trading mode, ``risk.max_daily_loss_percent``,
``paper_trading.starting_capital`` and the regime block. Porting that whole schema
would give this repository a second configuration system beside
:mod:`common.config.models`, with two places to change a default and two answers
to "is live enabled?" — and deviation **D1** already rules out porting the
reference loader for exactly that class of reason.

So the engine takes an :class:`EngineConfig` instead: a plain dataclass holding
those six values, built from this repository's :class:`~common.config.models.
ResolvedConfig` by :meth:`EngineConfig.from_resolved`. The engine's own reads are
unchanged in substance — ``self.cfg.strategy.timeframe`` becomes
``self.cfg.timeframe`` — and the live gate stays where Phase 1 put it, consulted
by the broker factory rather than re-derived here (deviation D19).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from common.config.models import ExecutionMode, ResolvedConfig
from common.risk import SquareOffPolicy

#: The reference's default session, matching NSE equity-derivatives hours. Kept as
#: defaults rather than constants so a config can move them without code changes.
DEFAULT_START_TIME = "09:15"
DEFAULT_END_TIME = "15:15"
DEFAULT_SQUARE_OFF_TIME = "15:20"
DEFAULT_TIMEZONE = "Asia/Kolkata"


@dataclass(frozen=True)
class SessionConfig:
    """Session boundaries for one strategy. Ported field-for-field.

    ``end_time`` is when new entries stop; ``square_off_time`` is the hard
    force-close. They are separate so a strategy cannot open a position that the
    square-off immediately closes — the same rule
    :class:`~common.risk.squareoff.SquareOffPolicy` encodes for the worker.

    Prefer :meth:`from_square_off_policy` over setting those two by hand. Until
    Part 2b-ii-B-1 they were configured independently of the policy's own
    ``entry_cutoff``/``square_off_at``, and the two sets of defaults had already
    drifted fifteen minutes apart (15:15/15:20 here against 15:00/15:15 there).
    """

    timezone: str = DEFAULT_TIMEZONE
    start_time: str = DEFAULT_START_TIME
    end_time: str = DEFAULT_END_TIME
    square_off_time: str = DEFAULT_SQUARE_OFF_TIME
    holidays: tuple[str, ...] = ()

    @classmethod
    def from_square_off_policy(
        cls,
        policy: SquareOffPolicy,
        *,
        start_time: str = DEFAULT_START_TIME,
        holidays: tuple[str, ...] = (),
    ) -> SessionConfig:
        """Derive the session boundaries from the policy that owns square-off.

        One configured pair, two derived strings: the engine's entry window closes
        at the policy's ``entry_cutoff`` and its active window ends at the policy's
        ``square_off_at``, so there is no second place to change a time and no way
        for the two to disagree.
        """
        return cls(
            timezone=policy.timezone,
            start_time=start_time,
            end_time=policy.entry_cutoff.strftime("%H:%M"),
            square_off_time=policy.square_off_at.strftime("%H:%M"),
            holidays=holidays,
        )


@dataclass(frozen=True)
class EngineConfig:
    """Exactly the configuration :class:`~common.engine.engine.TradingEngine` reads."""

    #: Candle interval as the reference expresses it, e.g. ``"5m"``, ``"1h"``.
    timeframe: str = "5m"
    session: SessionConfig = field(default_factory=SessionConfig)
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    #: Strategy-wide daily loss cap, as a percentage of ``starting_capital``.
    #: ``None`` or ``<= 0`` disables the guard entirely.
    max_daily_loss_percent: float | None = None
    starting_capital: float = 100_000.0
    #: ``warmup_from_history: false`` on a continuity-required strategy is a
    #: config error, not an operator waiver — see
    #: :func:`common.warmup.requirements.validate_warmup_config`.
    warmup_from_history: bool = True
    #: Market Regime Engine. Observational only; see :mod:`common.engine.regime`.
    regime_enabled: bool = False
    #: Free-form strategy parameters, passed through untouched.
    parameters: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_resolved(
        cls,
        cfg: ResolvedConfig,
        *,
        timeframe: str = "5m",
        session: SessionConfig | None = None,
        square_off_policy: SquareOffPolicy | None = None,
        starting_capital: float = 100_000.0,
    ) -> EngineConfig:
        """Build from this repository's resolved configuration.

        Only the strategy's own ``risk``/``parameters`` blocks are consulted, and
        the execution mode comes from the strategy config — the same value the
        broker factory gates on, read once rather than inferred twice.

        Pass ``square_off_policy`` to derive the session boundaries from the object
        that owns square-off. Passing **both** it and an explicit ``session`` is
        refused rather than resolved: silently preferring one would reintroduce the
        two-independently-configured-times drift this argument exists to end.
        """
        if session is not None and square_off_policy is not None:
            raise ValueError(
                "Pass either session or square_off_policy, not both — supplying "
                "both is what lets the entry-cutoff and square-off times drift "
                "apart. Use SessionConfig.from_square_off_policy() if you need to "
                "override the start time or the holiday calendar."
            )
        if square_off_policy is not None:
            session = SessionConfig.from_square_off_policy(square_off_policy)

        risk = cfg.strategy.risk or {}
        return cls(
            timeframe=timeframe,
            session=session or SessionConfig(timezone=cfg.global_config.timezone),
            execution_mode=cfg.strategy.mode,
            max_daily_loss_percent=risk.get("max_daily_loss_percent"),
            starting_capital=starting_capital,
            warmup_from_history=bool(cfg.strategy.parameters.get("warmup_from_history", True)),
            regime_enabled=bool(cfg.strategy.parameters.get("regime_enabled", False)),
            parameters=dict(cfg.strategy.parameters),
        )
