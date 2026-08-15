"""Durable cycle/leg persistence — the bridge between
:mod:`common.engine.positional.positional_models` and the
``strategy_cycles``/``strategy_cycle_legs`` tables (migration 0010).

Sibling to :mod:`common.engine.multi_leg_state`, for the positional engine's
generic durable state. Owns the *positional-only* leg-role <-> option-type
mapping (``SHORT_CALL``/``HEDGE_CALL`` -> CE, ``SHORT_PUT``/``HEDGE_PUT`` ->
PE) — deliberately never added to :mod:`common.engine.multi_leg_engine` or
:mod:`common.engine.multi_leg_state`'s own mappings, which stay exactly
``{CE: CE, PE: PE}`` (see ``LegRole``'s own docstring and
``tests/unit/test_leg_role_extension_is_additive.py``).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from common.logging import get_logger
from common.models import ExitReason, OptionType, OrderSide

from ..models import OptionContract
from .positional_models import Cycle, LegInstance, LegRole, LegState

if TYPE_CHECKING:
    from common.config.models import ExecutionMode
    from common.execution.repository import ExecutionRepository

log = get_logger(__name__)

#: The positional engine's own role -> option-type mapping. Never merged
#: into common.engine.multi_leg_engine's or multi_leg_state's identically-
#: named private dicts — those must stay structurally unable to resolve
#: these four roles.
ROLE_TO_OPTION_TYPE: dict[LegRole, OptionType] = {
    LegRole.SHORT_CALL: OptionType.CE,
    LegRole.HEDGE_CALL: OptionType.CE,
    LegRole.SHORT_PUT: OptionType.PE,
    LegRole.HEDGE_PUT: OptionType.PE,
}


class CycleRowInconsistent(RuntimeError):
    """A ``strategy_cycles``/``strategy_cycle_legs`` row could not be
    rebuilt into a consistent in-memory :class:`Cycle`. The caller must
    treat this the same way
    :class:`~common.engine.multi_leg_models.UnmanageableBasketState` is
    treated: fail closed, never guess.
    """


def persist_cycle(repository: ExecutionRepository, cycle: Cycle, *, runtime_id: str) -> None:
    repository.upsert_cycle(
        runtime_id=runtime_id,
        strategy_id=cycle.strategy_id,
        execution_mode=cycle.execution_mode,
        cycle_id=cycle.cycle_id,
        underlying=cycle.underlying,
        resolved_expiry_date=cycle.resolved_expiry_date,
        state=cycle.state.value,
        entries_consumed=cycle.entries_consumed,
        day_blocked_reason=cycle.day_blocked_reason,
        original_net_credit=cycle.original_net_credit,
        original_max_loss=cycle.original_max_loss,
        original_wing_width=cycle.original_wing_width,
        adjustments_today=cycle.adjustments_today,
        adjustments_today_date=cycle.adjustments_today_date,
        adjustments_this_cycle=cycle.adjustments_this_cycle,
        last_adjustment_at=(
            cycle.last_adjustment_at.isoformat() if cycle.last_adjustment_at is not None else None
        ),
        pending_adjustment_role=(
            cycle.pending_adjustment_role.value
            if cycle.pending_adjustment_role is not None
            else None
        ),
        pending_adjustment_state=cycle.pending_adjustment_state,
        square_off_state=cycle.square_off_state,
        opened_trading_date=cycle.opened_trading_date,
    )


def persist_cycle_leg(
    repository: ExecutionRepository,
    leg: LegInstance,
    *,
    runtime_id: str,
    strategy_id: str,
    execution_mode: ExecutionMode,
    cycle_id: str,
) -> None:
    contract = leg.contract
    option_type = ROLE_TO_OPTION_TYPE.get(leg.role)
    repository.upsert_cycle_leg(
        runtime_id=runtime_id,
        strategy_id=strategy_id,
        execution_mode=execution_mode,
        cycle_id=cycle_id,
        leg_id=leg.leg_id,
        leg_role=leg.role.value,
        option_type=option_type.value if option_type is not None else None,
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


def load_cycle(
    repository: ExecutionRepository,
    *,
    runtime_id: str,
    strategy_id: str,
    execution_mode: ExecutionMode,
) -> Cycle | None:
    """Rebuild the one non-terminal cycle for this strategy/mode (with every
    leg instance), or ``None`` if none is open.

    Raises :class:`CycleRowInconsistent` on any row this build cannot safely
    interpret — never silently drops or guesses a field. The caller
    (``recover_cycle`` in the positional runtime worker) turns that into the
    engine's fail-closed restart posture.
    """
    cycle_row = repository.load_open_cycle(
        runtime_id=runtime_id, strategy_id=strategy_id, execution_mode=execution_mode
    )
    if cycle_row is None:
        return None

    try:
        pending_role = (
            LegRole(cycle_row["pending_adjustment_role"])
            if cycle_row["pending_adjustment_role"]
            else None
        )
    except ValueError as exc:
        raise CycleRowInconsistent(
            f"strategy_cycles.pending_adjustment_role={cycle_row['pending_adjustment_role']!r} "
            "is not a recognised LegRole"
        ) from exc

    cycle = Cycle(
        cycle_id=cycle_row["cycle_id"],
        strategy_id=strategy_id,
        execution_mode=execution_mode,
        underlying=cycle_row["underlying"],
        resolved_expiry_date=cycle_row["resolved_expiry_date"],
        opened_trading_date=cycle_row["opened_trading_date"],
        state=_parse_cycle_state(cycle_row["state"]),
        entries_consumed=bool(cycle_row["entries_consumed"]),
        day_blocked_reason=cycle_row["day_blocked_reason"],
        original_net_credit=cycle_row["original_net_credit"],
        original_max_loss=cycle_row["original_max_loss"],
        original_wing_width=cycle_row["original_wing_width"],
        adjustments_today=int(cycle_row["adjustments_today"]),
        adjustments_today_date=cycle_row["adjustments_today_date"],
        adjustments_this_cycle=int(cycle_row["adjustments_this_cycle"]),
        last_adjustment_at=(
            datetime.fromisoformat(cycle_row["last_adjustment_at"])
            if cycle_row["last_adjustment_at"]
            else None
        ),
        pending_adjustment_role=pending_role,
        pending_adjustment_state=cycle_row["pending_adjustment_state"],
        square_off_state=cycle_row["square_off_state"],
    )

    for row in repository.load_cycle_legs(cycle_id=cycle.cycle_id):
        leg = _row_to_leg(row)
        cycle.legs[leg.leg_id] = leg

    return cycle


def _parse_cycle_state(raw: str):  # type: ignore[no-untyped-def]
    from .positional_models import CycleState

    try:
        return CycleState(raw)
    except ValueError as exc:
        raise CycleRowInconsistent(
            f"strategy_cycles.state={raw!r} is not a recognised CycleState"
        ) from exc


def _row_to_leg(row) -> LegInstance:  # type: ignore[no-untyped-def]
    try:
        role = LegRole(row["leg_role"])
        state = LegState(row["state"])
        side = OrderSide(row["side"]) if row["side"] else OrderSide.SELL
    except ValueError as exc:
        raise CycleRowInconsistent(
            f"strategy_cycle_legs row {row['leg_id']!r} is unusable: {exc}"
        ) from exc

    contract: OptionContract | None = None
    if row["security_id"]:
        option_type = ROLE_TO_OPTION_TYPE.get(role)
        if option_type is None:
            raise CycleRowInconsistent(
                f"strategy_cycle_legs row {row['leg_id']!r} has role {role.value} with no known "
                "OptionType mapping but carries a contract"
            )
        if row["strike"] is None or row["expiry"] is None or row["lot_size"] is None:
            raise CycleRowInconsistent(
                f"strategy_cycle_legs row {row['leg_id']!r} has a security_id but an incomplete "
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
        raise CycleRowInconsistent(
            f"strategy_cycle_legs row {row['leg_id']!r} is OPEN but has no usable "
            "contract/entry price"
        )

    exit_reason = ExitReason(row["exit_reason"]) if row["exit_reason"] else None

    return LegInstance(
        leg_id=row["leg_id"],
        basket_id=row["cycle_id"],
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
