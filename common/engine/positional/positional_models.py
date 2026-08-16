"""The positional multi-leg engine's in-memory domain model: :class:`Cycle` —
the cross-session sibling of :class:`~common.engine.multi_leg_models.Basket`
for a strategy whose lifecycle spans multiple trading days under one durable
``cycle_id`` (spec sections 9.2/9.3).

Built directly on the existing, proven multi-leg vocabulary rather than
duplicating it: :class:`~common.engine.multi_leg_models.LegInstance`,
``LegState`` (including ``CLOSE_SUBMISSION_UNKNOWN``),
``AdjustmentLifecycle``, ``MultiLegDurabilityError`` and
``UnmanageableBasketState`` are reused verbatim. ``LegRole`` is reused too —
extended additively with the four positional-only roles
(``SHORT_CALL``/``SHORT_PUT``/``HEDGE_CALL``/``HEDGE_PUT``); see that enum's
own docstring for the additivity guarantee.

Ownership rule, identical to ``Basket``'s: :class:`Cycle` is engine-owned,
mutable, in-memory state — the durable copy lives in the
``strategy_cycles``/``strategy_cycle_legs`` tables (migration 0010), written
by :mod:`common.engine.positional.positional_state`. A strategy built on
:class:`~common.engine.positional.positional_strategy.
BasePositionalMultiLegStrategy` is handed the current :class:`Cycle` on every
evaluation and returns a :class:`CycleSignal` describing the desired action;
it never mutates the cycle itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from common.margin import MarginEstimate
from common.models import ExitReason, OrderSide

from ..models import OptionContract
from ..multi_leg_models import (
    PENDING_LEG_STATES,
    TERMINAL_LEG_STATES,
    AdjustmentLifecycle,
    LegInstance,
    LegRole,
    LegState,
    MultiLegDurabilityError,
    UnmanageableBasketState,
)

__all__ = [
    "ENTRY_ROLE_ORDER",
    "HEDGE_ROLES",
    "SHORT_ROLES",
    "TERMINAL_CYCLE_STATES",
    "AdjustmentLifecycle",
    "Cycle",
    "CycleAction",
    "CycleLegIntent",
    "CycleSignal",
    "CycleState",
    "LegInstance",
    "LegRole",
    "LegState",
    "MultiLegDurabilityError",
    "UnmanageableBasketState",
    "UnmanageableCycleState",
    "entry_has_blocking_leg",
    "entry_is_complete",
    "hedge_for",
    "next_entry_role",
    "short_for",
]


class UnmanageableCycleState(UnmanageableBasketState):
    """Open cycle/leg exposure exists but this engine cannot safely adopt or
    manage it — the positional counterpart of
    :class:`~common.engine.multi_leg_models.UnmanageableBasketState`,
    subclassing it directly so any generic handler that already catches the
    base class continues to work. Raising this must reach the engine's
    restart path and latch new entries off until an operator/controlled
    reconciliation resolves it.
    """


class CycleState(StrEnum):
    """A cycle's top-level lifecycle (migration 0010's own CHECK
    vocabulary — kept in exact agreement with it)."""

    #: Sequential staged entry under way; per-leg progress lives entirely on
    #: the legs themselves (see :func:`next_entry_role`) — no duplicate
    #: "which stage" state is kept here.
    ENTERING = "ENTERING"
    #: The intended four-leg exposure is confirmed open; normal monitoring/
    #: adjustment. An in-flight adjustment is a sub-state of ACTIVE
    #: (``pending_adjustment_state``), not a separate top-level value.
    ACTIVE = "ACTIVE"
    #: An exit trigger has fired; working towards flat.
    EXITING = "EXITING"
    #: Flat, terminal, the intended lifecycle finished normally.
    COMPLETED = "COMPLETED"
    #: Entry never completed; every filled leg was unwound; no exposure
    #: remains; terminal.
    FAILED = "FAILED"
    #: Unmanageable/unknown exposure found; blocks new entries; requires
    #: operator/controlled reconciliation; deliberately NOT terminal.
    CRITICAL_UNRESOLVED = "CRITICAL_UNRESOLVED"
    #: Reserved for an explicit future operator action; terminal.
    ABANDONED = "ABANDONED"


TERMINAL_CYCLE_STATES = frozenset({CycleState.COMPLETED, CycleState.FAILED, CycleState.ABANDONED})

#: Spec section 5.2's required sequential order: both hedges, confirmed,
#: before either short.
ENTRY_ROLE_ORDER: tuple[LegRole, ...] = (
    LegRole.HEDGE_PUT,
    LegRole.HEDGE_CALL,
    LegRole.SHORT_PUT,
    LegRole.SHORT_CALL,
)
HEDGE_ROLES = frozenset({LegRole.HEDGE_PUT, LegRole.HEDGE_CALL})
SHORT_ROLES = frozenset({LegRole.SHORT_PUT, LegRole.SHORT_CALL})

#: The hedge role that protects each short role, and vice versa — used to
#: enforce "no short exposure without its corresponding protective hedge"
#: and to find which hedge a roll must repair first.
_HEDGE_FOR_SHORT = {LegRole.SHORT_PUT: LegRole.HEDGE_PUT, LegRole.SHORT_CALL: LegRole.HEDGE_CALL}
_SHORT_FOR_HEDGE = {LegRole.HEDGE_PUT: LegRole.SHORT_PUT, LegRole.HEDGE_CALL: LegRole.SHORT_CALL}


def hedge_for(short_role: LegRole) -> LegRole:
    return _HEDGE_FOR_SHORT[short_role]


def short_for(hedge_role: LegRole) -> LegRole:
    return _SHORT_FOR_HEDGE[hedge_role]


class CycleAction(StrEnum):
    """What a :class:`CycleSignal` asks the engine to do."""

    NONE = "NONE"
    #: Begin (or resume) the sequential four-leg entry. ``legs`` carries all
    #: four original :class:`CycleLegIntent`\\ s, already contract-resolved
    #: by the strategy's own selection logic — the engine works through
    #: them in :data:`ENTRY_ROLE_ORDER`, one at a time, never re-deriving or
    #: re-ranking them mid-sequence.
    ENTER_CYCLE = "ENTER_CYCLE"
    #: Roll the untested short: close ``target_leg_id`` (the old short) and,
    #: once confirmed closed, open the one replacement in ``legs``.
    ROLL_SHORT = "ROLL_SHORT"
    #: Repair a deficient hedge before a risk-increasing replacement is
    #: permitted (spec section 7.2/12.3 step 2). ``legs`` carries the one
    #: replacement hedge.
    REPAIR_HEDGE = "REPAIR_HEDGE"
    #: Close every open/pending leg and block further action for the cycle.
    EXIT_ALL = "EXIT_ALL"
    #: Close exactly the leg named by ``target_leg_id``.
    EXIT_LEG = "EXIT_LEG"


@dataclass(frozen=True)
class CycleLegIntent:
    """One leg's desired shape, inside a :class:`CycleSignal`. Unlike
    :class:`~common.engine.multi_leg_models.LegIntent`, the contract is
    already resolved — entry/adjustment candidate *selection* (spec section
    3.5/7.2's deterministic ranking) is the strategy's own job, evaluated
    once against a single consistent quote/Greek snapshot; the engine never
    re-selects a strike mid-sequence."""

    role: LegRole
    side: OrderSide
    contract: OptionContract
    is_replacement: bool = False
    replaces_leg_id: str | None = None


@dataclass(frozen=True)
class CycleSignal:
    """Emitted by a positional strategy — the counterpart of
    :class:`~common.engine.multi_leg_models.BasketSignal`."""

    action: CycleAction
    timestamp: datetime
    legs: tuple[CycleLegIntent, ...] = ()
    reason: str = ""
    exit_reason: ExitReason | None = None
    target_leg_id: str | None = None
    #: Set only on ``ENTER_CYCLE`` — persisted onto the cycle at the
    #: pre-effect checkpoint, before the first order (spec section 3.7).
    original_wing_width: float | None = None
    #: Set only on ``ROLL_SHORT`` — the strategy's own delta computation at
    #: signal time (it already has the Greeks snapshot this evaluation
    #: used), persisted onto the append-only adjustment ledger for
    #: observability. ``None`` is honest, never fabricated, when the
    #: strategy could not compute one.
    pre_adjustment_net_delta: float | None = None
    post_adjustment_net_delta: float | None = None
    #: Set only on ``ENTER_CYCLE`` — the accepted basket-margin decision
    #: (spec section 3.7) the strategy computed for this exact candidate
    #: before returning the signal. Persisted onto
    #: ``strategy_cycle_margin_snapshots`` (migration 0011) at the same
    #: pre-effect checkpoint as the cycle/leg rows themselves, atomically
    #: with the critical entry decision — never a separate best-effort
    #: write. ``None`` is never produced by a strategy that requires a
    #: margin gate (spec 3.7 blocks entry on an unusable estimate before a
    #: signal is ever returned); it stays optional here only so a future
    #: positional strategy without a margin requirement is not forced to
    #: fabricate one.
    margin_estimate: MarginEstimate | None = None


@dataclass
class Cycle:
    """One logical positional cycle — one resolved expiry's worth of
    weekly delta-neutral (or any future positional strategy's) exposure.

    ``legs`` holds every leg instance this cycle has ever had, original and
    replacement alike, keyed by ``leg_id`` — nothing is ever removed, the
    same append-only-by-construction property
    :class:`~common.engine.multi_leg_models.Basket` already has.
    """

    cycle_id: str
    strategy_id: str
    execution_mode: Any  # common.config.models.ExecutionMode; Any avoids an import cycle
    underlying: str
    resolved_expiry_date: str
    opened_trading_date: str
    legs: dict[str, LegInstance] = field(default_factory=dict)
    state: CycleState = CycleState.ENTERING
    entries_consumed: bool = False
    day_blocked_reason: str | None = None
    #: B_original (spec section 3.7/6.1) — captured once, when the intended
    #: four-leg exposure is confirmed, and never rebased afterwards.
    original_net_credit: float | None = None
    original_max_loss: float | None = None
    original_wing_width: float | None = None
    adjustments_today: int = 0
    adjustments_today_date: str | None = None
    adjustments_this_cycle: int = 0
    last_adjustment_at: datetime | None = None
    pending_adjustment_role: LegRole | None = None
    pending_adjustment_state: str | None = None
    square_off_state: str = "PENDING"
    created_at: datetime | None = None

    # ------------------------------------------------------------- queries
    def open_legs(self) -> list[LegInstance]:
        return [leg for leg in self.legs.values() if leg.state is LegState.OPEN]

    def pending_legs(self) -> list[LegInstance]:
        return [leg for leg in self.legs.values() if leg.state in PENDING_LEG_STATES]

    def closed_legs(self) -> list[LegInstance]:
        return [leg for leg in self.legs.values() if leg.state is LegState.CLOSED]

    def unresolved_legs(self) -> list[LegInstance]:
        return [leg for leg in self.legs.values() if leg.state is LegState.CLOSE_SUBMISSION_UNKNOWN]

    def current_leg(self, role: LegRole) -> LegInstance | None:
        """The currently open, pending, or unresolved leg for ``role``, if
        any (at most one) — mirrors ``Basket.leg_by_role``."""
        for leg in self.legs.values():
            if leg.role is role and leg.state not in TERMINAL_LEG_STATES:
                return leg
        return None

    def original_leg(self, role: LegRole) -> LegInstance | None:
        """The sequence-1, non-replacement leg for ``role`` — the one
        :data:`ENTRY_ROLE_ORDER` progression tracks, regardless of whether
        it has since been rolled."""
        for leg in self.legs.values():
            if leg.role is role and leg.sequence == 1 and not leg.is_replacement:
                return leg
        return None

    def latest_leg(self, role: LegRole) -> LegInstance | None:
        """The highest-``sequence`` leg for ``role``, terminal or not —
        unlike :meth:`current_leg` (which deliberately excludes
        ``TERMINAL_LEG_STATES``), this is for restart-resume logic that
        must inspect a role's most recent leg *because* it may already be
        terminal (e.g. proving a pending adjustment's old short really did
        close before deciding how to resume it)."""
        candidates = [leg for leg in self.legs.values() if leg.role is role]
        if not candidates:
            return None
        return max(candidates, key=lambda leg: leg.sequence)

    def short_legs(self) -> list[LegInstance]:
        return [leg for leg in self.open_legs() if leg.role in SHORT_ROLES]

    def hedge_legs(self) -> list[LegInstance]:
        return [leg for leg in self.open_legs() if leg.role in HEDGE_ROLES]

    def next_sequence(self, role: LegRole) -> int:
        existing = [leg.sequence for leg in self.legs.values() if leg.role is role]
        return (max(existing) + 1) if existing else 1

    def next_leg_id(self, role: LegRole) -> str:
        return f"{self.cycle_id}:{role.value}:{self.next_sequence(role)}"

    # ----------------------------------------------------------------- P&L
    def realised_gross_pnl(self) -> float:
        """R — gross realised P&L from every closed leg instance, including
        adjusted-out legs (spec section 6.1)."""
        return sum(leg.realized_gross_pnl or 0.0 for leg in self.closed_legs())

    def unrealised_gross_pnl(self) -> float:
        """U — gross unrealised P&L from currently open leg instances only."""
        return sum(leg.unrealised_pnl for leg in self.open_legs())

    def total_gross_pnl(self) -> float:
        """T = R + U — fill-based, includes every closed adjustment leg
        (spec section 6.1); charges are netted separately by the caller from
        the authoritative ``positions``/``trade_ledger`` charges columns,
        never estimated here."""
        return self.realised_gross_pnl() + self.unrealised_gross_pnl()


def next_entry_role(cycle: Cycle) -> LegRole | None:
    """The role whose order still needs attempting (or retrying), in
    hedge-first order — the earliest non-``OPEN`` role, so a later role is
    never even considered while an earlier one remains pending: sequential,
    never parallel. ``None`` means entry is complete or blocked (the
    earliest non-``OPEN`` role has failed or is unresolved).

    Purely a function of persisted leg state — the entry "stage" is never
    stored as a separate value, so a restart re-derives exactly where it
    left off from :data:`ENTRY_ROLE_ORDER` and the legs table alone.
    """
    for role in ENTRY_ROLE_ORDER:
        leg = cycle.original_leg(role)
        if leg is None:
            return role
        if leg.state is LegState.OPEN:
            continue
        if leg.state in PENDING_LEG_STATES:
            # Still needs an order attempted — including a *retry* of this
            # same role (``_open_leg_now`` is designed to be called again on
            # a still-PENDING_ORDER leg once a fresh quote arrives; every
            # role starts PENDING_ORDER the instant _enter_cycle creates its
            # row, before any order has actually been attempted, so treating
            # PENDING as "already in flight, skip it" would mean no role's
            # first attempt could ever happen).
            return role
        return None  # FAILED / CLOSE_SUBMISSION_UNKNOWN / anything else: blocked
    return None


def entry_is_complete(cycle: Cycle) -> bool:
    return all(
        (leg := cycle.original_leg(role)) is not None and leg.state is LegState.OPEN
        for role in ENTRY_ROLE_ORDER
    )


def entry_has_blocking_leg(cycle: Cycle) -> bool:
    """A role that reached a state entry can never safely proceed past —
    FAILED (contract/order genuinely refused) or CLOSE_SUBMISSION_UNKNOWN
    (an in-flight order whose outcome cannot be established)."""
    for role in ENTRY_ROLE_ORDER:
        leg = cycle.original_leg(role)
        if leg is not None and leg.state in (LegState.FAILED, LegState.CLOSE_SUBMISSION_UNKNOWN):
            return True
    return False
