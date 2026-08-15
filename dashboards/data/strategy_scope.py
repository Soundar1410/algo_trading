"""Reusable strategy discovery and selector — the one place every
runtime-group page builds its "Strategy:" dropdown from, rather than each
page/tab re-deriving its own list.

A strategy must appear here the moment any one of three things is true,
independent of the other two:

1. it is present in ``config/strategies/*.yaml`` (configured, whether
   enabled or not) — so a newly configured strategy appears before its
   first trade, and a disabled one never disappears;
2. it has ever reported a ``runtime_heartbeats`` row for this runtime —
   catches a strategy that is running but was, for whatever reason, never
   committed to config the normal way;
3. it has ever produced ``trade_ledger``/``signals``/``order_intents``/
   ``positions`` rows for this runtime — so a strategy removed from
   config after running for a while keeps its historical results
   reachable, rather than vanishing along with the config file.

No side effects: every function here takes an already-open
``connect_readonly`` connection (or ``None``, when no database exists yet)
and a config root (or ``None``, when config-based discovery would be
unreliable for this runtime — see :func:`discover_strategy_options`).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.config import Settings

from .account import _raw_strategy_files

RUNNING = "Running"
STOPPED = "Stopped"
DISABLED = "Disabled"
HISTORICAL_ONLY = "Historical only"

#: The selectbox sentinel for "no strategy filter" — a real value (not
#: ``None``) so it survives round-tripping through ``st.session_state``
#: and ``st.selectbox``'s own options list without special-casing.
ALL_STRATEGIES = "ALL"

_HEALTHY_STATES = {"RUNNING_PAPER", "RUNNING_LIVE", "RUNNING"}

#: Every table a strategy_id might appear in purely historically — no
#: config entry, no current heartbeat, but real past results. Each must
#: carry a ``strategy_id`` column and a ``runtime_id`` column.
_HISTORICAL_TABLES = ("trade_ledger", "signals", "order_intents", "positions")


@dataclass(frozen=True)
class StrategyOption:
    strategy_id: str
    status_label: str
    #: The most recently known execution mode, if any is known at all —
    #: from a current heartbeat's session first, config second. ``None``
    #: for a historical-only strategy with neither.
    execution_mode: str | None


def _latest_heartbeat_states(conn: sqlite3.Connection, runtime_id: str) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT strategy_id, health_state FROM runtime_heartbeats
        WHERE runtime_id = ? AND strategy_id IS NOT NULL
          AND id IN (
              SELECT MAX(id) FROM runtime_heartbeats
              WHERE runtime_id = ? AND strategy_id IS NOT NULL
              GROUP BY strategy_id
          )
        """,
        (runtime_id, runtime_id),
    ).fetchall()
    return {row["strategy_id"]: row["health_state"] for row in rows}


def _latest_execution_modes(conn: sqlite3.Connection, runtime_id: str) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT strategy_id, execution_mode FROM runtime_sessions
        WHERE runtime_id = ? AND strategy_id IS NOT NULL
          AND id IN (
              SELECT MAX(id) FROM runtime_sessions
              WHERE runtime_id = ? AND strategy_id IS NOT NULL
              GROUP BY strategy_id
          )
        """,
        (runtime_id, runtime_id),
    ).fetchall()
    return {row["strategy_id"]: row["execution_mode"] for row in rows}


def _historical_strategy_ids(conn: sqlite3.Connection, runtime_id: str) -> set[str]:
    ids: set[str] = set()
    for table in _HISTORICAL_TABLES:
        ids.update(
            row["strategy_id"]
            for row in conn.execute(
                f"SELECT DISTINCT strategy_id FROM {table} WHERE runtime_id = ?",
                (runtime_id,),
            )
        )
    return ids


def discover_strategy_options(
    conn: sqlite3.Connection | None,
    config_root: Path | str | None,
    runtime_id: str,
    settings: Settings | None = None,
) -> tuple[StrategyOption, ...]:
    """Every selectable strategy for one runtime group, status-labelled.

    ``conn=None`` means no database exists yet for this runtime (skips (2)
    and (3) above — nothing to read). ``config_root=None`` skips config-based
    discovery entirely — appropriate only when there genuinely is no
    ``config/`` tree to read (a bare fixture, an early test). Whenever a real
    ``config_root`` is passed, discovery is filtered to files that declare
    exactly this ``runtime_id`` — every strategy carries one, required
    (``common.config.models.StrategyConfig.runtime_id``), so a strategy
    belonging to a different runtime group can no longer appear here by
    accident. Before that field existed, passing a real ``config_root`` for a
    stub runtime (Positional Options, Intraday Stocks) would have shown
    ``intraday_options``'s own strategies on a page they had nothing to do
    with — closed now, not merely worked around.
    """
    # settings is accepted (not yet used) for a future multi-runtime config
    # layering, matching the same forward-compatible shape every other
    # config-reading function in this package already takes.
    configured: dict[str, bool] = {}
    if config_root is not None:
        for data in _raw_strategy_files(Path(config_root), runtime_id=runtime_id):
            strategy_id = data.get("strategy_id")
            if isinstance(strategy_id, str):
                configured[strategy_id] = bool(data.get("enabled", False))

    heartbeat_states: dict[str, str] = {}
    session_modes: dict[str, str] = {}
    historical_ids: set[str] = set()
    if conn is not None:
        heartbeat_states = _latest_heartbeat_states(conn, runtime_id)
        session_modes = _latest_execution_modes(conn, runtime_id)
        historical_ids = _historical_strategy_ids(conn, runtime_id)

    all_ids = set(configured) | set(heartbeat_states) | historical_ids
    options = []
    for strategy_id in sorted(all_ids):
        state = heartbeat_states.get(strategy_id)
        if state in _HEALTHY_STATES:
            status_label = RUNNING
        elif strategy_id in configured:
            status_label = STOPPED if configured[strategy_id] else DISABLED
        else:
            status_label = HISTORICAL_ONLY
        options.append(
            StrategyOption(
                strategy_id=strategy_id,
                status_label=status_label,
                execution_mode=session_modes.get(strategy_id),
            )
        )
    return tuple(options)


def render_strategy_selector(
    streamlit: Any, options: tuple[StrategyOption, ...], *, key: str
) -> str | None:
    """A persistent ``st.selectbox`` over raw strategy ids. Returns the
    resolved strategy id, or ``None`` for "All Strategies".

    ``st.session_state[key]`` always holds exactly what this function
    returns (Streamlit's own key-based persistence — the selection survives
    a tab switch or a fragment-only rerun with no extra bookkeeping), which
    is also the same key the Strategy Comparison tab's row-click writes to
    directly to change the active strategy.
    """
    if not options:
        streamlit.selectbox(
            "Strategy",
            [ALL_STRATEGIES],
            format_func=lambda _sid: "All Strategies",
            key=key,
            disabled=True,
            help="No strategies are configured for this runtime group yet.",
        )
        return None

    status_by_id = {o.strategy_id: o.status_label for o in options}
    values = [ALL_STRATEGIES, *(o.strategy_id for o in options)]

    def _label(strategy_id: str) -> str:
        if strategy_id == ALL_STRATEGIES:
            return "All Strategies"
        return f"{strategy_id} ({status_by_id[strategy_id]})"

    choice = streamlit.selectbox("Strategy", values, format_func=_label, key=key)
    return None if choice == ALL_STRATEGIES else choice
