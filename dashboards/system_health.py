"""Read-only Streamlit page — System Health.

Spec: authentication/token expiry, WebSocket state, expected vs. active
subscriptions, reconnects/stale instruments, database integrity/migration,
PID status, notification delivery failures, reconciliation state, the
live-gate matrix, and — the page's most-named fix — **active incidents kept
visually separate from resolved/historical ones, both always timestamped.**
This page is the reason Phase 7 Part 1 built ``common.health.snapshot``:
every runtime-group field here already existed in the snapshot before this
page grew account-wide and live-gate sections on top of it.

**PID shown, lock status not.** A PID recorded in ``runtime_sessions`` is
database state, reachable through the same connection every other field on
this page uses. Whether the matching ``.lock`` file is actually held is
filesystem state (``common.process.locks``), a different read path this
page does not open — process/lock reconciliation is a later phase's job.

Read-only/no-side-effect discipline is identical to ``dashboards/app.py`` —
see that module's docstring.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.health import HealthSnapshot

for _parent in Path(__file__).resolve().parents:
    if (_parent / "pyproject.toml").is_file():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from dashboards._shared import SnapshotUnavailable, load_snapshot, run_bounded  # noqa: E402
from dashboards.data.account import (  # noqa: E402
    ConfigUnavailable,
    LiveGateMatrix,
    load_account_status,
    load_live_gate_matrix,
    load_reconciliation_status,
    render_account_status,
    render_live_gate_matrix,
    render_reconciliation_status,
)
from dashboards.data.incidents import IncidentRow, classify_incidents  # noqa: E402
from dashboards.data.intraday_options import load_errors, load_notifications  # noqa: E402
from dashboards.formatting import format_ist  # noqa: E402


@dataclass(frozen=True)
class StrategyPid:
    strategy_id: str
    pid: int | None
    health_state: str


@dataclass(frozen=True)
class NotificationFailure:
    channel: str
    event_type: str
    failure_reason: str | None
    created_at: str


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
    active_incidents: tuple[IncidentRow, ...]
    resolved_incidents: tuple[IncidentRow, ...]
    notification_failures: tuple[NotificationFailure, ...]


def load_system_health(
    database_path: Path | str, runtime_id: str, trading_date: str
) -> SystemHealthView | SnapshotUnavailable:
    result = load_snapshot(database_path, runtime_id, trading_date)
    if isinstance(result, SnapshotUnavailable):
        return result
    snapshot = result

    errors = run_bounded(database_path, lambda conn: load_errors(conn, runtime_id))
    if isinstance(errors, SnapshotUnavailable):
        active_incidents: tuple[IncidentRow, ...] = ()
        resolved_incidents: tuple[IncidentRow, ...] = ()
    else:
        strategy_states = {s.strategy_id: s.health_state for s in snapshot.strategies}
        classified = classify_incidents(
            errors,
            broker_healthy=snapshot.broker.healthy,
            database_healthy=snapshot.database.integrity_ok,
            feed_currently_ok=(
                snapshot.market_data.subscriptions_match
                and snapshot.market_data.last_event not in {"disconnected", "reconnect_exhausted"}
            ),
            strategy_health_states=strategy_states,
        )
        active_incidents = tuple(row for row in classified if row.active)
        resolved_incidents = tuple(row for row in classified if not row.active)

    notifications = run_bounded(database_path, lambda conn: load_notifications(conn, runtime_id))
    failures = (
        tuple(
            NotificationFailure(
                channel=n.channel,
                event_type=n.event_type,
                failure_reason=n.failure_reason,
                created_at=n.created_at,
            )
            for n in notifications
            if not n.delivered
        )
        if not isinstance(notifications, SnapshotUnavailable)
        else ()
    )

    return _view_from_snapshot(snapshot, active_incidents, resolved_incidents, failures)


def _view_from_snapshot(
    snapshot: HealthSnapshot,
    active_incidents: tuple[IncidentRow, ...],
    resolved_incidents: tuple[IncidentRow, ...],
    notification_failures: tuple[NotificationFailure, ...],
) -> SystemHealthView:
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
        active_incidents=active_incidents,
        resolved_incidents=resolved_incidents,
        notification_failures=notification_failures,
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
        "still held is not shown here."
    )
    streamlit.write(f"Supervisor: {view.group_pid if view.group_pid is not None else '—'}")
    for strategy in view.strategy_pids:
        streamlit.write(
            f"{strategy.strategy_id}: pid={strategy.pid if strategy.pid is not None else '—'} "
            f"({strategy.health_state})"
        )

    streamlit.markdown(f"**Active incidents ({len(view.active_incidents)})**")
    if view.active_incidents:
        for incident in view.active_incidents:
            streamlit.error(
                f"{format_ist(incident.occurred_at)} — [{incident.component}] {incident.message}"
            )
    else:
        streamlit.success("No active incidents.")

    with streamlit.expander(f"Resolved / historical incidents ({len(view.resolved_incidents)})"):
        if view.resolved_incidents:
            for incident in view.resolved_incidents:
                streamlit.caption(
                    f"{format_ist(incident.occurred_at)} — [{incident.component}] "
                    f"{incident.message} (resolved)"
                )
        else:
            streamlit.caption("No historical incidents recorded.")

    if view.notification_failures:
        streamlit.markdown("**Notification delivery failures**")
        for failure in view.notification_failures:
            streamlit.warning(
                f"{format_ist(failure.created_at)} — {failure.channel}/{failure.event_type}: "
                f"{failure.failure_reason or 'no reason recorded'}"
            )


def main() -> None:  # pragma: no cover - exercised manually via `streamlit run`
    import datetime as _dt

    import streamlit as st

    from common.config import load_paths, load_settings

    st.set_page_config(page_title="algo_trading — System Health", layout="wide", page_icon="🩺")
    st.title("System Health")

    paths = load_paths()
    settings = load_settings()
    runtime_id = "intraday_options"
    database_path = paths.database_path(runtime_id)
    trading_date = _dt.date.today().isoformat()

    if "sh_auto_refresh" not in st.session_state:
        st.session_state["sh_auto_refresh"] = True
    with st.sidebar:
        st.session_state["sh_auto_refresh"] = st.checkbox(
            "Auto-refresh (5s)", value=st.session_state["sh_auto_refresh"]
        )
        if st.button("Refresh now"):
            st.rerun()

    @st.fragment(run_every=5 if st.session_state["sh_auto_refresh"] else None)
    def _body() -> None:
        render(st, load_system_health(database_path, runtime_id, trading_date))

        st.divider()
        render_account_status(
            st, load_account_status(paths.account_shared_database_path, trading_date=trading_date)
        )

        st.divider()
        render_reconciliation_status(st, load_reconciliation_status(database_path))

        st.divider()
        matrix: LiveGateMatrix | ConfigUnavailable = load_live_gate_matrix(
            paths.config_root, runtime_id, settings
        )
        render_live_gate_matrix(st, matrix)

    _body()


if __name__ == "__main__":  # pragma: no cover
    main()
