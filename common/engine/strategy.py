"""Strategy base class and registry.

Ported from the reference repository's ``framework/base/base_strategy.py``
(Phase 3 Part 2b-i). A strategy owns everything about how *it* trades: entry
conditions (:meth:`~BaseStrategy.on_candle`), exit conditions beyond the risk
manager (:meth:`~BaseStrategy.on_position_tick`), option selection, BUY vs SELL,
position size and its risk policy. :class:`~common.engine.engine.TradingEngine`
only ever talks to this abstract interface, so it needs no strategy-specific code.

**No real strategy subclasses this yet, and none may until Phase 9.** The port
exists so the engine has the contract it was written against; the only
implementations in this repository are test doubles.

Note the deliberate distinction from :class:`strategies.intraday_options.
fixture_strategy.Strategy`. That protocol is the *worker's* seam — one completed
candle in, at most one persisted-shape signal out — and Phase 1 built it to be
swappable for this one. This is the engine's richer contract: ticks, premium
candles, warm-up specs and a risk manager. Both stay; the worker chooses which it
drives, which is Part 2b-ii's decision.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from common.indicators.base import OHLC
from common.models import Tick

from .models import OpenPosition, OptionSelection, OrderSide, StrategySignal

if TYPE_CHECKING:
    from common.warmup.requirements import StrategyWarmupSpec

    from .models import Trade
    from .risk import RiskManager

# name -> BaseStrategy subclass
_REGISTRY: dict[str, type[BaseStrategy]] = {}


def register_strategy(name: str) -> Callable[[type[BaseStrategy]], type[BaseStrategy]]:
    """Class decorator that registers a strategy under ``name``."""

    def _wrap(cls: type[BaseStrategy]) -> type[BaseStrategy]:
        key = name.lower()
        if key in _REGISTRY and _REGISTRY[key] is not cls:
            raise ValueError(f"Strategy name {name!r} is already registered.")
        _REGISTRY[key] = cls
        cls.name = key
        return cls

    return _wrap


def get_strategy(name: str, cfg: Any) -> BaseStrategy:
    """Instantiate a registered strategy by name with the given runtime config."""
    key = name.lower()
    if key not in _REGISTRY:
        raise KeyError(
            f"Unknown strategy {name!r}. Registered: {', '.join(sorted(_REGISTRY)) or '(none)'}."
        )
    return _REGISTRY[key](cfg)


def available_strategies() -> list[str]:
    return sorted(_REGISTRY)


class BaseStrategy(ABC):
    """Turns closed candles into entry signals and owns its own trade rules.

    Contract:
        * :meth:`on_candle` is called once per *closed* candle and returns a
          :class:`~common.engine.models.StrategySignal` to act on, or ``None``.
        * :meth:`reset` is called at the start of each trading day.
        * :meth:`on_position_tick` is called on every tick for an open position,
          in addition to ``risk_manager.on_pnl()`` — override for
          strategy-specific exit logic. Default: rely entirely on the risk manager.
    """

    name: str = "base"

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self.params: dict[str, Any] = dict(getattr(cfg, "parameters", {}) or {})

    @abstractmethod
    def on_candle(self, candle: OHLC, timestamp: datetime) -> StrategySignal | None: ...

    @abstractmethod
    def reset(self) -> None: ...

    def on_position_tick(self, position: OpenPosition, tick: Tick) -> StrategySignal | None:
        return None

    def on_position_closed(self, trade: Trade) -> None:
        """Called whenever an open position is closed, for any reason (stop loss,
        trailing, target, opposite signal, square-off). Optional hook for
        strategies that track outcomes across trades (e.g. a consecutive-loss
        cooldown). Default: no-op."""
        return None

    def exit_state_snapshot(self) -> dict[str, Any]:
        """Restart-recoverable exit-policy state, for :class:`~common.engine.engine.
        TradingEngine` to persist. Phase 6 Part 2.

        Default ``{}`` (no-op) — the engine has no other way to reach whatever
        exit engine(s) a strategy holds privately (:attr:`risk_manager` is the
        only strategy-internal object the engine already reaches, and that is a
        different concern — see :class:`~common.engine.risk.RiskManager`). A
        strategy that owns a stateful exit engine overrides this to delegate to
        its own :meth:`~common.exit.base.BaseExit.snapshot`; one that doesn't
        inherits the no-op and pays nothing."""
        return {}

    def restore_exit_state(self, data: dict[str, Any]) -> None:
        """Reapply a previous :meth:`exit_state_snapshot`. Default no-op.

        Must never raise — a missing or foreign snapshot degrades exit timing
        quality (a trailing stop re-arms from the current price instead of its
        true peak), never safety, so the correct default is to continue with
        already-:meth:`reset` state rather than block."""
        return None

    @property
    def needs_option_candles(self) -> bool:
        """Opt into :class:`~common.engine.engine.TradingEngine` building a
        per-position candle stream from the *traded option's own* premium ticks
        (rebuilt fresh on every entry/re-entry/strike change, discarded on exit)
        and calling :meth:`on_option_candle` on each completed one. Default
        ``False`` — the engine allocates no extra candle builder and never calls
        the hook, so strategies that don't opt in are unaffected."""
        return False

    def on_option_candle(self, candle: OHLC, timestamp: datetime) -> StrategySignal | None:
        """Called once per *completed* candle of the currently open position's own
        traded-instrument (premium) price series — only when
        :attr:`needs_option_candles` is ``True``. Entry decisions must never be
        made here; this exists solely for exit rules that need to evaluate the
        traded instrument's own candle rather than the underlying's. Default:
        no-op."""
        return None

    def on_option_candle_gap(self) -> None:
        """Clear consecutive-candle references after skipped premium buckets.

        The engine calls this before rebuilding the traded-option candle stream.
        Strategies should retain best-close state for the open trade but forget
        any previous candle used for a range comparison.
        """
        return None

    def on_candle_gap(self, candle: OHLC, timestamp: datetime) -> None:
        """A closed **underlying** bar was stitched across a hole in the tick stream.

        Phase 4 Part 3, the counterpart to :meth:`on_option_candle_gap`. The
        engine calls this **instead of** :meth:`on_candle` — the bar is *not* fed
        to indicators and produces no signal, because its OHLC was assembled from
        ticks either side of missing data.

        **The rule this hook exists to let a strategy honour**, expressed in terms
        of the scope its indicators already declare in
        :mod:`common.warmup.requirements`:

        * :attr:`~common.warmup.requirements.IndicatorScope.SESSION_LOCAL`
          indicators — VWAP is the one in the tree — must be **reset**. They are
          session-cumulative, so missing volume is not something they recover
          from: every value for the rest of the day would be wrong by the amount
          the hole swallowed. Use :func:`common.indicators.reset_session_local`.
        * :attr:`~common.warmup.requirements.IndicatorScope.SESSION_SPANNING`
          indicators — EMA, RSI, ATR, ADX, SuperTrend — should be **left alone**.
          They are exponentially forgetting, so a missing bar decays out of them
          on its own; resetting would throw away far more history than the hole
          cost. That is the same convergence property Part 2 measured when it
          justified the oracle tolerances.

        Default: no-op, because a strategy holding only session-spanning
        indicators genuinely has nothing to do. The passed ``candle`` carries
        ``spans_gap=True`` and is supplied so a strategy can log or count it.
        """
        return None

    def status(self) -> str:
        """Short, human-readable indicator state for periodic heartbeat logs."""
        return "no indicator status"

    def warmup_spec(self) -> StrategyWarmupSpec | None:
        """Opt into historical indicator warm-up (:mod:`common.warmup`).

        Return a :class:`~common.warmup.requirements.StrategyWarmupSpec`
        aggregating this strategy's indicators' requirements (typically
        ``StrategyWarmupSpec.from_indicators([...])``). Default ``None`` = cold
        start, so strategies that haven't opted in are unaffected."""
        return None

    def on_warmup_complete(self) -> None:
        """Called once, after :class:`~common.engine.engine.TradingEngine` has
        finished replaying (or attempting to replay) today's warm-up candles,
        and before the first live candle. Phase 9.

        Warm-up replays closed candles through :meth:`on_candle` itself — there
        is no separate "replay mode" — so any indicator holds correct state
        afterwards, but a *day-scoped detector* built on top of an indicator
        (e.g. :class:`~common.indicators.ema.ConfirmedCrossover`, which latches
        "confirmed side" and must not treat a trend inherited from a prior
        session as today's fresh signal) would otherwise come out of warm-up
        already primed with yesterday's side. This hook is where such a
        strategy resets *only* that day-scoped detector, leaving the indicator
        itself (session-spanning, intentionally warmed) untouched.

        Called unconditionally — whether warm-up replayed real history, ran
        cold (no manager/history available), or was skipped entirely
        (:meth:`warmup_spec` returned ``None``) — so a strategy's hook body
        does not need to know which case it is in; resetting an
        already-fresh detector is a harmless no-op. Default: no-op, for every
        strategy that has no such detector.
        """
        return None

    @property
    @abstractmethod
    def option_selection(self) -> OptionSelection:
        """Default ITM/ATM/OTM + steps for this strategy's entries."""

    @property
    @abstractmethod
    def trade_side(self) -> OrderSide:
        """BUY (go long the option) or SELL (write/short the option)."""

    @property
    @abstractmethod
    def quantity_lots(self) -> int:
        """Lots per trade for this strategy."""

    @property
    @abstractmethod
    def risk_manager(self) -> RiskManager:
        """This strategy's own risk policy (SL / lock / trail / target)."""
