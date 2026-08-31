"""Reusable strategy discovery and selector — the one place every
runtime-group page builds its "Strategy:" dropdown from, rather than each
page/tab re-deriving its own list.

A strategy must appear here the moment either of two things is true,
independent of the other:

1. it is present in ``config/strategies/*.yaml`` (configured, whether
   enabled or not) — so a newly configured strategy appears before its
   first trade, and a disabled one never disappears;
2. it currently has a healthy ``runtime_heartbeats`` row for this runtime
   — catches a strategy that is running but was, for whatever reason,
   never committed to config the normal way.

Deliberately **not** a third condition for "ever produced a
``trade_ledger``/``signals``/``order_intents``/``positions`` row, even
long after it stopped reporting" — that was tried and reverted (31 August
2026): a strategy renamed or removed from config stayed selectable forever
under its old id, labelled "Historical only", which is exactly the clutter
a rename (see docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md's ema_cross_*
addendum) leaves behind in this dropdown with no way to clear it from the
UI. That old id's rows are not deleted and remain fully queryable by
direct DB access or a saved report — only the *picker* stops offering
them once nothing configured or running still claims that id.

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

#: The selectbox sentinel for "no strategy filter" — a real value (not
#: ``None``) so it survives round-tripping through ``st.session_state``
#: and ``st.selectbox``'s own options list without special-casing.
ALL_STRATEGIES = "ALL"

_HEALTHY_STATES = {"RUNNING_PAPER", "RUNNING_LIVE", "RUNNING"}


@dataclass(frozen=True)
class StrategyOption:
    strategy_id: str
    status_label: str
    #: The most recently known execution mode, if any is known at all —
    #: from a current heartbeat's session first, config second. ``None``
    #: when neither exists (a healthy-but-unconfigured strategy with no
    #: recorded session yet).
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


def discover_strategy_options(
    conn: sqlite3.Connection | None,
    config_root: Path | str | None,
    runtime_id: str,
    settings: Settings | None = None,
) -> tuple[StrategyOption, ...]:
    """Every selectable strategy for one runtime group, status-labelled.

    ``conn=None`` means no database exists yet for this runtime (skips (2)
    above — nothing to read). ``config_root=None`` skips config-based
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
    if conn is not None:
        heartbeat_states = _latest_heartbeat_states(conn, runtime_id)
        session_modes = _latest_execution_modes(conn, runtime_id)

    # Every id here is either configured right now or currently reporting a
    # healthy heartbeat — never merely "left a trace once." A strategy_id
    # whose only trace is an old heartbeat or old trade-table rows, with
    # neither a config entry nor a healthy heartbeat today, does not qualify
    # for either branch below and is correctly absent from the picker (see
    # the module docstring for why that third condition was removed).
    running_ids = {sid for sid, state in heartbeat_states.items() if state in _HEALTHY_STATES}
    all_ids = set(configured) | running_ids
    options = []
    for strategy_id in sorted(all_ids):
        if strategy_id in running_ids:
            status_label = RUNNING
        else:
            status_label = STOPPED if configured[strategy_id] else DISABLED
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
