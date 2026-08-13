"""Mode-transition safety (spec 366-373, "Mode-change rules").

    Reject paper -> live while an open paper position exists.
    Reject live -> paper or live -> disabled while broker positions or
    pending live orders exist.

Mode changes already take effect only after a worker restart structurally
(``ExecutionMode`` is immutable for a worker session — nothing in this
codebase re-reads config mid-run). What was missing is the restart-time
refusal itself: this module is that refusal, called once, at admission
time, before a worker for the new mode is ever started.

Two asymmetric checks, because the two directions have asymmetric proof
requirements:

* **paper -> live**: a pure local-database question — every trading date
  ever recorded, not just today (:meth:`~common.execution.repository
  .ExecutionRepository.open_positions_all_dates`).
* **live -> paper/disabled**: cannot be answered from the local database
  alone. Local records only prove what *this process* believes happened;
  the broker is authoritative for whether a position or order still
  genuinely exists. This direction requires a **fresh** reconciliation run
  (never a cached one) and refuses outright if no ``broker``/
  ``reconciliation_runner`` were supplied — "cannot prove this is safe" and
  "proven unsafe" are both refusals here, never treated differently.
"""

from __future__ import annotations

from dataclasses import dataclass

from common.broker.base import Broker
from common.config.models import ExecutionMode
from common.logging import get_logger
from common.models import OrderStatus
from common.reconciliation import LocalOrderState, LocalPositionState, ReconciliationRunner

from .repository import ExecutionRepository

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ModeTransitionDecision:
    allowed: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.allowed


def check_mode_transition_safety(
    repository: ExecutionRepository,
    *,
    strategy_id: str,
    runtime_id: str,
    new_mode: ExecutionMode | None,
    broker: Broker | None,
    reconciliation_runner: ReconciliationRunner | None,
) -> ModeTransitionDecision:
    """Decide whether ``strategy_id`` may admit as ``new_mode`` this
    restart. ``new_mode=None`` means the strategy is disabled this
    session — no worker starts for it at all, which is exactly as
    dangerous as ``mode: paper`` if it was live before (spec's "exactly
    one way to disable something" means disabling never bypasses this
    check just because it isn't technically a mode). Called once per
    admission, before any worker for the new mode starts — see
    ``runtimes/intraday_options/__main__.py::build_supervisor``.
    """
    if new_mode is ExecutionMode.LIVE:
        return _check_paper_to_live(repository, strategy_id=strategy_id)
    return _check_live_to_paper_or_disabled(
        repository,
        strategy_id=strategy_id,
        runtime_id=runtime_id,
        broker=broker,
        reconciliation_runner=reconciliation_runner,
    )


def _check_paper_to_live(
    repository: ExecutionRepository, *, strategy_id: str
) -> ModeTransitionDecision:
    open_paper = repository.open_positions_all_dates(
        strategy_id=strategy_id, execution_mode=ExecutionMode.PAPER
    )
    if open_paper:
        dates = sorted({p.trading_date for p in open_paper})
        return ModeTransitionDecision(
            False,
            f"{len(open_paper)} open paper position(s) exist for {strategy_id!r} "
            f"(trading_date(s): {', '.join(dates)}) — paper -> live is refused while "
            "any are open, regardless of when they were opened",
        )
    return ModeTransitionDecision(True)


def _check_live_to_paper_or_disabled(
    repository: ExecutionRepository,
    *,
    strategy_id: str,
    runtime_id: str,
    broker: Broker | None,
    reconciliation_runner: ReconciliationRunner | None,
) -> ModeTransitionDecision:
    open_live = repository.open_positions_all_dates(
        strategy_id=strategy_id, execution_mode=ExecutionMode.LIVE
    )
    live_orders = repository.open_orders(strategy_id=strategy_id, execution_mode=ExecutionMode.LIVE)
    unknown_orders = [row for row in live_orders if row["status"] == "UNKNOWN"]
    pending_intents = list(
        repository.database.connect().execute(
            "SELECT oi.correlation_id FROM order_intents oi "
            "LEFT JOIN orders o ON o.intent_id = oi.id "
            "WHERE oi.strategy_id = ? AND oi.execution_mode = 'live' AND o.id IS NULL",
            (strategy_id,),
        )
    )
    live_sessions = repository.database.connect().execute(
        "SELECT 1 FROM runtime_sessions WHERE strategy_id = ? "
        "AND execution_mode = 'live' LIMIT 1",
        (strategy_id,),
    ).fetchone()
    has_live_history = bool(
        open_live
        or live_orders
        or pending_intents
        or live_sessions is not None
        or repository.all_orders(
            strategy_id=strategy_id, execution_mode=ExecutionMode.LIVE
        )
    )

    if not has_live_history:
        # A supplied broker can still expose state missing locally (for
        # example after restoring the wrong operational DB).  Reconcile the
        # empty local view rather than silently assuming the broker is empty.
        if broker is not None and reconciliation_runner is not None:
            result = reconciliation_runner.run(
                runtime_id=runtime_id,
                strategy_id=strategy_id,
                broker=broker,
                local_orders=[],
                local_positions=[],
                trigger="mode_transition",
            )
            if result.status != "completed" or result.critical_mismatch_count:
                return ModeTransitionDecision(
                    False,
                    "broker reconciliation could not prove the account empty for "
                    f"{strategy_id!r}",
                )
        return ModeTransitionDecision(True)

    if broker is None or reconciliation_runner is None:
        return ModeTransitionDecision(
            False,
            f"{strategy_id!r} has live history (open positions or orders) but no "
            "broker/reconciliation_runner was supplied — cannot prove the transition "
            "is safe, so it is refused",
        )

    # The comparison itself needs every order, terminal included — a
    # locally FILLED order is correctly absent from open_orders(), but the
    # broker's own order-book report still names it, and without the
    # terminal rows here that match would be misread as BROKER_ONLY.
    all_local_orders = repository.all_orders(
        strategy_id=strategy_id, execution_mode=ExecutionMode.LIVE
    )
    local_orders = [
        LocalOrderState(correlation_id=row["correlation_id"], status=OrderStatus(row["status"]))
        for row in all_local_orders
    ]
    local_orders.extend(
        LocalOrderState(
            correlation_id=str(row["correlation_id"]),
            status=OrderStatus.UNKNOWN,
        )
        for row in pending_intents
    )
    local_positions = [
        LocalPositionState(
            security_id=p.security_id,
            quantity=p.quantity,
            average_price=p.average_price,
            product_type="",
            status="OPEN",
        )
        for p in open_live
    ]

    result = reconciliation_runner.run(
        runtime_id=runtime_id,
        strategy_id=strategy_id,
        broker=broker,
        local_orders=local_orders,
        local_positions=local_positions,
        trigger="mode_transition",
    )

    if result.status != "completed":
        return ModeTransitionDecision(
            False,
            f"reconciliation failed while checking the transition for {strategy_id!r}: "
            f"{result.error_message}",
        )
    if result.critical_mismatch_count > 0:
        return ModeTransitionDecision(
            False,
            f"reconciliation found {result.critical_mismatch_count} critical mismatch(es) "
            f"for {strategy_id!r} — refusing the transition until they are resolved",
        )
    if unknown_orders:
        return ModeTransitionDecision(
            False,
            f"{len(unknown_orders)} local order(s) for {strategy_id!r} are still UNKNOWN — "
            "refusing until reconciliation resolves them",
        )
    if pending_intents:
        return ModeTransitionDecision(
            False,
            f"{len(pending_intents)} live intent(s) for {strategy_id!r} have no "
            "terminal broker outcome — refusing the transition",
        )
    if live_orders:
        return ModeTransitionDecision(
            False,
            f"{len(live_orders)} pending live order(s) still exist for {strategy_id!r}",
        )
    if open_live:
        return ModeTransitionDecision(
            False,
            f"{len(open_live)} open live position(s) still exist for {strategy_id!r}",
        )
    return ModeTransitionDecision(True)
