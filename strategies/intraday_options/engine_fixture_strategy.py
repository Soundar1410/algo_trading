"""The deterministic test-only strategy for the ported engine.

**This is not a trading strategy and must never be treated as one.** Like its
sibling :mod:`strategies.intraday_options.fixture_strategy`, it contains no market
hypothesis and no edge. Its job is to make :class:`~common.engine.engine.
TradingEngine`'s orchestration observable: given candle N, enter; given a premium
candle whose close turns adverse, exit — so a test can assert the engine routed
ticks, built both candle streams, consulted the exit policy and closed a position.

Why it exists at all (Phase 3 Part 2b-i, deviation D22)
--------------------------------------------------------
The reference repository's only end-to-end ``TradingEngine`` suite is
``strategies/ema_cross/tests/test_premium_candle_exit.py``, and it builds the real
EMA-cross strategy: an actual indicator, an actual trade manager, actual tuned
config. ``CLAUDE.md`` defers real strategies to Phase 9, and porting one as "just a
test fixture" would put a live edge in the tree ahead of schedule. So the nine
properties that suite proves are rebuilt against this double instead.

What is *not* faked: the exit decision. This double delegates to the **real**
:class:`~common.exit.momentum_close.MomentumCloseExit` from the Part 2a registry,
in ``premium`` mode, driven by a real :class:`~common.engine.models.OpenPosition`.
That is deliberate — a double that reimplemented the exit rule would prove the
engine calls *something*, not that the ported policies work through it.

Determinism is by candle index, not by clock: the Nth completed underlying candle
triggers entry, so the same tape always produces the same trades.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from common.engine.models import (
    Moneyness,
    OpenPosition,
    OptionSelection,
    OptionType,
    OrderSide,
    SignalAction,
    StrategySignal,
    Trade,
)
from common.engine.risk import RiskManager
from common.engine.strategy import BaseStrategy
from common.exit import get_exit_engine
from common.exit.momentum_close import MomentumCloseExit
from common.indicators.base import OHLC
from common.models import ExitReason, Tick
from common.warmup.requirements import StrategyWarmupSpec


class FixtureRiskManager(RiskManager):
    """Exits at a fixed unrealised-rupee stop. No trailing, no target, no state.

    Enough to prove the engine consults the risk manager *before* the premium
    candle exit — the priority rule the reference suite pinned — without importing
    a real risk policy (Phase 9).
    """

    name = "fixture"

    def __init__(self, stop_loss_rupees: float | None = None) -> None:
        super().__init__(cfg=None)
        self.stop_loss_rupees = stop_loss_rupees
        self.armed_lots = 0
        self.resets = 0

    def reset(self) -> None:
        self.armed_lots = 0
        self.resets += 1

    def new_position(self, lots: int = 1) -> None:
        self.armed_lots = lots

    def on_pnl(self, pnl: float) -> ExitReason | None:
        if self.stop_loss_rupees is None:
            return None
        return ExitReason.STOP_LOSS if pnl <= -abs(self.stop_loss_rupees) else None

    @property
    def state(self) -> Any:
        return {"armed_lots": self.armed_lots, "stop": self.stop_loss_rupees}


class EngineFixtureStrategy(BaseStrategy):
    """Enters on a chosen candle; exits from the traded option's own premium bars.

    Args:
        enter_on_candle: 1-based index (or indices) of the completed underlying
            candle that produces an ENTER signal. ``None`` never enters.
        option_type: which leg to enter (CE or PE).
        side: BUY (long the option) or SELL (write it).
        premium_exit: opt into the premium candle stream and the real
            ``MOMENTUM_CLOSE`` policy evaluated on it.
        stop_loss_rupees: arms :class:`FixtureRiskManager`; ``None`` disables it.
        continuity_required: declare a path-dependent warm-up need, so the
            fail-closed gate can be exercised.
    """

    name = "engine_fixture"

    def __init__(
        self,
        *,
        enter_on_candle: int | Sequence[int] | None = 1,
        option_type: OptionType = OptionType.CE,
        side: OrderSide = OrderSide.BUY,
        premium_exit: bool = False,
        stop_loss_rupees: float | None = None,
        lots: int = 1,
        continuity_required: bool = False,
        warmup_spec_raises: bool = False,
    ) -> None:
        super().__init__(cfg=None)
        # A sequence rather than a single index so a test can drive a genuine
        # re-entry — the property the reference proved on its trade manager, and
        # one that cannot be shown by closing a single position and inspecting the
        # wreckage.
        if enter_on_candle is None:
            self.entry_candles: frozenset[int] = frozenset()
        elif isinstance(enter_on_candle, int):
            self.entry_candles = frozenset({enter_on_candle})
        else:
            self.entry_candles = frozenset(enter_on_candle)
        self.option_type = option_type
        self.side = side
        self._premium_exit = premium_exit
        self._lots = lots
        self._continuity_required = continuity_required
        self._warmup_spec_raises = warmup_spec_raises
        self._risk = FixtureRiskManager(stop_loss_rupees)

        # The real Part 2a policy, in premium mode — not a reimplementation.
        # Resolved through the registry rather than constructed directly, so this
        # also exercises the name->class wiring; the isinstance check is what makes
        # that lookup type-safe, and it would catch a registry that silently
        # started handing back a different class under the same name.
        policy = get_exit_engine("momentum_close", {"price_stream": "premium"})
        if not isinstance(policy, MomentumCloseExit):  # pragma: no cover - registry guard
            raise TypeError(
                f"the 'momentum_close' registry entry resolved to {type(policy).__name__}, "
                "which does not expose should_exit_closes()"
            )
        self._exit_engine: MomentumCloseExit = policy

        # Observable state the tests assert on.
        self.candles_seen = 0
        self.option_candles_seen = 0
        self.underlying_candles: list[OHLC] = []
        self.premium_closes: list[float] = []
        self.gaps_notified = 0
        self.closed_trades: list[Trade] = []
        self.resets = 0
        #: The position the engine last handed to on_position_tick. Kept so a test
        #: can prove the exit engine saw a real OpenPosition, not a stand-in.
        self.last_position: OpenPosition | None = None
        self._previous_premium_close: float | None = None

    # ------------------------------------------------------------- lifecycle
    def reset(self) -> None:
        self.resets += 1
        self.candles_seen = 0
        self.option_candles_seen = 0
        self.underlying_candles = []
        self.premium_closes = []
        self._previous_premium_close = None
        self._exit_engine.reset()

    def status(self) -> str:
        return f"candles={self.candles_seen} premium_bars={self.option_candles_seen}"

    def warmup_spec(self) -> StrategyWarmupSpec | None:
        if self._warmup_spec_raises:
            raise RuntimeError("fixture: warmup_spec deliberately raises")
        if not self._continuity_required:
            return None
        return StrategyWarmupSpec(min_bars=10, continuity_required=True)

    # ---------------------------------------------------------------- entries
    def on_candle(self, candle: OHLC, timestamp: datetime) -> StrategySignal | None:
        self.candles_seen += 1
        self.underlying_candles.append(candle)
        if self.candles_seen not in self.entry_candles:
            return None
        return StrategySignal(
            action=SignalAction.ENTER,
            timestamp=timestamp,
            option_type=self.option_type,
            side=self.side,
            reason=f"fixture entry on candle {self.candles_seen}",
        )

    # ----------------------------------------------------------- premium exit
    @property
    def needs_option_candles(self) -> bool:
        return self._premium_exit

    def on_option_candle(self, candle: OHLC, timestamp: datetime) -> StrategySignal | None:
        self.option_candles_seen += 1
        self.premium_closes.append(candle.close)
        previous = self._previous_premium_close
        self._previous_premium_close = candle.close
        # The real registered policy decides, on the traded option's own bars.
        if self._exit_engine.should_exit_closes(candle.close, previous, side=self.side):
            return StrategySignal(
                action=SignalAction.EXIT,
                timestamp=timestamp,
                exit_reason=ExitReason.MOMENTUM_LOW,
                reason="fixture premium-candle exit",
            )
        return None

    def on_option_candle_gap(self) -> None:
        """Forget the previous premium close so a streak re-primes on fresh bars.

        This is the whole point of the hook: after a gap the next bar's close has
        no meaningful predecessor, and comparing across the hole would manufacture
        an exit that the market never showed.
        """
        self.gaps_notified += 1
        self._previous_premium_close = None

    def on_position_tick(self, position: OpenPosition, tick: Tick) -> StrategySignal | None:
        self.last_position = position
        return None

    def on_position_closed(self, trade: Trade) -> None:
        """Drop the streak with the position it belonged to.

        Load-bearing, not bookkeeping. The engine resets its *own* premium candle
        builder on close, but the streak's other half — "what did the previous bar
        close at?" — lives here, in the strategy. Without this, the next position's
        first bar would be compared against the *previous contract's* last close and
        could exit immediately on a price relationship that never existed for the
        new leg.

        The reference does the same thing in ``EMA1TradeManager.on_position_closed``,
        where its test asserts ``engine.extreme_close is None`` afterwards. Keeping
        it here means the rebuilt re-entry test covers both halves of the property
        end to end rather than only the engine's.
        """
        self.closed_trades.append(trade)
        self._previous_premium_close = None
        self._exit_engine.reset()

    # ------------------------------------------------------------- properties
    @property
    def option_selection(self) -> OptionSelection:
        return OptionSelection(moneyness=Moneyness.ATM, steps=0)

    @property
    def trade_side(self) -> OrderSide:
        return self.side

    @property
    def quantity_lots(self) -> int:
        return self._lots

    @property
    def risk_manager(self) -> RiskManager:
        return self._risk
