"""Read-model interface for Positional Options — not implemented yet.

``runtimes/positional_options/__init__.py`` is inert scaffolding (runbook
D56/D69: date-scoped persistence identity is unresolved for a strategy that
is not date-scoped). No supervisor, no worker, no database exists for this
runtime group. The dataclasses below are the read-model interface a real
positional worker's persistence would need to satisfy — written now so the
page's tab structure and a future engine's schema can be designed against
the same shape, not because any of this is queried today. No function in
this module opens a database; there is none to open.
"""

from __future__ import annotations

from dataclasses import dataclass

NOT_CONFIGURED = (
    "Positional options is not implemented yet. There is no supervisor, "
    "worker or database for this runtime group — see the runbook's Phase 6 "
    "Part 5 record (D56, D69) for the open persistence-identity question a "
    "real positional strategy will need to answer first."
)


@dataclass(frozen=True)
class CycleRow:
    """One weekly/positional strategy cycle — the unit adjustments and legs
    attach to. Modelled on the reference weekly dashboard's ``CycleState``
    lifecycle (created → active → adjusting → exit → completed/failed)."""

    cycle_id: str
    strategy_id: str
    underlying: str
    expiry: str
    state: str
    leg_count: int
    adjustment_count: int
    unrealized_pnl: float | None


@dataclass(frozen=True)
class LegRow:
    cycle_id: str
    underlying: str
    expiry: str
    strike: float | None
    option_type: str | None
    side: str
    role: str | None
    quantity: int
    entry_price: float | None
    entry_time: str | None
    exit_price: float | None
    exit_time: str | None
    status: str


@dataclass(frozen=True)
class AdjustmentRow:
    cycle_id: str
    triggered_at: str
    trigger_reason: str
    pre_adjustment_delta: float | None
    post_adjustment_delta: float | None
    cost: float | None


@dataclass(frozen=True)
class WeeklyHistoryRow:
    cycle_id: str
    strategy_id: str
    final_status: str
    net_pnl: float | None
    adjustment_count: int
    created_at: str
    updated_at: str
