"""Fill-based P&L and the individual threshold checks the 13-step exit/
adjustment priority ladder (spec section 6.2) is built from. Pure functions
only — no engine/persistence access — so each rule is testable in
isolation and the ladder's *ordering* (owned by ``strategy.py``) can never
accidentally be re-derived here.
"""

from __future__ import annotations

from dataclasses import dataclass

from common.engine.positional.positional_models import Cycle

from .config import ExitsConfig


@dataclass(frozen=True)
class PnlSnapshot:
    """Spec section 6.1's exact formula, computed once per evaluation."""

    realised_pnl: float
    unrealised_pnl: float
    charges: float
    net_strategy_pnl: float
    credit_captured_percent: float | None
    loss_amount: float


def compute_pnl(cycle: Cycle, *, charges: float) -> PnlSnapshot:
    realised = cycle.realised_gross_pnl()
    unrealised = cycle.unrealised_gross_pnl()
    net = realised + unrealised - charges
    credit_captured = (
        100.0 * net / cycle.original_net_credit
        if cycle.original_net_credit is not None and cycle.original_net_credit != 0
        else None
    )
    loss_amount = max(0.0, -net)
    return PnlSnapshot(
        realised_pnl=realised,
        unrealised_pnl=unrealised,
        charges=charges,
        net_strategy_pnl=net,
        credit_captured_percent=credit_captured,
        loss_amount=loss_amount,
    )


def _credit_multiple_breached(cycle: Cycle, loss_amount: float, multiple: float) -> bool:
    if cycle.original_net_credit is None or cycle.original_net_credit <= 0:
        return False
    return loss_amount >= multiple * cycle.original_net_credit


def is_emergency_stop(cycle: Cycle, pnl: PnlSnapshot, config: ExitsConfig) -> bool:
    return _credit_multiple_breached(cycle, pnl.loss_amount, config.emergency_loss_credit_multiple)


def is_hard_stop(cycle: Cycle, pnl: PnlSnapshot, config: ExitsConfig) -> bool:
    return _credit_multiple_breached(cycle, pnl.loss_amount, config.hard_loss_credit_multiple)


def is_soft_stop(cycle: Cycle, pnl: PnlSnapshot, config: ExitsConfig) -> bool:
    return _credit_multiple_breached(cycle, pnl.loss_amount, config.soft_loss_credit_multiple)


def is_capital_stop(pnl: PnlSnapshot, config: ExitsConfig, *, allocated_capital: float) -> bool:
    threshold = config.maximum_cycle_loss_capital_percent / 100.0 * allocated_capital
    return pnl.loss_amount >= threshold


def is_profit_target(cycle: Cycle, pnl: PnlSnapshot, config: ExitsConfig) -> bool:
    if cycle.original_net_credit is None or cycle.original_net_credit <= 0:
        return False
    target = (config.profit_credit_capture_percent / 100.0) * cycle.original_net_credit
    return pnl.net_strategy_pnl >= target


def is_margin_breach(
    *, estimated_margin: float | None, allocated_capital: float, config: ExitsConfig
) -> bool:
    if estimated_margin is None or allocated_capital <= 0:
        return False
    utilization_percent = 100.0 * estimated_margin / allocated_capital
    return utilization_percent > config.maximum_margin_utilization_percent
