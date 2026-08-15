"""The multi-leg engine's in-memory domain models: baskets and leg instances.

Sibling to :mod:`common.engine.models`, for the same reason
:class:`~common.engine.multi_leg_engine.MultiLegEngine` is a sibling of
:class:`~common.engine.engine.TradingEngine` rather than a modification of it
(see that module's docstring): the single-leg engine's vocabulary —
``StrategySignal``, one ``OpenPosition`` at a time — has no way to express two
independently-managed legs sharing one logical entry decision. This module is
that missing vocabulary, generic to any multi-leg strategy, not specific to
``straddle_920``.

Reused from :mod:`common.engine.models` rather than redeclared: ``OptionContract``,
``OptionSelection``, ``Trade`` — their shapes already fit a leg exactly.
:class:`~common.models.OrderSide` and :class:`~common.models.ExitReason` come from
:mod:`common.models.trading`, same as the single-leg module.

Ownership rule
---------------
A :class:`Basket` is engine-owned, mutable, in-memory state — the durable copy
lives in the ``strategy_baskets``/``strategy_legs`` tables (migration ``0009``),
written by :mod:`common.engine.multi_leg_state`. A strategy built on
:class:`~common.engine.multi_leg_strategy.BaseMultiLegStrategy` is handed the
current :class:`Basket` on every call and reads it to decide what to do next; it
returns a :class:`BasketSignal` describing the desired action rather than
mutating the basket itself, mirroring how :class:`~common.engine.strategy.
BaseStrategy.on_candle` returns a :class:`~common.engine.models.StrategySignal`
instead of touching :class:`~common.engine.positions.PositionManager` directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from common.models import ExitReason, OrderSide

from .models import OptionContract, OptionSelection

__all__ = [
    "PENDING_LEG_STATES",
    "TERMINAL_LEG_STATES",
    "UNRESOLVED_LEG_STATES",
    "AdjustmentLifecycle",
    "Basket",
    "BasketAction",
    "BasketSignal",
    "LegInstance",
    "LegIntent",
    "LegRole",
    "LegState",
    "MultiLegDurabilityError",
    "UnmanageableBasketState",
]


class UnmanageableBasketState(RuntimeError):
    """Open basket/leg exposure exists but this engine cannot safely adopt/manage it.

    The multi-leg counterpart of :class:`~common.engine.models.
    UnmanageablePositionState` — raising this must reach the engine's restart path
    and latch new entries off for the day rather than guess at a premium basis or
    an adjustment count. See :mod:`runtimes.intraday_options.multi_leg_engine_worker`.
    """


class MultiLegDurabilityError(RuntimeError):
    """A required basket/leg persistence write failed while persistence was
    configured (a production worker) — never swallowed.

    Raised only at *pre-effect* checkpoints, where the durable claim must
    exist before an irreversible action (an order submission) is allowed to
    proceed — the primary-attempt consumption, a pending leg's identity
    before it is subscribed, and the sole adjustment's claim before its
    exit is submitted. A *post-effect* projection write (recording that an
    already-executed fill closed a leg) failing is a different, lesser
    problem — the trade already happened durably through the order/fill
    tables — and is handled as best-effort plus an incident, never by
    raising this and never by undoing the trade.

    An engine constructed with no persistence callbacks at all (an offline
    or test engine — see :class:`~common.engine.multi_leg_engine.
    MultiLegEngine`'s ``persist_basket``/``persist_leg`` parameters) never
    raises this: "no persistence configured" is an explicit, supported mode,
    distinct from "persistence configured but failing".
    """


class LegRole(StrEnum):
    """Which side of the basket a leg represents.

    ``GENERIC`` is the escape hatch for a future multi-leg strategy whose legs
    are not CE/PE (e.g. a calendar spread) — nothing in this module or the
    engine requires exactly CE+PE.
    """

    CE = "CE"
    PE = "PE"
    GENERIC = "GENERIC"


class LegState(StrEnum):
    """A leg instance's lifecycle, from intent to terminal outcome."""

    #: The basket wants this leg but its contract has not been resolved yet.
    PENDING_CONTRACT = "PENDING_CONTRACT"
    #: Contract resolved; the dynamic subscription has been requested.
    PENDING_SUBSCRIPTION = "PENDING_SUBSCRIPTION"
    #: Subscribed; waiting for a fresh tick to fill the sell/buy order.
    PENDING_ORDER = "PENDING_ORDER"
    #: Filled and currently open.
    OPEN = "OPEN"
    #: Closed with a fill (adjustment, risk trigger, square-off, ...).
    CLOSED = "CLOSED"
    #: Never reached OPEN — contract resolution or subscription failed.
    FAILED = "FAILED"
    #: Never reached OPEN before the cutoff that permits it passed.
    EXPIRED = "EXPIRED"
    #: A close was submitted for this leg and its outcome could not be
    #: established (the gateway raised rather than confirming a fill).
    #: Deliberately **not** ``OPEN`` (so nothing retries the close
    #: automatically — see :meth:`~common.engine.multi_leg_engine.
    #: MultiLegEngine._close_leg_safely`) and **not** ``CLOSED`` (the fill
    #: is not confirmed). Only an operator or a future reconciliation pass
    #: that actually queries broker/execution state may resolve this —
    #: never a guess.
    CLOSE_SUBMISSION_UNKNOWN = "CLOSE_SUBMISSION_UNKNOWN"


#: States a leg cannot leave — used by :meth:`Basket.open_legs`/``pending_legs``
#: and by restart reconciliation to decide what still needs managing.
TERMINAL_LEG_STATES = frozenset({LegState.CLOSED, LegState.FAILED, LegState.EXPIRED})
PENDING_LEG_STATES = frozenset(
    {LegState.PENDING_CONTRACT, LegState.PENDING_SUBSCRIPTION, LegState.PENDING_ORDER}
)
#: Neither open, pending, nor terminal — needs an operator or reconciliation,
#: never an automatic retry. Excluded from both ``open_legs()`` and
#: ``pending_legs()`` by construction (neither filter matches this state).
UNRESOLVED_LEG_STATES = frozenset({LegState.CLOSE_SUBMISSION_UNKNOWN})


class AdjustmentLifecycle(StrEnum):
    """``Basket.pending_replacement_state``'s vocabulary once an adjustment
    has been triggered (spec section 12.3) — explicit states so a crash at
    any point is distinguishable, at restart, from a healthy one.

    Required ordering (never skipped, never reordered):

    1. ``CLAIMED`` — the sole adjustment is durably claimed (``adjustment_
       count`` incremented and persisted) but the adjusted leg's close has
       not been submitted yet.
    2. ``EXIT_SUBMISSION_PENDING`` — about to submit / submitting the close.
       Written in the same durable checkpoint as ``CLAIMED`` in practice
       (nothing observable happens between deciding to claim and attempting
       the close), so the engine writes this value directly rather than two
       separate states for the same instant.
    3. ``EXIT_UNKNOWN`` — the close was attempted and its outcome could not
       be established. Terminal for the day: no replacement is attempted
       while this holds, and it is never silently resolved by a guess.
    4. ``EXIT_CONFIRMED`` — the closing fill is confirmed.
    5. ``AWAITING_NEXT_CANDLE`` — the **only** state from which a replacement
       signal may be produced (spec section 12.3 step 5).
    6. ``REPLACEMENT_PENDING`` — the one-shot replacement attempt has been
       made (a leg is pending or the attempt failed) and will not be retried.
    7. ``REPLACEMENT_FILLED`` / ``REPLACEMENT_FAILED`` / ``REPLACEMENT_EXPIRED``
       — terminal outcomes for the day's one adjustment cycle.
    """

    CLAIMED = "CLAIMED"
    EXIT_SUBMISSION_PENDING = "EXIT_SUBMISSION_PENDING"
    EXIT_UNKNOWN = "EXIT_UNKNOWN"
    EXIT_CONFIRMED = "EXIT_CONFIRMED"
    AWAITING_NEXT_CANDLE = "AWAITING_NEXT_CANDLE"
    REPLACEMENT_PENDING = "REPLACEMENT_PENDING"
    REPLACEMENT_FILLED = "REPLACEMENT_FILLED"
    REPLACEMENT_FAILED = "REPLACEMENT_FAILED"
    REPLACEMENT_EXPIRED = "REPLACEMENT_EXPIRED"


class BasketAction(StrEnum):
    """What a :class:`BasketSignal` asks the engine to do."""

    #: Open every leg in ``legs`` as one new logical basket (the primary entry).
    ENTER_BASKET = "ENTER_BASKET"
    #: Open one additional leg into the *existing* basket (a replacement).
    ENTER_LEG = "ENTER_LEG"
    #: Close exactly the leg named by ``target_leg_id``.
    EXIT_LEG = "EXIT_LEG"
    #: Close every currently open/pending leg and block further action today.
    EXIT_ALL = "EXIT_ALL"
    #: No action this call — distinct from returning ``None`` only where a
    #: strategy wants to be explicit; the engine treats both identically.
    NONE = "NONE"


@dataclass(frozen=True)
class LegIntent:
    """One leg's desired shape, inside a :class:`BasketSignal`."""

    role: LegRole
    side: OrderSide
    option_selection: OptionSelection = field(default_factory=OptionSelection)
    is_replacement: bool = False
    #: The leg instance this replaces, when ``is_replacement`` is true.
    replaces_leg_id: str | None = None


@dataclass(frozen=True)
class BasketSignal:
    """Emitted by a multi-leg strategy — the counterpart of ``StrategySignal``."""

    action: BasketAction
    timestamp: datetime
    legs: tuple[LegIntent, ...] = ()
    reason: str = ""
    exit_reason: ExitReason | None = None
    #: For ``EXIT_LEG``: which existing leg instance is targeted.
    target_leg_id: str | None = None


@dataclass
class LegInstance:
    """One durable leg — original or replacement — inside a basket.

    Mutable: the engine updates ``state``/prices/exit fields in place as the
    leg moves through its lifecycle. ``leg_id`` is stable for this instance's
    whole life; a replacement gets its own new ``leg_id`` (see
    :meth:`Basket.next_leg_id`), never reusing the leg it replaces.
    """

    leg_id: str
    basket_id: str
    role: LegRole
    sequence: int
    is_replacement: bool
    side: OrderSide
    quantity: int = 0
    contract: OptionContract | None = None
    state: LegState = LegState.PENDING_CONTRACT
    entry_price: float | None = None
    entry_time: datetime | None = None
    entry_correlation_id: str | None = None
    last_price: float | None = None
    exit_price: float | None = None
    exit_time: datetime | None = None
    exit_reason: ExitReason | None = None
    exit_correlation_id: str | None = None
    realized_gross_pnl: float | None = None
    max_favorable_pnl: float = 0.0
    max_adverse_pnl: float = 0.0
    replaces_leg_id: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state is LegState.OPEN

    @property
    def is_pending(self) -> bool:
        return self.state in PENDING_LEG_STATES

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_LEG_STATES

    @property
    def unrealised_pnl(self) -> float:
        """Gross open P&L at the last seen price. Zero unless genuinely OPEN.

        Matches :class:`~common.engine.models.OpenPosition.unrealised_pnl`'s
        formula exactly (spec section 11): a SELL leg profits as the option
        loses value.
        """
        if self.state is not LegState.OPEN or self.entry_price is None or self.last_price is None:
            return 0.0
        if self.side is OrderSide.BUY:
            return (self.last_price - self.entry_price) * self.quantity
        return (self.entry_price - self.last_price) * self.quantity

    def update_price(self, price: float) -> None:
        self.last_price = price
        pnl = self.unrealised_pnl
        if pnl > self.max_favorable_pnl:
            self.max_favorable_pnl = pnl
        if pnl < self.max_adverse_pnl:
            self.max_adverse_pnl = pnl


@dataclass
class Basket:
    """One logical basket — e.g. the straddle for one trading day.

    ``legs`` holds every leg instance this basket has ever had, original and
    replacement alike, keyed by ``leg_id`` — nothing is ever removed from it,
    which is what makes "closing and reopening the same security creates a
    new leg instance" (spec section 15.2) true structurally rather than by
    convention.
    """

    basket_id: str
    strategy_id: str
    execution_mode: Any  # common.config.models.ExecutionMode; Any avoids an import cycle
    trading_date: str
    legs: dict[str, LegInstance] = field(default_factory=dict)
    adjustment_count: int = 0
    pending_replacement_role: LegRole | None = None
    pending_replacement_state: str | None = None
    #: B_original (spec section 13.4) — the sum of the original two legs'
    #: entry premiums, captured once at the initial opening and never rebased.
    original_combined_basis: float | None = None
    entries_consumed: bool = False
    day_blocked_reason: str | None = None
    square_off_state: str = "PENDING"
    lifecycle_state: str = "OPEN"
    created_at: datetime | None = None

    # ------------------------------------------------------------- queries
    def open_legs(self) -> list[LegInstance]:
        return [leg for leg in self.legs.values() if leg.state is LegState.OPEN]

    def pending_legs(self) -> list[LegInstance]:
        return [leg for leg in self.legs.values() if leg.is_pending]

    def closed_legs(self) -> list[LegInstance]:
        return [leg for leg in self.legs.values() if leg.state is LegState.CLOSED]

    def leg_by_role(self, role: LegRole) -> LegInstance | None:
        """The currently open or pending leg for ``role``, if any (at most one)."""
        for leg in self.legs.values():
            if leg.role is role and (leg.is_open or leg.is_pending):
                return leg
        return None

    def next_sequence(self, role: LegRole) -> int:
        existing = [leg.sequence for leg in self.legs.values() if leg.role is role]
        return (max(existing) + 1) if existing else 1

    def next_leg_id(self, role: LegRole) -> str:
        return f"{self.basket_id}:{role.value}:{self.next_sequence(role)}"

    # --------------------------------------------------------------- P&L
    def realised_gross_pnl(self) -> float:
        """R — gross realised P&L from every closed leg instance, including
        adjusted-out legs (spec section 11)."""
        return sum(leg.realized_gross_pnl or 0.0 for leg in self.closed_legs())

    def unrealised_gross_pnl(self) -> float:
        """U — gross unrealised P&L from currently open leg instances only."""
        return sum(leg.unrealised_pnl for leg in self.open_legs())

    def total_gross_pnl(self) -> float:
        """T = R + U (spec section 11)."""
        return self.realised_gross_pnl() + self.unrealised_gross_pnl()

    def current_open_basis(self) -> float:
        """Sum(entry_premium_i x quantity_i) over currently open legs — the
        combined-stop basis (spec section 13.3). Rebases automatically after
        a replacement fills, because it reads ``open_legs()`` live rather than
        a cached original value."""
        return sum((leg.entry_price or 0.0) * leg.quantity for leg in self.open_legs())
