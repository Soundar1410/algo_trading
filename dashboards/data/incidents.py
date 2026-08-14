"""Active vs. resolved incident classification — pure, no I/O.

The exact bug the spec names: "the existing dashboard confusingly shows old
feed errors after recovery without timestamps" —
:mod:`common.health.snapshot`'s own ``RecentError`` docstring records the
real incident this was caught from (14 August 2026: a feed outage, already
fixed by a clean restart nearly four hours earlier, still shown
indistinguishably from a live one).

No new table is added to answer this. Every error already carries
``occurred_at``; what was missing was cross-referencing it against each
component's *current* health signal. An error is "active" only when it is
the most recent recorded error for its (strategy, component) pair **and**
that component's current state is still unhealthy; everything else is
"resolved / historical" — always shown with its timestamp, never silently
dropped.
"""

from __future__ import annotations

from dataclasses import dataclass

from .intraday_options import EventErrorRow

#: Health states that mean "currently fine" — everything else (DEGRADED,
#: STALE, FAILED, CRASHED, STOPPED, or no heartbeat at all) is not.
_HEALTHY_STRATEGY_STATES = {"RUNNING_PAPER", "RUNNING_LIVE", "RUNNING"}


@dataclass(frozen=True)
class IncidentRow:
    strategy_id: str | None
    execution_mode: str | None
    severity: str
    component: str
    message: str
    occurred_at: str
    active: bool


def classify_incidents(
    errors: tuple[EventErrorRow, ...],
    *,
    broker_healthy: bool,
    database_healthy: bool,
    feed_currently_ok: bool,
    strategy_health_states: dict[str, str],
) -> tuple[IncidentRow, ...]:
    """Classify every error row as active or resolved/historical.

    ``errors`` must be ordered most-recent-first (every loader in this
    package returns rows that way, matching ``ORDER BY id DESC``).
    """
    latest_at: dict[tuple[str | None, str], str] = {}
    for row in errors:
        key = (row.strategy_id, row.component)
        latest_at.setdefault(key, row.occurred_at)

    result = []
    for row in errors:
        key = (row.strategy_id, row.component)
        is_latest_in_group = latest_at[key] == row.occurred_at
        component = row.component.lower()
        if "broker" in component:
            active = is_latest_in_group and not broker_healthy
        elif "feed" in component or "market_data" in component:
            active = is_latest_in_group and not feed_currently_ok
        elif "database" in component:
            active = is_latest_in_group and not database_healthy
        elif row.strategy_id is not None:
            state = strategy_health_states.get(row.strategy_id)
            active = is_latest_in_group and state not in _HEALTHY_STRATEGY_STATES
        else:
            # No specific health signal covers this component — the safe
            # default is "the newest one might still be live", not silence.
            active = is_latest_in_group
        result.append(
            IncidentRow(
                strategy_id=row.strategy_id,
                execution_mode=row.execution_mode,
                severity=row.severity,
                component=row.component,
                message=row.message,
                occurred_at=row.occurred_at,
                active=active,
            )
        )
    return tuple(result)
