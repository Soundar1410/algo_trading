"""Read-only Streamlit dashboard — one tile, for the walking skeleton.

Two constraints from the spec shape this file:

* **The dashboard is read-only.** It opens the database through
  :func:`~common.persistence.database.connect_readonly`, which uses SQLite's
  ``mode=ro`` URI, so a write is refused by the driver itself rather than by
  convention. It cannot run a migration or mutate operational state even if
  someone later imports the wrong helper into a page.
* **The dashboard never opens a market-data connection.** It reads persisted
  state only. A second WebSocket from a dashboard would compete with the
  supervisor's shared feed for the same subscription budget.

Phase 1 ships one tile. The multipage layout the spec describes is Phase 7,
where the dashboard is hardened; building five pages now would mean five pages
reading tables that do not exist yet.

The data functions are importable and tested directly; Streamlit is imported
lazily so the test suite never needs it at collection time.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.persistence import connect_readonly


@dataclass(frozen=True)
class RuntimeTile:
    """Everything the single Phase 1 tile shows."""

    runtime_id: str
    execution_mode: str
    health_state: str
    heartbeat_age_seconds: float | None
    open_positions: int
    realised_pnl: float
    orders_today: int
    last_error: str | None

    @property
    def mode_label(self) -> str:
        """Always explicit. An operator must never guess whether this is real money."""
        if self.execution_mode == "paper":
            return f"{self.execution_mode.upper()} (simulated)"
        return self.execution_mode.upper()


def load_tile(database_path: Path | str, runtime_id: str, trading_date: str) -> RuntimeTile:
    """Read one runtime's summary through a read-only connection."""
    conn = connect_readonly(database_path)
    try:
        return _build_tile(conn, runtime_id, trading_date)
    finally:
        conn.close()


def _build_tile(conn: sqlite3.Connection, runtime_id: str, trading_date: str) -> RuntimeTile:
    heartbeat = conn.execute(
        """
        SELECT health_state, beat_at,
               (julianday('now') - julianday(beat_at)) * 86400.0 AS age_seconds
        FROM runtime_heartbeats
        WHERE runtime_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (runtime_id,),
    ).fetchone()

    positions = conn.execute(
        """
        SELECT COUNT(*) AS open_count, COALESCE(SUM(realised_pnl), 0.0) AS realised
        FROM positions
        WHERE runtime_id = ? AND trading_date = ? AND execution_mode = 'paper'
        """,
        (runtime_id, trading_date),
    ).fetchone()

    open_count = conn.execute(
        """
        SELECT COUNT(*) AS c FROM positions
        WHERE runtime_id = ? AND trading_date = ? AND status = 'OPEN' AND quantity != 0
        """,
        (runtime_id, trading_date),
    ).fetchone()

    orders = conn.execute(
        """
        SELECT COUNT(*) AS c FROM orders o
        JOIN order_intents i ON i.id = o.intent_id
        WHERE o.runtime_id = ? AND i.trading_date = ?
        """,
        (runtime_id, trading_date),
    ).fetchone()

    error = conn.execute(
        "SELECT message FROM errors WHERE runtime_id = ? ORDER BY id DESC LIMIT 1",
        (runtime_id,),
    ).fetchone()

    return RuntimeTile(
        runtime_id=runtime_id,
        execution_mode="paper",
        health_state=heartbeat["health_state"] if heartbeat else "STOPPED",
        heartbeat_age_seconds=float(heartbeat["age_seconds"]) if heartbeat else None,
        open_positions=int(open_count["c"]) if open_count else 0,
        realised_pnl=float(positions["realised"]) if positions else 0.0,
        orders_today=int(orders["c"]) if orders else 0,
        last_error=error["message"] if error else None,
    )


def render(streamlit: Any, tile: RuntimeTile) -> None:
    """Draw the tile. Takes the streamlit module so this stays testable."""
    streamlit.subheader(f"{tile.runtime_id} — {tile.mode_label}")
    columns = streamlit.columns(4)
    columns[0].metric("Health", tile.health_state)
    columns[1].metric(
        "Heartbeat age",
        "—" if tile.heartbeat_age_seconds is None else f"{tile.heartbeat_age_seconds:.0f}s",
    )
    columns[2].metric("Open positions", tile.open_positions)
    columns[3].metric("Realised P&L", f"{tile.realised_pnl:,.2f}")
    streamlit.caption(f"Orders today: {tile.orders_today}")
    if tile.last_error:
        streamlit.error(f"Last error: {tile.last_error}")


def main() -> None:  # pragma: no cover - exercised manually via `streamlit run`
    import datetime as _dt

    import streamlit as st

    from common.config import load_paths

    st.set_page_config(page_title="algo_trading — paper forward testing", layout="wide")
    st.title("algo_trading")
    st.caption(
        "Read-only. Paper forward testing on live market data. "
        "Live order placement is not implemented."
    )

    paths = load_paths()
    runtime_id = "intraday_options"
    database_path = paths.database_path(runtime_id)
    trading_date = _dt.date.today().isoformat()

    if not database_path.is_file():
        st.info(f"No database yet at {database_path}. Start the supervisor first.")
        return

    render(st, load_tile(database_path, runtime_id, trading_date))


if __name__ == "__main__":  # pragma: no cover
    main()
