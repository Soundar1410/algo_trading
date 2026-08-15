"""The engine's in-memory domain models.

Ported from the reference repository's ``framework/core/models.py`` (Phase 3
Part 2b-i). These are the types that flow *inside* one strategy process while a
position is live: the strategy's decision, the contract being traded, the open
position the risk and exit engines read on every tick, and the closed trade the
report is built from.

Two names are deliberately **not** the reference's (deviation D19):

``Signal`` → :class:`StrategySignal`
    This repository already has a :class:`common.models.Signal` — a frozen,
    persistence-shaped record carrying ``strategy_id``/``execution_mode``, written
    to the ``signals`` table before any broker call. That one is the audit record;
    this one is the strategy's in-flight decision. They are different things.

``Position`` → :class:`OpenPosition`
    Likewise :class:`common.models.Position` is the persisted net-exposure row,
    keyed by strategy/mode/trading-date. This one is the mutable, tick-by-tick
    object the ten Part 2a exit policies read (``.contract.option_type``,
    ``.side``, ``.entry_price``, ``.last_price``). Two live types sharing the name
    ``Position`` in one tree is precisely the confusion that produces a wrong exit
    rather than an error.

Everything else keeps its reference name and shape. In particular
:class:`OptionType`, :class:`OrderSide` and :class:`ExitReason` are **imported**
from :mod:`common.models.trading`, where Part 2a already ported them — they are
not redeclared here, so ``position.side is OrderSide.BUY`` holds across both
layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from common.models import ExitReason, OptionType, OrderSide

__all__ = [
    "AdoptedPosition",
    "ExitReason",
    "Moneyness",
    "OpenPosition",
    "OptionContract",
    "OptionSelection",
    "OptionType",
    "OrderSide",
    "SignalAction",
    "StrategySignal",
    "Trade",
    "UnmanageablePositionState",
]


class UnmanageablePositionState(RuntimeError):
    """Open exposure exists but this engine cannot safely adopt/manage it."""


class Moneyness(StrEnum):
    """Strike selection relative to spot, independent of CE/PE."""

    ITM = "ITM"
    ATM = "ATM"
    OTM = "OTM"


class SignalAction(StrEnum):
    """What the strategy wants the engine to do."""

    ENTER = "ENTER"
    EXIT = "EXIT"


@dataclass(frozen=True)
class OptionSelection:
    """How a strategy wants its strike chosen, independent of a signal."""

    moneyness: Moneyness = Moneyness.ATM
    #: Strikes away from ATM in the ITM/OTM direction (0 = pure ATM).
    steps: int = 0


@dataclass(frozen=True)
class StrategySignal:
    """A trading instruction emitted by a strategy on a closed candle."""

    action: SignalAction
    timestamp: datetime
    #: Required for ENTER, ignored for EXIT.
    option_type: OptionType | None = None
    #: BUY = go long the option, SELL = write it.
    side: OrderSide = OrderSide.BUY
    #: Overrides the strategy default, if set.
    option_selection: OptionSelection | None = None
    reason: str = ""
    # Optional override for the closed Trade's exit_reason (e.g. a granular
    # candle-structure/trail reason from an exit engine). None (the default)
    # keeps existing behaviour: the engine records a generic STRATEGY_EXIT.
    exit_reason: ExitReason | None = None


@dataclass(frozen=True)
class OptionContract:
    """An identifiable, tradable option contract."""

    symbol: str  # human-readable, e.g. "NIFTY 25JUN 23500 CE"
    security_id: str  # broker instrument id used for orders/subscription
    strike: float
    option_type: OptionType
    expiry: str  # ISO date or broker expiry code
    lot_size: int

    def __str__(self) -> str:  # pragma: no cover - display aid
        return self.symbol


@dataclass
class OpenPosition:
    """An open position — long (BUY) or short/written (SELL).

    The reference's ``Position``. Mutable by design: the engine updates it on
    every option tick, and the risk manager and exit engines read the running
    figures from it.
    """

    contract: OptionContract
    side: OrderSide
    lots: int
    entry_price: float
    entry_time: datetime
    quantity: int = field(init=False)  # lots * lot_size
    last_price: float = field(init=False)
    # Running best/worst unrealised P&L seen while the position is open, used
    # to report MFE/MAE on the resulting Trade — the "how good/bad did it get
    # before exit" figures a single entry/exit price pair can't show.
    max_favorable_pnl: float = field(init=False)
    max_adverse_pnl: float = field(init=False)
    #: The correlation ID the entry order actually persisted under, if the
    #: gateway reported one (``LifecycleGateway`` does; ``InMemoryGateway``
    #: does not). ``None`` for an adopted position until restart recovery
    #: fills it in from the persisted row. Set by ``PositionManager.open``/
    #: ``.adopt`` after construction — not a constructor argument, so every
    #: existing caller (positional or keyword) is unaffected.
    entry_correlation_id: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.quantity = self.lots * self.contract.lot_size
        self.last_price = self.entry_price
        self.max_favorable_pnl = 0.0
        self.max_adverse_pnl = 0.0

    def update_price(self, price: float) -> None:
        self.last_price = price
        pnl = self.unrealised_pnl
        if pnl > self.max_favorable_pnl:
            self.max_favorable_pnl = pnl
        if pnl < self.max_adverse_pnl:
            self.max_adverse_pnl = pnl

    @property
    def unrealised_pnl(self) -> float:
        """Gross (pre-charge) P&L in rupees at the last seen price.

        Long (BUY): (current - entry) * qty. Short/written (SELL): the
        inverse, since the position profits as the option loses value.
        """
        if self.side is OrderSide.BUY:
            return (self.last_price - self.entry_price) * self.quantity
        return (self.entry_price - self.last_price) * self.quantity


@dataclass(frozen=True)
class AdoptedPosition:
    """A position a previous process opened, rebuilt for this one to manage.

    Phase 3 Part 2b-ii-B-2. The engine is handed one of these at
    :meth:`~common.engine.engine.TradingEngine._start_day` when a restart finds an
    open position in the database. It carries what
    :meth:`~common.engine.positions.PositionManager.adopt` needs and nothing more —
    in particular no order, no correlation ID and no gateway, because adopting must
    place no order at all.

    ``entry_price``, ``quantity`` and ``entry_charges`` come from the authoritative
    ``positions`` row; the contract itself has to be rebuilt from
    ``strategy_state.payload``, because the row cannot express an option's type,
    strike, expiry or lot size. See :mod:`common.engine.state_payload`.
    """

    contract: OptionContract
    side: OrderSide
    lots: int
    entry_price: float
    entry_time: datetime
    entry_charges: float = 0.0
    #: Last traded premium, if the caller knows one. ``None`` leaves the position
    #: marked at its entry price until the first tick arrives.
    last_price: float | None = None
    #: The excursion this position saw before the restart, from
    #: ``positions.highest_favourable``/``.lowest_favourable`` (Phase 6 Part 3).
    #: Default ``0.0`` matches a fresh ``OpenPosition``'s own starting value, so
    #: a position with nothing persisted yet (an older row, or one closed before
    #: Part 3 shipped) restarts exactly as it did before this part existed.
    max_favorable_pnl: float = 0.0
    max_adverse_pnl: float = 0.0
    #: The last candle this strategy-day fully processed before the restart
    #: (``strategy_state.last_candle_end_at``), Phase 6 Part 3. ``None`` means
    #: nothing was recorded (an older row, or no candle has closed yet) — the
    #: engine then guards against nothing, exactly as before this part existed.
    #: Position-scoped rather than a day-level provider deliberately: it is
    #: only ever restored when a position was actually adopted, matching the
    #: write side's own "only while a position is open" gate (see
    #: ``TradingEngine._persist_exit_state``).
    last_candle_end_at: datetime | None = None


@dataclass
class Trade:
    """A completed (round-trip) trade, used for the end-of-day report."""

    contract: OptionContract
    side: OrderSide
    lots: int
    quantity: int
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    exit_reason: ExitReason
    gross_pnl: float
    charges: float
    charges_breakdown: dict[str, float] = field(default_factory=dict)
    # Maximum Favorable/Adverse Excursion: the best/worst unrealised gross P&L
    # seen while the position was open (from OpenPosition.max_favorable_pnl /
    # max_adverse_pnl). Default 0.0 for any Trade built without a position
    # (e.g. a hand-built test fixture) rather than requiring every call site
    # to supply them.
    mfe: float = 0.0
    mae: float = 0.0
    # Market Regime Engine tags (common.engine.regime). ``entry_regime`` is the
    # market regime at the moment the position was opened (the decision-relevant
    # tag), ``exit_regime`` the regime at close, and ``session_tags`` a
    # comma-joined set of orthogonal session flags (e.g. "EXPIRY_DAY,GAP_DAY").
    entry_regime: str | None = None
    exit_regime: str | None = None
    session_tags: str = ""
    # JSON-encoded raw classifier feature values at entry — lets a regime label be
    # recomputed under different thresholds later without replaying history.
    entry_regime_features: str = "{}"
    # Combined premium-candle exit analytics. Optional so trades produced without
    # the premium stream, and older fixtures, remain compatible.
    exit_mode: str | None = None
    highest_completed_close: float | None = None
    lowest_completed_close: float | None = None
    best_favourable_close: float | None = None
    retracement_points: float | None = None
    retracement_percentage: float | None = None
    candle_structure_triggered: bool | None = None
    trail_triggered: bool | None = None
    trail_activated: bool | None = None
    #: The correlation IDs the entry and exit orders actually persisted
    #: under, when the gateway reports one — the join key a caller (e.g.
    #: multi-leg restart reconciliation) uses to find the authoritative
    #: ``order_intents``/``orders``/``fills`` rows for this exact round
    #: trip, rather than approximating by ``(security_id, time)``.
    entry_correlation_id: str | None = None
    exit_correlation_id: str | None = None

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.charges

    @property
    def capital_used(self) -> float:
        """Premium deployed to open this trade (entry price * quantity)."""
        return self.entry_price * self.quantity
