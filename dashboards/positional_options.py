"""Read-only Streamlit page — Positional Options.

Eight tabs (spec): Overview, Active Cycles, Legs, Adjustments, Orders &
Fills, History, Performance, Health — all backed by
:mod:`dashboards.data.positional`, never by SQL written in this file.

**A persistent "Strategy:" selector**, right below the title and above the
tabs, scopes every tab. Built from
:func:`dashboards.data.strategy_scope.discover_strategy_options` — the same
reusable component the Intraday Options page uses — so a strategy appears
the moment it is configured, running, disabled, or has any historical
record, never only once it has traded.

**No database, no config: the graceful stub.** ``render(streamlit)`` called
with no arguments (or ``main()`` before a positional database has ever been
created) renders the identical eight tabs with a plain "not configured"
message in each and a disabled selector — never an exception, never a
fabricated empty table. The moment ``config_root``/``database_path`` are
supplied (``main()`` always supplies both, from
:func:`common.config.load_paths`), every tab queries real data through
:func:`dashboards._shared.run_bounded`, exactly like every other page in
this package — no restructuring, only what
:func:`~dashboards.data.strategy_scope.discover_strategy_options` promised.

Read-only/no-side-effect discipline is identical to ``dashboards/Home.py`` —
see that module's docstring.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

for _parent in Path(__file__).resolve().parents:
    if (_parent / "pyproject.toml").is_file():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from dashboards._shared import SnapshotUnavailable, load_snapshot, run_bounded  # noqa: E402
from dashboards.data.intraday_options import load_errors  # noqa: E402
from dashboards.data.positional import (  # noqa: E402
    NOT_CONFIGURED,
    AdjustmentRow,
    CycleOrderRow,
    CycleRow,
    LegRow,
    load_adjustments_for_cycle,
    load_cycles,
    load_legs_for_cycle,
    load_open_cycle,
    load_orders_for_cycle,
)
from dashboards.data.strategy_scope import (  # noqa: E402
    StrategyOption,
    discover_strategy_options,
    render_strategy_selector,
)
from dashboards.formatting import MISSING, format_inr, format_ist, health_badge  # noqa: E402

_TERMINAL_CYCLE_STATES = frozenset({"COMPLETED", "FAILED", "ABANDONED"})

_RUNTIME_ID = "positional_options"

_TABS = (
    "Overview",
    "Active Cycles",
    "Legs",
    "Adjustments",
    "Orders & Fills",
    "History",
    "Performance",
    "Health",
)


def _discover_options(
    streamlit: Any, config_root: object, database_path: object
) -> tuple[StrategyOption, ...]:
    if database_path is not None:
        result = run_bounded(
            database_path,  # type: ignore[arg-type]
            lambda conn: discover_strategy_options(conn, config_root, _RUNTIME_ID),  # type: ignore[arg-type]
        )
        return () if isinstance(result, SnapshotUnavailable) else result
    if config_root is not None:
        return discover_strategy_options(None, config_root, _RUNTIME_ID)  # type: ignore[arg-type]
    return ()


def render(streamlit: Any, config_root: object = None, database_path: object = None) -> None:
    streamlit.subheader("Positional Options")
    options = _discover_options(streamlit, config_root, database_path)
    selected_strategy = render_strategy_selector(streamlit, options, key="po_strategy")

    if database_path is None:
        streamlit.warning(NOT_CONFIGURED)
        tabs = streamlit.tabs(list(_TABS))
        for tab, label in zip(tabs, _TABS, strict=True):
            with tab:
                streamlit.info(f"{label}: not configured. {NOT_CONFIGURED}")
        return

    tabs = streamlit.tabs(list(_TABS))
    with tabs[0]:
        _render_overview(streamlit, database_path, selected_strategy)
    with tabs[1]:
        _render_active_cycles(streamlit, database_path, selected_strategy)
    with tabs[2]:
        _render_legs(streamlit, database_path, selected_strategy)
    with tabs[3]:
        _render_adjustments(streamlit, database_path, selected_strategy)
    with tabs[4]:
        _render_orders(streamlit, database_path, selected_strategy)
    with tabs[5]:
        _render_history(streamlit, database_path, selected_strategy)
    with tabs[6]:
        _render_performance(streamlit, database_path, selected_strategy)
    with tabs[7]:
        _render_health(streamlit, database_path, selected_strategy)


# ------------------------------------------------------------------- tabs
def _require_strategy(streamlit: Any, strategy_id: str | None) -> bool:
    if strategy_id is None:
        streamlit.info("Select a strategy above to see its cycles.")
        return False
    return True


def _render_overview(streamlit: Any, database_path: object, strategy_id: str | None) -> None:
    if not _require_strategy(streamlit, strategy_id):
        return
    assert strategy_id is not None
    result = run_bounded(
        database_path,  # type: ignore[arg-type]
        lambda conn: load_open_cycle(conn, strategy_id=strategy_id, execution_mode="paper"),
    )
    if isinstance(result, SnapshotUnavailable):
        streamlit.info(result.reason)
        return
    if result is None:
        streamlit.info("No open cycle for this strategy — nothing is currently at risk.")
        return
    cycle: CycleRow = result
    cols = streamlit.columns(4)
    cols[0].metric("State", cycle.state)
    cols[1].metric("Expiry", cycle.resolved_expiry_date)
    cols[2].metric("Net credit", format_inr(cycle.original_net_credit))
    cols[3].metric("Adjustments (cycle)", str(cycle.adjustments_this_cycle))
    streamlit.caption(
        f"cycle_id={cycle.cycle_id} · opened {cycle.opened_trading_date} · "
        f"underlying={cycle.underlying} · square_off={cycle.square_off_state}"
    )
    if cycle.day_blocked_reason:
        streamlit.warning(f"Entries blocked today: {cycle.day_blocked_reason}")
    if cycle.pending_adjustment_role:
        streamlit.info(
            f"Adjustment in flight: role={cycle.pending_adjustment_role} "
            f"state={cycle.pending_adjustment_state}"
        )


def _render_active_cycles(streamlit: Any, database_path: object, strategy_id: str | None) -> None:
    if not _require_strategy(streamlit, strategy_id):
        return
    assert strategy_id is not None
    result = run_bounded(
        database_path,  # type: ignore[arg-type]
        lambda conn: load_cycles(conn, strategy_id=strategy_id, execution_mode="paper"),
    )
    cycles: tuple[CycleRow, ...] = () if isinstance(result, SnapshotUnavailable) else result
    active = tuple(c for c in cycles if c.state not in _TERMINAL_CYCLE_STATES)
    if not active:
        streamlit.info("No active (non-terminal) cycle for this strategy.")
        return
    streamlit.dataframe(
        [
            {
                "Cycle": c.cycle_id,
                "State": c.state,
                "Expiry": c.resolved_expiry_date,
                "Net credit": format_inr(c.original_net_credit),
                "Adj/day": c.adjustments_today,
                "Adj/cycle": c.adjustments_this_cycle,
                "Square-off": c.square_off_state,
                "Updated": format_ist(c.updated_at),
            }
            for c in active
        ],
        width="stretch",
    )


def _render_legs(streamlit: Any, database_path: object, strategy_id: str | None) -> None:
    if not _require_strategy(streamlit, strategy_id):
        return
    assert strategy_id is not None
    cycle_id = _resolve_display_cycle(streamlit, database_path, strategy_id)
    if cycle_id is None:
        return
    result = run_bounded(
        database_path, lambda conn: load_legs_for_cycle(conn, cycle_id=cycle_id)  # type: ignore[arg-type]
    )
    legs: tuple[LegRow, ...] = () if isinstance(result, SnapshotUnavailable) else result
    if not legs:
        streamlit.info(f"No legs recorded for cycle {cycle_id}.")
        return
    streamlit.dataframe(
        [
            {
                "Role": leg.leg_role,
                "Replacement": "yes" if leg.is_replacement else "no",
                "Symbol": leg.symbol or MISSING,
                "Strike": leg.strike if leg.strike is not None else MISSING,
                "Side": leg.side or MISSING,
                "Qty": leg.quantity if leg.quantity is not None else MISSING,
                "Entry": leg.entry_price if leg.entry_price is not None else MISSING,
                "Exit": leg.exit_price if leg.exit_price is not None else MISSING,
                "State": leg.state,
                "Gross P&L": format_inr(leg.realized_gross_pnl),
            }
            for leg in legs
        ],
        width="stretch",
    )


def _render_adjustments(streamlit: Any, database_path: object, strategy_id: str | None) -> None:
    if not _require_strategy(streamlit, strategy_id):
        return
    assert strategy_id is not None
    cycle_id = _resolve_display_cycle(streamlit, database_path, strategy_id)
    if cycle_id is None:
        return
    result = run_bounded(
        database_path,  # type: ignore[arg-type]
        lambda conn: load_adjustments_for_cycle(conn, cycle_id=cycle_id),
    )
    rows: tuple[AdjustmentRow, ...] = () if isinstance(result, SnapshotUnavailable) else result
    if not rows:
        streamlit.info(f"No completed adjustments for cycle {cycle_id}.")
        return
    streamlit.dataframe(
        [
            {
                "#": row.adjustment_sequence,
                "Reason": row.trigger_reason,
                "Target leg": row.target_leg_id,
                "Replacement leg": row.replacement_leg_id or MISSING,
                "Pre-delta": row.pre_adjustment_net_delta,
                "Post-delta": row.post_adjustment_net_delta,
                "P&L": format_inr(row.realized_pnl),
                "Outcome": row.lifecycle_state,
                "Claimed": format_ist(row.claimed_at),
            }
            for row in rows
        ],
        width="stretch",
    )


def _render_orders(streamlit: Any, database_path: object, strategy_id: str | None) -> None:
    if not _require_strategy(streamlit, strategy_id):
        return
    assert strategy_id is not None
    cycle_id = _resolve_display_cycle(streamlit, database_path, strategy_id)
    if cycle_id is None:
        return
    result = run_bounded(
        database_path, lambda conn: load_orders_for_cycle(conn, cycle_id=cycle_id)  # type: ignore[arg-type]
    )
    rows: tuple[CycleOrderRow, ...] = () if isinstance(result, SnapshotUnavailable) else result
    if not rows:
        streamlit.info(f"No orders placed yet for cycle {cycle_id}.")
        return
    streamlit.dataframe(
        [
            {
                "Leg": row.leg_id or MISSING,
                "Side": row.side,
                "Qty": row.quantity,
                "Status": row.order_status or MISSING,
                "Filled qty": row.filled_quantity if row.filled_quantity is not None else MISSING,
                "Filled price": row.filled_price if row.filled_price is not None else MISSING,
                "Rejection": row.rejection_reason or MISSING,
            }
            for row in rows
        ],
        width="stretch",
    )


def _render_history(streamlit: Any, database_path: object, strategy_id: str | None) -> None:
    if not _require_strategy(streamlit, strategy_id):
        return
    assert strategy_id is not None
    result = run_bounded(
        database_path,  # type: ignore[arg-type]
        lambda conn: load_cycles(conn, strategy_id=strategy_id, execution_mode="paper"),
    )
    cycles: tuple[CycleRow, ...] = () if isinstance(result, SnapshotUnavailable) else result
    terminal = tuple(c for c in cycles if c.state in _TERMINAL_CYCLE_STATES)
    if not terminal:
        streamlit.info("No completed cycle history yet.")
        return
    streamlit.dataframe(
        [
            {
                "Cycle": c.cycle_id,
                "Final state": c.state,
                "Expiry": c.resolved_expiry_date,
                "Net credit": format_inr(c.original_net_credit),
                "Adjustments": c.adjustments_this_cycle,
                "Opened": c.opened_trading_date,
                "Updated": format_ist(c.updated_at),
            }
            for c in terminal
        ],
        width="stretch",
    )


def _render_performance(streamlit: Any, database_path: object, strategy_id: str | None) -> None:
    if not _require_strategy(streamlit, strategy_id):
        return
    assert strategy_id is not None
    result = run_bounded(
        database_path,  # type: ignore[arg-type]
        lambda conn: load_cycles(conn, strategy_id=strategy_id, execution_mode="paper"),
    )
    cycles: tuple[CycleRow, ...] = () if isinstance(result, SnapshotUnavailable) else result
    terminal = tuple(c for c in cycles if c.state in _TERMINAL_CYCLE_STATES)
    if not terminal:
        streamlit.info("No completed cycle to compute performance from.")
        return
    completed = tuple(c for c in terminal if c.state == "COMPLETED")
    cols = streamlit.columns(3)
    cols[0].metric("Cycles completed", str(len(completed)))
    cols[1].metric("Cycles failed/abandoned", str(len(terminal) - len(completed)))
    total_credit = sum(c.original_net_credit or 0.0 for c in completed)
    cols[2].metric("Total original credit captured", format_inr(total_credit))
    streamlit.caption(
        "Fill-based realised P&L per cycle (including adjustment legs and "
        "charges) is on the Legs/Adjustments tabs; this summary is credit-"
        "basis only — no live mark-to-market is persisted for paper legs."
    )


def _render_health(streamlit: Any, database_path: object, strategy_id: str | None) -> None:
    snapshot = load_snapshot(database_path, _RUNTIME_ID, _today_ist())  # type: ignore[arg-type]
    if isinstance(snapshot, SnapshotUnavailable):
        streamlit.info(snapshot.reason)
    else:
        group_state = snapshot.group.health_state if snapshot.group else None
        streamlit.write(f"Supervisor health: {health_badge(group_state)}")
        strategy_health = next(
            (s for s in snapshot.strategies if s.strategy_id == strategy_id), None
        )
        if strategy_health is not None:
            streamlit.write(f"{strategy_id} health: {health_badge(strategy_health.health_state)}")
    result = run_bounded(
        database_path,  # type: ignore[arg-type]
        lambda conn: load_errors(conn, _RUNTIME_ID, strategy_id=strategy_id, limit=50),
    )
    errors = () if isinstance(result, SnapshotUnavailable) else result
    if not errors:
        streamlit.info("No recorded errors for this runtime.")
        return
    streamlit.dataframe(
        [
            {
                "Strategy": row.strategy_id or MISSING,
                "Severity": row.severity,
                "Component": row.component,
                "Message": row.message,
                "Occurred": format_ist(row.occurred_at),
            }
            for row in errors
        ],
        width="stretch",
    )


def _resolve_display_cycle(
    streamlit: Any, database_path: object, strategy_id: str
) -> str | None:
    """The cycle every per-cycle tab drills into: the open one if there is
    one, else the most recently updated cycle of any state — so History's
    own legs/adjustments remain reachable after a cycle completes."""
    result = run_bounded(
        database_path,  # type: ignore[arg-type]
        lambda conn: load_cycles(conn, strategy_id=strategy_id, execution_mode="paper", limit=1),
    )
    if isinstance(result, SnapshotUnavailable):
        streamlit.info(result.reason)
        return None
    if not result:
        streamlit.info("No cycle exists yet for this strategy.")
        return None
    return result[0].cycle_id


def _today_ist() -> str:
    from common.utils.timeutils import local_date_in, now_ist

    return local_date_in(now_ist()).isoformat()


def main() -> None:  # pragma: no cover - exercised manually via `streamlit run`
    import streamlit as st

    from common.config import load_paths

    st.set_page_config(
        page_title="algo_trading — Positional Options", layout="wide", page_icon="📅"
    )
    st.title("Positional Options")
    paths = load_paths()
    render(st, paths.config_root, paths.database_path(_RUNTIME_ID))


if __name__ == "__main__":  # pragma: no cover
    main()
