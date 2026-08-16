"""Strategy loading and restart reconciliation for the positional multi-leg
engine — the positional counterpart of ``runtimes.intraday_options.
multi_leg_engine_worker``'s ``load_multi_leg_strategy``/``recover_basket``/
``_reconcile_basket``.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from common.config.models import ExecutionMode
from common.engine.positional.positional_models import (
    Cycle,
    LegState,
    UnmanageableCycleState,
)
from common.engine.positional.positional_state import CycleRowInconsistent
from common.engine.positional.positional_state import load_cycle as _load_cycle
from common.engine.positional.positional_state import persist_cycle_leg as _persist_cycle_leg
from common.engine.positional.positional_strategy import BasePositionalMultiLegStrategy
from common.execution import ExecutionRepository
from common.logging import get_logger
from common.models import OrderSide

from .config_adapter import WorkerConfig

log = get_logger(__name__)


def load_positional_strategy(
    strategy_ref: str, kwargs: dict[str, Any]
) -> BasePositionalMultiLegStrategy:
    """Resolve a ``"package.module:ClassName"`` reference and construct it —
    mirrors ``engine_worker.load_strategy``/``multi_leg_engine_worker.
    load_multi_leg_strategy`` exactly, against the positional base class."""
    module_name, separator, class_name = strategy_ref.partition(":")
    if not separator or not module_name or not class_name:
        raise ValueError(f"strategy_ref must be 'package.module:ClassName', got {strategy_ref!r}")
    module = import_module(module_name)
    try:
        factory = getattr(module, class_name)
    except AttributeError as exc:
        raise ValueError(f"{module_name!r} has no attribute {class_name!r}") from exc
    strategy = factory(**kwargs)
    if not isinstance(strategy, BasePositionalMultiLegStrategy):
        raise TypeError(
            f"{strategy_ref!r} resolved to {type(strategy).__name__}, which is not a "
            "BasePositionalMultiLegStrategy; the positional engine cannot drive it."
        )
    return strategy


def recover_cycle(config: WorkerConfig, repository: ExecutionRepository) -> Cycle | None:
    """Rebuild the one non-terminal cycle this strategy/mode has, or
    ``None`` — and *reconcile* it against authoritative order/position
    state, mirroring ``multi_leg_engine_worker.recover_basket``'s
    conservative posture: any row/state this cannot safely interpret raises
    :class:`~common.engine.positional.positional_models.UnmanageableCycleState`
    rather than guessing.
    """
    try:
        cycle = _load_cycle(
            repository,
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=ExecutionMode.PAPER,
        )
    except CycleRowInconsistent as exc:
        _record_recovery_failure(config, repository, str(exc))
        raise UnmanageableCycleState(str(exc)) from exc
    if cycle is None:
        return None

    mismatches = _reconcile_cycle(repository, cycle, config)
    if mismatches:
        detail = "; ".join(mismatches)
        _record_recovery_failure(config, repository, detail)
        raise UnmanageableCycleState(detail)
    return cycle


def _reconcile_cycle(
    repository: ExecutionRepository, cycle: Any, config: WorkerConfig
) -> list[str]:
    """Cross-check ``cycle``'s projected leg states against the
    authoritative ``order_intents``/``orders``/``positions``/
    ``cycle_position_bindings`` tables, correcting in place wherever the
    authoritative data can establish the true state (mirrors
    ``multi_leg_engine_worker._reconcile_basket`` exactly, with one
    addition: a missing, duplicate, or contradictory
    ``cycle_position_bindings`` row is itself a fail-closed mismatch — spec
    review correction 4)."""
    open_positions = {
        p.security_id: p for p in repository.open_positions_for_cycle(cycle_id=cycle.cycle_id)
    }
    claimed_security_ids: dict[str, str] = {}
    mismatches: list[str] = []

    for leg in cycle.legs.values():
        original_state = leg.state
        result = _reconcile_leg(repository, leg, open_positions)
        if leg.state is not original_state:
            # Reconciliation corrected an in-memory-only projection gap
            # (spec section 9.4: "repaired by reconciliation" must mean
            # durably repaired, not merely correct for the lifetime of
            # this one process — a second restart before this leg is ever
            # touched again must not have to re-derive the same
            # correction from authoritative order/position history every
            # time, and any other reader of ``strategy_cycle_legs``
            # — a dashboard, an audit query — must see the true state
            # too). Persisted with the same best-effort semantics
            # ``PositionalMultiLegEngine._persist_leg`` itself uses
            # elsewhere: a failure here is recorded, never raised — the
            # in-memory correction this session runs on is authoritative
            # either way.
            try:
                _persist_cycle_leg(
                    repository,
                    leg,
                    runtime_id=config.runtime_id,
                    strategy_id=config.strategy_id,
                    execution_mode=ExecutionMode.PAPER,
                    cycle_id=leg.basket_id,
                )
            except Exception as exc:
                log.exception(
                    "%s: could not durably persist restart reconciliation's own "
                    "correction for leg %s",
                    config.strategy_id,
                    leg.leg_id,
                )
                _record_recovery_failure(
                    config,
                    repository,
                    f"leg {leg.leg_id} reconciled to {leg.state.value} in memory but the "
                    f"durable correction failed: {exc}",
                )
        if result is not None:
            mismatches.append(result)
            continue
        if leg.state is LegState.OPEN and leg.contract is not None:
            sid = leg.contract.security_id
            other = claimed_security_ids.get(sid)
            if other is not None:
                mismatches.append(
                    f"legs {other!r} and {leg.leg_id!r} are both OPEN against the same "
                    f"position {sid} — duplicate open leg mapping"
                )
            else:
                claimed_security_ids[sid] = leg.leg_id

    for security_id in open_positions:
        if security_id not in claimed_security_ids:
            mismatches.append(
                f"position {security_id} is OPEN but no leg in cycle {cycle.cycle_id!r} claims it"
            )

    return mismatches


def _reconcile_leg(
    repository: ExecutionRepository, leg: Any, open_positions: dict[str, Any]
) -> str | None:
    history = repository.cycle_order_history(cycle_id=leg.basket_id)
    leg_history = [row for row in history if row["leg_id"] == leg.leg_id]
    entry_side = leg.side.value
    entry_rows = [r for r in leg_history if r["side"] == entry_side]
    exit_rows = [r for r in leg_history if r["side"] != entry_side]
    if len(entry_rows) > 1:
        return f"leg {leg.leg_id!r} has {len(entry_rows)} entry order_intents rows (expected <= 1)"
    if len(exit_rows) > 1:
        return f"leg {leg.leg_id!r} has {len(exit_rows)} exit order_intents rows (expected <= 1)"
    entry_row = entry_rows[0] if entry_rows else None
    entry_outcome = _resolve_intent_outcome(entry_row)
    position = open_positions.get(leg.contract.security_id) if leg.contract is not None else None

    if leg.state is LegState.OPEN:
        if entry_outcome != "FILLED":
            return (
                f"leg {leg.leg_id!r} is OPEN in the projection but its entry order "
                f"resolves to {entry_outcome}, not a confirmed fill"
            )
        if position is None:
            # The projection still says OPEN, but no authoritative open
            # position exists. Before failing closed, check whether a
            # confirmed *exit* fill explains it — a real close that
            # succeeded (the broker-authoritative order/fill/position
            # writes all committed) but whose own leg-state persistence
            # never landed, e.g. a crash between _close_leg's authoritative
            # positions.close() call and its own best-effort _persist_leg
            # (spec section 9.4: "post-effect projection failures must
            # generate incidents and be repaired by reconciliation, not
            # pretend the trade did not occur"). Reconstructed from the
            # authoritative order_intents/orders fill row alone —
            # exit_time/exit_reason are left unknown (None) rather than
            # fabricated; realized_gross_pnl is computed from the leg's
            # own (already-correct, untouched) entry_price and this
            # reconstructed exit price, the same formula
            # LegInstance.unrealised_pnl already uses for the open case.
            exit_row = exit_rows[0] if exit_rows else None
            exit_outcome = _resolve_intent_outcome(exit_row)
            reconstructed_price = (
                exit_row["order_average_fill_price"] if exit_row is not None else None
            )
            if exit_outcome == "FILLED" and reconstructed_price is not None:
                exit_price = float(reconstructed_price)
                leg.state = LegState.CLOSED
                leg.exit_price = exit_price
                leg.exit_correlation_id = exit_row["correlation_id"] if exit_row else None
                if leg.entry_price is not None:
                    leg.realized_gross_pnl = (
                        (leg.entry_price - exit_price)
                        if leg.side is OrderSide.SELL
                        else (exit_price - leg.entry_price)
                    ) * leg.quantity
                return None
            return f"leg {leg.leg_id!r} is OPEN but no matching OPEN position exists"
        return None

    if leg.state is LegState.CLOSE_SUBMISSION_UNKNOWN:
        exit_row = exit_rows[0] if exit_rows else None
        exit_outcome = _resolve_intent_outcome(exit_row)
        if exit_outcome == "FILLED" and position is None:
            leg.state = LegState.CLOSED
            return None
        if position is not None:
            leg.state = LegState.OPEN
            return None
        return (
            f"leg {leg.leg_id!r} has an unresolved close (exit_outcome={exit_outcome}) and no "
            "authoritative position to fall back on"
        )

    pending_states = (
        LegState.PENDING_CONTRACT,
        LegState.PENDING_SUBSCRIPTION,
        LegState.PENDING_ORDER,
    )
    if leg.state in pending_states:
        if entry_outcome == "FILLED":
            if position is None:
                return f"leg {leg.leg_id!r}'s entry order filled but no matching position exists"
            leg.state = LegState.OPEN
            leg.entry_price = position.average_price
            leg.quantity = position.quantity
            leg.entry_correlation_id = position.entry_correlation_id
        return None

    return None


def _resolve_intent_outcome(row: Any) -> str:
    if row is None:
        return "NEVER_PLACED"
    status = row["order_status"]
    if status is None:
        return "UNKNOWN"
    if status == "FILLED":
        return "FILLED"
    if status in ("REJECTED", "CANCELLED", "EXPIRED"):
        return "TERMINAL_NO_FILL"
    return "UNKNOWN"


def _record_recovery_failure(
    config: WorkerConfig, repository: ExecutionRepository, detail: str
) -> None:
    log.critical("positional restart recovery failed for %s: %s", config.strategy_id, detail)
    repository.record_error(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=ExecutionMode.PAPER,
        severity="CRITICAL",
        component="positional_multi_leg_engine.recovery",
        message=detail,
    )
