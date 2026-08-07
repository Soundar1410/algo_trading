"""Read-only Streamlit page — Intraday Options.

Per-strategy detail for the ``intraday_options`` runtime group: explicit
``PAPER``/``LIVE`` badge, worker PID, heartbeat, entry-cutoff/square-off
state, open positions — everything :mod:`common.health.snapshot` actually
has, sourced from :class:`~common.health.snapshot.StrategyHealth`.

**What this page does not show, and why.** Spec's Intraday Options page also
asks for engine type, open legs and baskets, selected strikes and expiry,
per-leg P&L and roll count. None of that is shown: those describe
``MultiLegEngine``/``FixedStrikeEngine``, and per the runbook's D56/D34 (the
Trading_Automation reuse audit), neither engine is ported into this
codebase yet — there is no data to read. Inventing fields for an engine that
does not exist would be exactly the "looks finished but isn't" pattern the
runbook already declines elsewhere. Revisit once a real multi-leg or
fixed-strike strategy exists to answer what those columns should show.

Read-only/no-side-effect discipline is identical to ``dashboards/app.py`` —
see that module's docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.health import HealthSnapshot, StrategyHealth

from ._shared import SnapshotUnavailable, load_snapshot

#: Fields the spec asks for on this page that no ported engine produces yet.
#: Named explicitly, once, so the page can say so rather than staying silent
#: about the gap — see the module docstring.
NOT_YET_AVAILABLE = (
    "engine type, open legs/baskets, selected strikes/expiry, per-leg P&L "
    "and roll count are not shown: MultiLegEngine/FixedStrikeEngine are not "
    "ported into this codebase yet (runbook D56/D34)."
)


def mode_label(execution_mode: str | None) -> str:
    """Always explicit. An operator must never guess whether this is real money."""
    if execution_mode == "paper":
        return "PAPER (simulated)"
    if execution_mode == "live":
        return "LIVE"
    return "UNKNOWN"


@dataclass(frozen=True)
class StrategyRow:
    """One strategy's row on this page."""

    strategy_id: str
    execution_mode: str | None
    health_state: str
    heartbeat_age_seconds: float | None
    pid: int | None
    square_off_state: str | None
    entries_blocked: bool | None
    open_positions: int
    last_error: str | None

    @property
    def mode_label(self) -> str:
        return mode_label(self.execution_mode)


@dataclass(frozen=True)
class IntradayOptionsView:
    runtime_id: str
    realised_pnl_paper: float
    realised_pnl_live: float
    strategies: tuple[StrategyRow, ...]


def load_intraday_options(
    database_path: Path | str, runtime_id: str, trading_date: str
) -> IntradayOptionsView | SnapshotUnavailable:
    result = load_snapshot(database_path, runtime_id, trading_date)
    if isinstance(result, SnapshotUnavailable):
        return result
    return _view_from_snapshot(result)


def _view_from_snapshot(snapshot: HealthSnapshot) -> IntradayOptionsView:
    return IntradayOptionsView(
        runtime_id=snapshot.runtime_id,
        realised_pnl_paper=snapshot.realised_pnl_paper,
        realised_pnl_live=snapshot.realised_pnl_live,
        strategies=tuple(_row(s) for s in snapshot.strategies),
    )


def _row(strategy: StrategyHealth) -> StrategyRow:
    return StrategyRow(
        strategy_id=strategy.strategy_id,
        execution_mode=strategy.execution_mode,
        health_state=strategy.health_state,
        heartbeat_age_seconds=strategy.heartbeat_age_seconds,
        pid=strategy.pid,
        square_off_state=strategy.square_off_state,
        entries_blocked=strategy.entries_blocked,
        open_positions=strategy.open_positions,
        last_error=strategy.last_error,
    )


def render(streamlit: Any, result: IntradayOptionsView | SnapshotUnavailable) -> None:
    if isinstance(result, SnapshotUnavailable):
        streamlit.info(result.reason)
        return

    view = result
    streamlit.subheader(f"{view.runtime_id} — Intraday Options")

    pnl = streamlit.columns(2)
    pnl[0].metric("Realised P&L — paper", f"{view.realised_pnl_paper:,.2f}")
    pnl[1].metric("Realised P&L — live", f"{view.realised_pnl_live:,.2f}")

    if not view.strategies:
        streamlit.info("No strategy has reported a heartbeat yet.")
        return

    for strategy in view.strategies:
        streamlit.markdown(f"**{strategy.strategy_id} — {strategy.mode_label}**")
        row = streamlit.columns(5)
        row[0].metric("Health", strategy.health_state)
        row[1].metric(
            "Heartbeat age",
            "—"
            if strategy.heartbeat_age_seconds is None
            else f"{strategy.heartbeat_age_seconds:.0f}s",
        )
        row[2].metric("PID", strategy.pid if strategy.pid is not None else "—")
        row[3].metric("Open positions", strategy.open_positions)
        row[4].metric("Square-off", strategy.square_off_state or "—")
        if strategy.entries_blocked:
            streamlit.warning(f"{strategy.strategy_id}: entries blocked")
        if strategy.last_error:
            streamlit.error(f"{strategy.strategy_id}: {strategy.last_error}")

    streamlit.caption(NOT_YET_AVAILABLE)


def main() -> None:  # pragma: no cover - exercised manually via `streamlit run`
    import datetime as _dt

    import streamlit as st

    from common.config import load_paths

    st.set_page_config(page_title="algo_trading — Intraday Options", layout="wide")
    st.title("Intraday Options")

    paths = load_paths()
    runtime_id = "intraday_options"
    database_path = paths.database_path(runtime_id)
    trading_date = _dt.date.today().isoformat()

    render(st, load_intraday_options(database_path, runtime_id, trading_date))


if __name__ == "__main__":  # pragma: no cover
    main()
