"""Read-only Streamlit page — System Health.

Spec 2605-2613: authentication/token expiry, WebSocket state, stale
instruments, reconnect counts, database health, PID status, recent errors.
This page is the reason Phase 7 Part 1 built ``common.health.snapshot`` —
every field here already existed in the snapshot before this page did.

**PID shown, lock status not.** A PID recorded in ``runtime_sessions`` is
database state, reachable through the same connection every other field on
this page uses. Whether the matching ``.lock`` file is actually held is
filesystem state (``common.process.locks``) — a different read path this
page does not open, by the same "exclusively snapshot.py and
connect_readonly" discipline every page in this package follows. Process/
lock reconciliation is Part 4's job ("PID handling and operator commands"),
not this page's.

Read-only/no-side-effect discipline is identical to ``dashboards/app.py`` —
see that module's docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.health import HealthSnapshot

from ._shared import SnapshotUnavailable, load_snapshot


@dataclass(frozen=True)
class StrategyPid:
    strategy_id: str
    pid: int | None
    health_state: str


@dataclass(frozen=True)
class SystemHealthView:
    runtime_id: str
    token_expiry: str | None
    auth_event: str | None
    feed_last_event: str | None
    feed_last_reason: str | None
    reconnect_count: int
    reconnect_exhausted_count: int
    stale_instrument_count: int
    subscriptions_match: bool
    database_healthy: bool
    integrity_problems: tuple[str, ...]
    foreign_key_violation_count: int
    migration_version: str | None
    group_pid: int | None
    strategy_pids: tuple[StrategyPid, ...]
    recent_errors: tuple[str, ...]


def load_system_health(
    database_path: Path | str, runtime_id: str, trading_date: str
) -> SystemHealthView | SnapshotUnavailable:
    result = load_snapshot(database_path, runtime_id, trading_date)
    if isinstance(result, SnapshotUnavailable):
        return result
    return _view_from_snapshot(result)


def _view_from_snapshot(snapshot: HealthSnapshot) -> SystemHealthView:
    return SystemHealthView(
        runtime_id=snapshot.runtime_id,
        token_expiry=snapshot.auth.token_expiry,
        auth_event=snapshot.auth.event,
        feed_last_event=snapshot.market_data.last_event,
        feed_last_reason=snapshot.market_data.last_reason,
        reconnect_count=snapshot.market_data.reconnect_count,
        reconnect_exhausted_count=snapshot.market_data.reconnect_exhausted_count,
        stale_instrument_count=snapshot.market_data.stale_instrument_count,
        subscriptions_match=snapshot.market_data.subscriptions_match,
        database_healthy=snapshot.database.integrity_ok,
        integrity_problems=snapshot.database.integrity_problems,
        foreign_key_violation_count=snapshot.database.foreign_key_violation_count,
        migration_version=snapshot.database.migration_version,
        group_pid=snapshot.group.pid if snapshot.group else None,
        strategy_pids=tuple(
            StrategyPid(strategy_id=s.strategy_id, pid=s.pid, health_state=s.health_state)
            for s in snapshot.strategies
        ),
        recent_errors=snapshot.recent_errors,
    )


def render(streamlit: Any, result: SystemHealthView | SnapshotUnavailable) -> None:
    if isinstance(result, SnapshotUnavailable):
        streamlit.info(result.reason)
        return

    view = result
    streamlit.subheader(f"{view.runtime_id} — System Health")

    auth = streamlit.columns(2)
    auth[0].metric("Last auth event", view.auth_event or "none recorded")
    auth[1].metric("Token expiry", view.token_expiry or "unknown")

    feed = streamlit.columns(4)
    feed[0].metric("Feed state", view.feed_last_event or "no data yet")
    feed[1].metric("Reconnects", view.reconnect_count)
    feed[2].metric("Reconnects exhausted", view.reconnect_exhausted_count)
    feed[3].metric("Stale instruments", view.stale_instrument_count)
    if not view.subscriptions_match:
        streamlit.warning("Expected and active subscriptions do not match.")
    if view.feed_last_reason:
        streamlit.caption(f"Last feed reason: {view.feed_last_reason}")

    db = streamlit.columns(3)
    db[0].metric("Database", "ok" if view.database_healthy else "problem")
    db[1].metric("Foreign-key violations", view.foreign_key_violation_count)
    db[2].metric("Migration version", view.migration_version or "unknown")
    for problem in view.integrity_problems:
        streamlit.error(f"integrity_check: {problem}")

    streamlit.markdown("**PIDs**")
    streamlit.caption(
        "From runtime_sessions only — whether the matching lock file is "
        "still held is not shown here; see Part 4."
    )
    streamlit.write(f"Supervisor: {view.group_pid if view.group_pid is not None else '—'}")
    for strategy in view.strategy_pids:
        streamlit.write(
            f"{strategy.strategy_id}: pid={strategy.pid if strategy.pid is not None else '—'} "
            f"({strategy.health_state})"
        )

    if view.recent_errors:
        streamlit.markdown("**Recent errors**")
        for message in view.recent_errors:
            streamlit.error(message)


def main() -> None:  # pragma: no cover - exercised manually via `streamlit run`
    import datetime as _dt

    import streamlit as st

    from common.config import load_paths

    st.set_page_config(page_title="algo_trading — System Health", layout="wide")
    st.title("System Health")

    paths = load_paths()
    runtime_id = "intraday_options"
    database_path = paths.database_path(runtime_id)
    trading_date = _dt.date.today().isoformat()

    render(st, load_system_health(database_path, runtime_id, trading_date))


if __name__ == "__main__":  # pragma: no cover
    main()
