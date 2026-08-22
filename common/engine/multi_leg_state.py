"""Durable basket/leg persistence — the bridge between :mod:`common.engine.
multi_leg_models` and the ``strategy_baskets``/``strategy_legs`` tables
(migration ``0009``).

Sibling to :mod:`common.engine.state_payload`, for the multi-leg engine's
generic durable state rather than the single-leg engine's JSON blob. Unlike
``state_payload``, this module's tables are the *principal* source of basket
lifecycle truth (see ``multi_leg_models.py``'s own docstring on ownership) —
not a checkpoint alongside ``positions``, because a leg with no fill yet has no
``positions`` row to check against at all.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from common.logging import get_logger
from common.models import ExitReason, OptionType, OrderSide

from .models import OptionContract
from .multi_leg_models import (
    AdjustmentTarget,
    AnchorUpdate,
    Basket,
    BasketRollState,
    LegInstance,
    LegRole,
    LegState,
    RollClaim,
)

if TYPE_CHECKING:
    from common.config.models import ExecutionMode
    from common.execution.repository import BasketRollClaimSeed, ExecutionRepository

log = get_logger(__name__)


def persist_basket(
    repository: ExecutionRepository,
    basket: Basket,
    *,
    runtime_id: str,
) -> None:
    repository.upsert_strategy_basket(
        runtime_id=runtime_id,
        strategy_id=basket.strategy_id,
        execution_mode=basket.execution_mode,
        trading_date=basket.trading_date,
        basket_id=basket.basket_id,
        lifecycle_state=basket.lifecycle_state,
        entries_consumed=basket.entries_consumed,
        day_blocked_reason=basket.day_blocked_reason,
        adjustment_count=basket.adjustment_count,
        pending_replacement_role=(
            basket.pending_replacement_role.value
            if basket.pending_replacement_role is not None
            else None
        ),
        pending_replacement_state=basket.pending_replacement_state,
        original_combined_basis=basket.original_combined_basis,
        square_off_state=basket.square_off_state,
    )


def persist_leg(
    repository: ExecutionRepository,
    leg: LegInstance,
    *,
    runtime_id: str,
    strategy_id: str,
    execution_mode: ExecutionMode,
    trading_date: str,
) -> None:
    contract = leg.contract
    repository.upsert_strategy_leg(
        runtime_id=runtime_id,
        strategy_id=strategy_id,
        execution_mode=execution_mode,
        trading_date=trading_date,
        basket_id=leg.basket_id,
        leg_id=leg.leg_id,
        leg_role=leg.role.value,
        leg_sequence=leg.sequence,
        is_replacement=leg.is_replacement,
        replaces_leg_id=leg.replaces_leg_id,
        security_id=contract.security_id if contract is not None else None,
        symbol=contract.symbol if contract is not None else None,
        strike=contract.strike if contract is not None else None,
        expiry=contract.expiry if contract is not None else None,
        lot_size=contract.lot_size if contract is not None else None,
        side=leg.side.value,
        quantity=leg.quantity or None,
        entry_price=leg.entry_price,
        entry_time=leg.entry_time.isoformat() if leg.entry_time is not None else None,
        entry_correlation_id=leg.entry_correlation_id,
        exit_price=leg.exit_price,
        exit_time=leg.exit_time.isoformat() if leg.exit_time is not None else None,
        exit_reason=leg.exit_reason.value if leg.exit_reason is not None else None,
        exit_correlation_id=leg.exit_correlation_id,
        realized_gross_pnl=leg.realized_gross_pnl,
        state=leg.state.value,
    )


class BasketRowInconsistent(RuntimeError):
    """A ``strategy_baskets``/``strategy_legs`` row could not be rebuilt into a
    consistent in-memory :class:`Basket`. Caller must treat this the same way
    :func:`~common.engine.multi_leg_models.UnmanageableBasketState` is treated:
    fail closed, never guess."""


def load_basket(
    repository: ExecutionRepository,
    *,
    strategy_id: str,
    execution_mode: ExecutionMode,
    trading_date: str,
) -> Basket | None:
    """Rebuild the durable :class:`Basket` (with every leg instance) for one
    strategy-day, or ``None`` if nothing was ever persisted for it.

    Raises :class:`BasketRowInconsistent` on any row this build cannot safely
    interpret (an unknown enum value, an OPEN leg with no contract/entry
    price) — never silently drops or guesses a field. The caller
    (``recover_basket`` in ``multi_leg_engine_worker.py``) is responsible for
    turning that into the engine's fail-closed restart posture.
    """
    basket_row = repository.load_strategy_basket(
        strategy_id=strategy_id, execution_mode=execution_mode, trading_date=trading_date
    )
    if basket_row is None:
        return None

    try:
        pending_role = (
            LegRole(basket_row["pending_replacement_role"])
            if basket_row["pending_replacement_role"]
            else None
        )
    except ValueError as exc:
        raise BasketRowInconsistent(
            f"strategy_baskets.pending_replacement_role={basket_row['pending_replacement_role']!r} "
            "is not a recognised LegRole"
        ) from exc

    basket = Basket(
        basket_id=basket_row["basket_id"],
        strategy_id=strategy_id,
        execution_mode=execution_mode,
        trading_date=trading_date,
        adjustment_count=int(basket_row["adjustment_count"]),
        pending_replacement_role=pending_role,
        pending_replacement_state=basket_row["pending_replacement_state"],
        original_combined_basis=(
            float(basket_row["original_combined_basis"])
            if basket_row["original_combined_basis"] is not None
            else None
        ),
        entries_consumed=bool(basket_row["entries_consumed"]),
        day_blocked_reason=basket_row["day_blocked_reason"],
        square_off_state=basket_row["square_off_state"],
        lifecycle_state=basket_row["lifecycle_state"],
    )

    for row in repository.load_strategy_legs(
        strategy_id=strategy_id, execution_mode=execution_mode, trading_date=trading_date
    ):
        leg = _row_to_leg(row)
        basket.legs[leg.leg_id] = leg

    basket.roll_state = load_basket_roll_state(
        repository,
        strategy_id=strategy_id,
        execution_mode=execution_mode,
        trading_date=trading_date,
        basket_id=basket.basket_id,
    )

    return basket


def load_basket_roll_state(
    repository: ExecutionRepository,
    *,
    strategy_id: str,
    execution_mode: ExecutionMode,
    trading_date: str,
    basket_id: str,
) -> BasketRollState:
    """Rebuild the durable :class:`BasketRollState` (migration ``0013``) for
    one basket — the shared reference spot plus every roll claim ever made,
    across every role.

    Never returns ``None``: a basket with no roll history (including every
    ``straddle_920`` basket, which does not use this table) gets an empty
    :class:`BasketRollState` with ``reference_price=None`` and no claims, so
    a strategy reading a loaded basket never has to distinguish "never
    loaded" from "loaded, nothing yet" — see :attr:`~.multi_leg_models.
    Basket.roll_state`'s own docstring.

    Raises :class:`BasketRowInconsistent` on an unrecognised ``leg_role`` —
    never silently drops or guesses (same discipline as :func:`load_basket`
    itself). Deliberately does **not** validate ``lifecycle_state`` against
    a closed set: :class:`~.multi_leg_models.RollClaim` keeps it as a plain
    ``str`` precisely so a value this code does not yet recognise (a future
    vocabulary extension) is still representable rather than raising —
    see that class's own docstring.
    """
    anchor_row = repository.load_basket_roll_anchor(
        strategy_id=strategy_id,
        execution_mode=execution_mode,
        trading_date=trading_date,
        basket_id=basket_id,
    )
    reference_price = (
        float(anchor_row["reference_price"])
        if anchor_row is not None and anchor_row["reference_price"] is not None
        else None
    )
    anchor_candle_ts = (
        datetime.fromisoformat(anchor_row["anchor_candle_ts"])
        if anchor_row is not None and anchor_row["anchor_candle_ts"]
        else None
    )

    claims = tuple(
        _row_to_roll_claim(row) for row in repository.load_basket_rolls(basket_id=basket_id)
    )

    return BasketRollState(
        reference_price=reference_price,
        anchor_candle_ts=anchor_candle_ts,
        claims=claims,
    )


def _row_to_roll_claim(row) -> RollClaim:  # type: ignore[no-untyped-def]
    try:
        role = LegRole(row["leg_role"])
    except ValueError as exc:
        raise BasketRowInconsistent(
            f"strategy_basket_rolls row (basket_id={row['basket_id']!r}, "
            f"roll_sequence={row['roll_sequence']!r}) has leg_role={row['leg_role']!r}, "
            "not a recognised LegRole"
        ) from exc

    return RollClaim(
        claim_group_id=row["claim_group_id"],
        leg_role=role,
        roll_sequence=int(row["roll_sequence"]),
        lifecycle_state=row["lifecycle_state"],
        target_leg_id=row["target_leg_id"],
        close_correlation_id=row["close_correlation_id"],
        close_intent_id=(
            int(row["close_intent_id"]) if row["close_intent_id"] is not None else None
        ),
        replacement_leg_id=row["replacement_leg_id"],
        reference_price_at_claim=(
            float(row["reference_price_at_claim"])
            if row["reference_price_at_claim"] is not None
            else None
        ),
        claim_candle_ts=datetime.fromisoformat(row["claim_candle_ts"]),
        claimed_at=datetime.fromisoformat(row["claimed_at"]),
    )


def _row_to_leg(row) -> LegInstance:  # type: ignore[no-untyped-def]
    try:
        role = LegRole(row["leg_role"])
        state = LegState(row["state"])
        side = OrderSide(row["side"]) if row["side"] else OrderSide.SELL
    except ValueError as exc:
        raise BasketRowInconsistent(
            f"strategy_legs row {row['leg_id']!r} is unusable: {exc}"
        ) from exc

    contract: OptionContract | None = None
    if row["security_id"]:
        option_type = _ROLE_TO_OPTION_TYPE.get(role)
        if option_type is None:
            raise BasketRowInconsistent(
                f"strategy_legs row {row['leg_id']!r} has role {role.value} with no known "
                "OptionType mapping but carries a contract"
            )
        if row["strike"] is None or row["expiry"] is None or row["lot_size"] is None:
            raise BasketRowInconsistent(
                f"strategy_legs row {row['leg_id']!r} has a security_id but an incomplete "
                "contract record (strike/expiry/lot_size)"
            )
        contract = OptionContract(
            symbol=row["symbol"] or row["security_id"],
            security_id=row["security_id"],
            strike=float(row["strike"]),
            option_type=option_type,
            expiry=row["expiry"],
            lot_size=int(row["lot_size"]),
        )

    if state is LegState.OPEN and (contract is None or row["entry_price"] is None):
        raise BasketRowInconsistent(
            f"strategy_legs row {row['leg_id']!r} is OPEN but has no usable contract/entry price"
        )

    exit_reason = ExitReason(row["exit_reason"]) if row["exit_reason"] else None

    return LegInstance(
        leg_id=row["leg_id"],
        basket_id=row["basket_id"],
        role=role,
        sequence=int(row["leg_sequence"]),
        is_replacement=bool(row["is_replacement"]),
        side=side,
        quantity=int(row["quantity"]) if row["quantity"] is not None else 0,
        contract=contract,
        state=state,
        entry_price=row["entry_price"],
        entry_time=datetime.fromisoformat(row["entry_time"]) if row["entry_time"] else None,
        entry_correlation_id=row["entry_correlation_id"],
        last_price=row["entry_price"],
        exit_price=row["exit_price"],
        exit_time=datetime.fromisoformat(row["exit_time"]) if row["exit_time"] else None,
        exit_reason=exit_reason,
        exit_correlation_id=row["exit_correlation_id"],
        realized_gross_pnl=row["realized_gross_pnl"],
        replaces_leg_id=row["replaces_leg_id"],
    )


_ROLE_TO_OPTION_TYPE = {LegRole.CE: OptionType.CE, LegRole.PE: OptionType.PE}


class RollLedger:
    """The production :class:`~common.engine.multi_leg_models.RollLedgerPort`
    (migration ``0013``, Phase 2) — backed by :class:`ExecutionRepository`,
    generic to any multi-leg strategy. A worker wires one instance into
    ``MultiLegEngine(roll_ledger=...)`` regardless of which strategy it is
    running; an offline/test engine that never wires one gets the
    unchanged single-target fallback (see the port's own docstring).
    """

    def __init__(
        self,
        repository: ExecutionRepository,
        *,
        runtime_id: str,
        strategy_id: str,
        execution_mode: ExecutionMode,
        trading_date: str,
    ) -> None:
        self._repository = repository
        self._runtime_id = runtime_id
        self._strategy_id = strategy_id
        self._execution_mode = execution_mode
        self._trading_date = trading_date

    def commit_claims(
        self,
        basket: Basket,
        *,
        claim_group_id: str,
        targets: tuple[AdjustmentTarget, ...],
        anchor: AnchorUpdate | None,
        claim_candle_ts: datetime,
        claimed_at: datetime,
    ) -> dict[str, int]:
        from common.execution.repository import BasketRollClaimSeed

        roll_state = basket.roll_state
        reference_price = anchor.price if anchor is not None else (
            roll_state.reference_price if roll_state is not None else None
        )
        seen_this_call: dict[LegRole, int] = {}
        seeds: list[BasketRollClaimSeed] = []
        assigned: dict[str, int] = {}
        for target in targets:
            already = roll_state.roll_count(target.role) if roll_state is not None else 0
            bump = seen_this_call.get(target.role, 0) + 1
            seen_this_call[target.role] = bump
            roll_sequence = already + bump
            assigned[target.leg_id] = roll_sequence
            seeds.append(
                BasketRollClaimSeed(
                    claim_group_id=claim_group_id,
                    leg_role=target.role.value,
                    roll_sequence=roll_sequence,
                    target_leg_id=target.leg_id,
                    reference_price_at_claim=reference_price,
                    claim_candle_ts=claim_candle_ts.isoformat(),
                    claimed_at=claimed_at.isoformat(),
                )
            )

        # Mirrors persist_basket's own field mapping (duplicated rather than
        # reused: persist_basket owns a single, already-tested transaction
        # of its own — upsert_strategy_basket — and must stay untouched;
        # this call instead needs that same row written atomically alongside
        # the anchor/claims below, via commit_basket_state).
        self._repository.commit_basket_state(
            runtime_id=self._runtime_id,
            strategy_id=self._strategy_id,
            execution_mode=self._execution_mode,
            trading_date=self._trading_date,
            basket_id=basket.basket_id,
            lifecycle_state=basket.lifecycle_state,
            entries_consumed=basket.entries_consumed,
            day_blocked_reason=basket.day_blocked_reason,
            adjustment_count=basket.adjustment_count,
            pending_replacement_role=(
                basket.pending_replacement_role.value
                if basket.pending_replacement_role is not None
                else None
            ),
            pending_replacement_state=basket.pending_replacement_state,
            original_combined_basis=basket.original_combined_basis,
            square_off_state=basket.square_off_state,
            anchor=(
                (anchor.price, anchor.candle_ts.isoformat()) if anchor is not None else None
            ),
            new_claims=tuple(seeds),
        )
        return assigned

    def record_outcome(
        self,
        *,
        basket_id: str,
        leg_role: LegRole,
        roll_sequence: int,
        lifecycle_state: str,
        replacement_leg_id: str | None = None,
    ) -> None:
        self._repository.update_basket_roll_outcome(
            basket_id=basket_id,
            leg_role=leg_role.value,
            roll_sequence=roll_sequence,
            lifecycle_state=lifecycle_state,
            replacement_leg_id=replacement_leg_id,
        )

    def resolve_close_intent(self, close_intent_id: int) -> str:
        from common.execution.repository import resolve_intent_outcome

        row = self._repository.order_intent_by_id(close_intent_id)
        return resolve_intent_outcome(row)

    def consume_group_replacement(
        self, *, basket_id: str, members: tuple[tuple[LegRole, int], ...]
    ) -> None:
        self._repository.consume_group_replacement(
            basket_id=basket_id,
            members=tuple((role.value, roll_sequence) for role, roll_sequence in members),
        )
