"""Read-only Streamlit dashboard — Master page.

``streamlit run dashboards/app.py`` is this platform's one entry point; the
other four pages (``dashboards/pages/``) are Streamlit's native multipage
convention, auto-discovered from this script's directory.

Two constraints from the spec shape every page in this package, this one
included:

* **The dashboard is read-only.** Every page reads through
  :func:`~dashboards._shared.load_snapshot`, which opens the database with
  :func:`~common.persistence.database.connect_readonly` — SQLite's ``mode=ro``
  URI, so a write is refused by the driver itself rather than by convention.
* **The dashboard never opens a market-data connection, a broker, or a write
  connection.** A second WebSocket from a dashboard would compete with the
  supervisor's shared feed for the same subscription budget, and a broker
  import here is exactly the side-effecting import the spec forbids.
  ``tests/unit/test_dashboard.py`` enforces this by AST-walking every module
  in this package.

Phase 1 shipped one tile reading four inline ``SELECT``s
(``RuntimeTile``/``_build_tile``, now retired). Phase 7 Part 1 built
:mod:`common.health.snapshot` specifically so no page — this one included —
ever writes its own SQL again; Part 3 is what actually puts that layer behind
a page.

Data functions are importable and tested directly; Streamlit is imported
lazily so the test suite never needs it at collection time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.health import HealthSnapshot

from ._shared import SnapshotUnavailable, load_snapshot

#: Spec section 9 requires reconciliation status on the Master page.
#: Reconciliation itself is Phase 10 throughout the spec (controlled-live
#: only) — shown explicitly rather than as a blank so an operator does not
#: mistake "not built yet" for "nothing to reconcile".
RECONCILIATION_STATUS = "Not implemented (Phase 10 — controlled live)"


@dataclass(frozen=True)
class RuntimeCard:
    """One runtime group's summary — the Master page's one card.

    Deliberately singular today: this platform has exactly one real runtime
    (``intraday_options``); ``positional_options`` and ``intraday_stocks``
    have no supervisor, no database and their own stub pages. ``load_master``
    takes one ``runtime_id`` rather than discovering "every configured
    runtime" because doing that would mean reading ``config/runtimes/*.yaml``
    — a different read path than every other page in this package uses, for
    a list that has exactly one real entry today. A future second runtime
    calls this once per group, the same way ``main`` below would loop.

    ``disabled_count`` is deliberately absent. A strategy with ``enabled:
    false`` in config never starts a worker, so it never writes a heartbeat
    and is invisible to :func:`~common.health.snapshot.read_snapshot` — spec
    asks for a "disabled" count, but this page's data source (the database
    alone) cannot answer it without also reading strategy config, which
    would be a second read path for one field. Not shown, not faked as zero.
    """

    runtime_id: str
    group_health_state: str | None
    heartbeat_age_seconds: float | None
    paper_count: int
    live_count: int
    failed_count: int
    total_count: int
    open_positions: int
    orders_today: int
    realised_pnl_paper: float
    realised_pnl_live: float
    feed_last_event: str | None
    broker_healthy: bool
    database_healthy: bool
    recent_errors: tuple[str, ...]


def load_master(
    database_path: Path | str, runtime_id: str, trading_date: str
) -> RuntimeCard | SnapshotUnavailable:
    """Build the one runtime card this page shows, or say why it cannot."""
    result = load_snapshot(database_path, runtime_id, trading_date)
    if isinstance(result, SnapshotUnavailable):
        return result
    return _card_from_snapshot(result)


def _card_from_snapshot(snapshot: HealthSnapshot) -> RuntimeCard:
    paper_count = sum(1 for s in snapshot.strategies if s.execution_mode == "paper")
    live_count = sum(1 for s in snapshot.strategies if s.execution_mode == "live")
    failed_count = sum(1 for s in snapshot.strategies if s.health_state == "FAILED")
    return RuntimeCard(
        runtime_id=snapshot.runtime_id,
        group_health_state=snapshot.group.health_state if snapshot.group else None,
        heartbeat_age_seconds=(
            snapshot.group.heartbeat_age_seconds if snapshot.group else None
        ),
        paper_count=paper_count,
        live_count=live_count,
        failed_count=failed_count,
        total_count=len(snapshot.strategies),
        open_positions=snapshot.open_positions,
        orders_today=snapshot.orders_today,
        realised_pnl_paper=snapshot.realised_pnl_paper,
        realised_pnl_live=snapshot.realised_pnl_live,
        feed_last_event=snapshot.market_data.last_event,
        broker_healthy=snapshot.broker.healthy,
        database_healthy=snapshot.database.integrity_ok,
        recent_errors=snapshot.recent_errors,
    )


def render(streamlit: Any, result: RuntimeCard | SnapshotUnavailable) -> None:
    """Draw the Master page. Takes the streamlit module so this stays testable."""
    if isinstance(result, SnapshotUnavailable):
        streamlit.info(result.reason)
        return

    card = result
    streamlit.subheader(f"{card.runtime_id} — supervisor")

    top = streamlit.columns(4)
    top[0].metric("Group health", card.group_health_state or "STOPPED")
    top[1].metric(
        "Heartbeat age",
        "—" if card.heartbeat_age_seconds is None else f"{card.heartbeat_age_seconds:.0f}s",
    )
    top[2].metric("Open positions", card.open_positions)
    top[3].metric(
        "Strategies",
        f"{card.total_count} (paper {card.paper_count} / live {card.live_count})",
    )
    streamlit.caption(f"Orders today: {card.orders_today}")

    if card.failed_count:
        streamlit.error(f"{card.failed_count} strategy(ies) in FAILED state")

    streamlit.caption(
        "Strategy counts are read from runtime state, not config — a "
        "strategy with enabled: false in YAML never starts and so never "
        "appears here. No 'disabled' count is shown for that reason."
    )

    pnl = streamlit.columns(2)
    pnl[0].metric("Realised P&L — paper", f"{card.realised_pnl_paper:,.2f}")
    pnl[1].metric("Realised P&L — live", f"{card.realised_pnl_live:,.2f}")

    status = streamlit.columns(3)
    status[0].metric("Feed", card.feed_last_event or "no data yet")
    status[1].metric("Broker", "healthy" if card.broker_healthy else "error")
    status[2].metric("Database", "ok" if card.database_healthy else "problem")

    streamlit.caption(f"Reconciliation: {RECONCILIATION_STATUS}")

    if card.recent_errors:
        streamlit.subheader("Recent errors")
        for message in card.recent_errors:
            streamlit.error(message)


def main() -> None:  # pragma: no cover - exercised manually via `streamlit run`
    import datetime as _dt

    import streamlit as st

    from common.config import load_paths

    st.set_page_config(page_title="algo_trading — Master", layout="wide")
    st.title("algo_trading")
    st.caption(
        "Read-only. Paper forward testing on live market data. "
        "Live order placement is not implemented."
    )

    paths = load_paths()
    runtime_id = "intraday_options"
    database_path = paths.database_path(runtime_id)
    trading_date = _dt.date.today().isoformat()

    render(st, load_master(database_path, runtime_id, trading_date))


if __name__ == "__main__":  # pragma: no cover
    main()
