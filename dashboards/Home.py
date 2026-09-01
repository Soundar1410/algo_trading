"""Read-only Streamlit dashboard — Home command centre.

``streamlit run dashboards/Home.py`` is this platform's one entry point; the
other four pages (``dashboards/pages/``) are Streamlit's native multipage
convention, auto-discovered from this script's directory. Home is a compact
command centre (spec: total strategies, running/stopped/degraded/disabled
counts, paper vs. live P&L kept visually separate, open positions, orders
today, DB/feed health, active incidents, recent alerts, a 30-day paper-
forward-testing rollup) plus three clickable category cards
(``st.page_link``) into Intraday Options / Positional Options / Intraday
Stocks.

Two constraints from the spec shape every page in this package, this one
included — see ``dashboards/_shared.py``'s module docstring for the full
argument:

* **The dashboard is read-only.** Every page reads through
  :func:`~dashboards._shared.load_snapshot`/:func:`~dashboards._shared.run_bounded`,
  both backed by :func:`~common.persistence.database.connect_readonly`.
* **The dashboard never opens a market-data connection, a broker, or a
  write connection.** ``tests/unit/test_dashboard.py`` AST-walks every
  module in this package to enforce it.

**Live-gate status is the one deliberate exception to "the database only".**
Global/runtime/strategy live-gate flags and the disabled-strategy count live
in ``config/*.yaml``, not SQLite. Isolated into ``dashboards/data/account.py``
so a broken YAML file degrades only that section, never the snapshot-backed
rest of the page.

**Which runtime a category card reads is discovered, not hardcoded.** Each
of the three cards resolves its own ``config/runtimes/<runtime_id>.yaml``
and only attempts a database read if that runtime is configured *and*
enabled — a future real ``positional_options``/``intraday_stocks`` runtime
lights the same card up with no code change here (spec: "do not hardcode
the master dashboard permanently to intraday_options").

Data functions are importable and tested directly; Streamlit is imported
lazily so the test suite never needs it at collection time.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from common.config import ConfigError, Settings, load_runtime_config, load_settings
from common.utils.timeutils import DEFAULT_TZ, get_tz

# ``streamlit run dashboards/Home.py`` executes this file directly as
# ``__main__`` — see the original module's long-standing note (now here)
# on why the sys.path fix-up below is required before any ``dashboards.*``
# import, and is a no-op when this module is imported normally as a package
# member (e.g. by the test suite).
for _parent in Path(__file__).resolve().parents:
    if (_parent / "pyproject.toml").is_file():
        if str(_parent) not in sys.path:
            sys.path.insert(0, str(_parent))
        break

from dashboards._shared import SnapshotUnavailable, load_snapshot, run_bounded  # noqa: E402
from dashboards.data import positional as positional_data  # noqa: E402
from dashboards.data import stocks as stocks_data  # noqa: E402
from dashboards.data.account import (  # noqa: E402
    ConfigUnavailable,
    LiveGateMatrix,
    load_account_status,
    load_live_gate_matrix,
    render_account_status,
)
from dashboards.data.calendar_stats import (  # noqa: E402
    ThirtyDayRollup,
    build_thirty_day_rollup,
    merge_daily_outcomes,
)
from dashboards.data.incidents import IncidentRow, classify_incidents  # noqa: E402
from dashboards.data.intraday_options import (  # noqa: E402
    EventErrorRow,
    OverviewRow,
    load_daily_outcomes,
    load_errors,
    load_inception_date,
    load_overview,
)
from dashboards.formatting import (  # noqa: E402
    colored,
    format_age,
    format_inr,
    format_ist,
    format_ist_time_only,
)

_IST = get_tz(DEFAULT_TZ)

#: (category label, runtime_id, page path for st.page_link, not-configured message)
#: ``st.page_link`` resolves its path relative to the entrypoint script's
#: own directory (``dashboards/``, since ``streamlit run dashboards/Home.py``
#: is the entrypoint) — *not* relative to the repository root. Getting this
#: wrong raises ``StreamlitPageNotFoundError`` at render time; only an
#: AppTest-driven smoke test catches it, since every other test in this
#: package drives ``render()`` with a fake streamlit that never validates
#: the path at all.
_CATEGORIES: tuple[tuple[str, str, str, str], ...] = (
    (
        "Intraday Options",
        "intraday_options",
        "pages/1_Intraday_Options.py",
        "No configuration found for this runtime group.",
    ),
    (
        "Positional Options",
        "positional_options",
        "pages/2_Positional_Options.py",
        positional_data.NOT_CONFIGURED,
    ),
    (
        "Intraday Stocks",
        "intraday_stocks",
        "pages/3_Intraday_Stocks.py",
        stocks_data.NOT_CONFIGURED,
    ),
)

_HEALTHY_STATES = {"RUNNING_PAPER", "RUNNING_LIVE", "RUNNING"}
_DEGRADED_STATES = {"DEGRADED", "STALE", "FAILED", "CRASHED"}


def _bucket(health_state: str | None) -> str:
    if health_state in _HEALTHY_STATES:
        return "running"
    if health_state in _DEGRADED_STATES:
        return "degraded"
    return "stopped"


@dataclass(frozen=True)
class CategoryCard:
    """One category's summary — spec: "one clickable card per runtime
    category", with status/strategies/mode/today's P&L/open positions/
    heartbeat/error count, paper and live never blended."""

    category: str
    page_path: str
    configured: bool
    status_label: str
    detail: str
    strategies_running: int
    strategies_total: int
    realised_pnl_paper: float | None
    realised_pnl_live: float | None
    open_positions: int | None
    last_heartbeat_age_seconds: float | None
    active_error_count: int


def _build_category_card(
    category: str,
    runtime_id: str,
    page_path: str,
    not_configured_message: str,
    *,
    config_root: Path,
    database_path: Path,
    trading_date: str,
    settings: Settings,
) -> CategoryCard:
    try:
        runtime_config = load_runtime_config(config_root, runtime_id)
    except ConfigError:
        runtime_config = None

    if runtime_config is None or not runtime_config.enabled:
        return CategoryCard(
            category=category,
            page_path=page_path,
            configured=False,
            status_label="NOT CONFIGURED",
            detail=not_configured_message,
            strategies_running=0,
            strategies_total=0,
            realised_pnl_paper=None,
            realised_pnl_live=None,
            open_positions=None,
            last_heartbeat_age_seconds=None,
            active_error_count=0,
        )

    result = load_snapshot(database_path, runtime_id, trading_date)
    if isinstance(result, SnapshotUnavailable):
        return CategoryCard(
            category=category,
            page_path=page_path,
            configured=True,
            status_label="STOPPED",
            detail=result.reason,
            strategies_running=0,
            strategies_total=0,
            realised_pnl_paper=None,
            realised_pnl_live=None,
            open_positions=None,
            last_heartbeat_age_seconds=None,
            active_error_count=0,
        )

    snapshot = result
    buckets = [_bucket(s.health_state) for s in snapshot.strategies]
    running = buckets.count("running")
    group_state = snapshot.group.health_state if snapshot.group else None
    if group_state in _HEALTHY_STATES and running == len(buckets) and buckets:
        status_label = "HEALTHY"
    elif group_state in _HEALTHY_STATES or running:
        status_label = "DEGRADED"
    else:
        status_label = "STOPPED"

    error_rows = load_errors_bounded(database_path, runtime_id)
    strategy_states = {s.strategy_id: s.health_state for s in snapshot.strategies}
    active_errors = 0
    if not isinstance(error_rows, SnapshotUnavailable):
        classified = classify_incidents(
            error_rows,
            broker_healthy=snapshot.broker.healthy,
            database_healthy=snapshot.database.integrity_ok,
            feed_currently_ok=(
                snapshot.market_data.subscriptions_match
                and snapshot.market_data.last_event
                not in {"disconnected", "reconnect_exhausted"}
            ),
            strategy_health_states=strategy_states,
        )
        active_errors = sum(1 for row in classified if row.active)

    return CategoryCard(
        category=category,
        page_path=page_path,
        configured=True,
        status_label=status_label,
        detail="",
        strategies_running=running,
        strategies_total=len(buckets),
        realised_pnl_paper=snapshot.realised_pnl_paper,
        realised_pnl_live=snapshot.realised_pnl_live,
        open_positions=snapshot.open_positions,
        last_heartbeat_age_seconds=(
            snapshot.group.heartbeat_age_seconds if snapshot.group else None
        ),
        active_error_count=active_errors,
    )


def load_errors_bounded(
    database_path: Path, runtime_id: str
) -> tuple[EventErrorRow, ...] | SnapshotUnavailable:
    return run_bounded(database_path, lambda conn: load_errors(conn, runtime_id))


@dataclass(frozen=True)
class HomeView:
    generated_at_ist: str
    market_status: str
    total_strategies: int
    running_count: int
    degraded_count: int
    stopped_count: int
    disabled_count: int
    realised_pnl_paper: float
    realised_pnl_live: float
    open_positions: int
    orders_today: int
    database_healthy: bool | None
    feed_status: str | None
    active_incidents: tuple[IncidentRow, ...]
    category_cards: tuple[CategoryCard, ...]
    thirty_day: ThirtyDayRollup | None
    live_gate_matrix: LiveGateMatrix | ConfigUnavailable | None


def market_status(now: datetime) -> str:
    """OPEN/CLOSED from IST weekday + NSE session hours only — no exchange
    holiday calendar exists anywhere in this project's config today (see
    ``dashboards/data/calendar_stats.py``'s module caveat)."""
    local = now.astimezone(_IST)
    if local.weekday() >= 5:
        return "CLOSED"
    minutes = local.hour * 60 + local.minute
    return "OPEN" if 9 * 60 + 15 <= minutes <= 15 * 60 + 30 else "CLOSED"


def load_home(
    *,
    config_root: Path,
    operational_root: Path,
    trading_date: str,
    settings: Settings | None = None,
) -> HomeView:
    settings = settings if settings is not None else load_settings()
    now = datetime.now(_IST)
    today = date.fromisoformat(trading_date)

    primary_database = operational_root / "intraday_options.db"
    primary_result = load_snapshot(primary_database, "intraday_options", trading_date)

    live_gate_matrix = load_live_gate_matrix(config_root, "intraday_options", settings)
    if isinstance(live_gate_matrix, ConfigUnavailable):
        total_strategies = 0
        disabled_count = 0
        enabled_strategy_ids: tuple[str, ...] = ()
    else:
        total_strategies = len(live_gate_matrix.rows)
        disabled_count = live_gate_matrix.disabled_count
        enabled_strategy_ids = tuple(
            row.strategy_id for row in live_gate_matrix.rows if row.strategy_enabled
        )

    running_count = degraded_count = stopped_count = 0
    realised_pnl_paper = realised_pnl_live = 0.0
    open_positions = orders_today = 0
    database_healthy: bool | None = None
    feed_status: str | None = None
    active_incidents: tuple[IncidentRow, ...] = ()

    overview_rows: tuple[OverviewRow, ...] = ()
    if not isinstance(primary_result, SnapshotUnavailable):
        snapshot = primary_result
        overview_by_id = {}

        overview_result = run_bounded(
            primary_database, lambda conn: load_overview(conn, "intraday_options", trading_date)
        )
        if not isinstance(overview_result, SnapshotUnavailable):
            overview_rows = overview_result
            overview_by_id = {row.strategy_id: row for row in overview_rows}

        for strategy_id in enabled_strategy_ids:
            row = overview_by_id.get(strategy_id)
            bucket = _bucket(row.health_state if row else None)
            if bucket == "running":
                running_count += 1
            elif bucket == "degraded":
                degraded_count += 1
            else:
                stopped_count += 1

        realised_pnl_paper = snapshot.realised_pnl_paper
        realised_pnl_live = snapshot.realised_pnl_live
        open_positions = snapshot.open_positions
        orders_today = snapshot.orders_today
        database_healthy = snapshot.database.integrity_ok
        feed_status = snapshot.market_data.last_event

        error_rows = run_bounded(
            primary_database, lambda conn: load_errors(conn, "intraday_options")
        )
        if not isinstance(error_rows, SnapshotUnavailable):
            strategy_states = {s.strategy_id: s.health_state for s in snapshot.strategies}
            classified = classify_incidents(
                error_rows,
                broker_healthy=snapshot.broker.healthy,
                database_healthy=snapshot.database.integrity_ok,
                feed_currently_ok=(
                    snapshot.market_data.subscriptions_match
                    and snapshot.market_data.last_event
                    not in {"disconnected", "reconnect_exhausted"}
                ),
                strategy_health_states=strategy_states,
            )
            active_incidents = tuple(row for row in classified if row.active)
    else:
        stopped_count = len(enabled_strategy_ids)

    thirty_day: ThirtyDayRollup | None = None
    inception = run_bounded(
        primary_database, lambda conn: load_inception_date(conn, "intraday_options")
    )
    if not isinstance(inception, SnapshotUnavailable) and inception is not None:
        window_start = max(inception, today - timedelta(days=29))
        outcomes = run_bounded(
            primary_database,
            lambda conn: tuple(
                daily
                for strategy_id in enabled_strategy_ids
                for daily in load_daily_outcomes(
                    conn,
                    "intraday_options",
                    strategy_id=strategy_id,
                    execution_mode=None,
                    start_date=window_start.isoformat(),
                    end_date=today.isoformat(),
                )
            ),
        )
        if not isinstance(outcomes, SnapshotUnavailable):
            combined = merge_daily_outcomes(outcomes)
            thirty_day = build_thirty_day_rollup(combined, inception_date=inception, today=today)

    cards = tuple(
        _build_category_card(
            category,
            runtime_id,
            page_path,
            not_configured_message,
            config_root=config_root,
            database_path=operational_root / f"{runtime_id}.db",
            trading_date=trading_date,
            settings=settings,
        )
        for category, runtime_id, page_path, not_configured_message in _CATEGORIES
    )

    return HomeView(
        generated_at_ist=now.isoformat(),
        market_status=market_status(now),
        total_strategies=total_strategies,
        running_count=running_count,
        degraded_count=degraded_count,
        stopped_count=stopped_count,
        disabled_count=disabled_count,
        realised_pnl_paper=realised_pnl_paper,
        realised_pnl_live=realised_pnl_live,
        open_positions=open_positions,
        orders_today=orders_today,
        database_healthy=database_healthy,
        feed_status=feed_status,
        active_incidents=active_incidents,
        category_cards=cards,
        thirty_day=thirty_day,
        live_gate_matrix=live_gate_matrix,
    )


# =================================================================== render
def _status_color(label: str) -> str:
    return {
        "HEALTHY": "green",
        "DEGRADED": "orange",
        "STOPPED": "gray",
        "NOT CONFIGURED": "gray",
    }.get(label, "gray")


def _render_category_card(streamlit: Any, card: CategoryCard) -> None:
    with streamlit.container(border=True):
        streamlit.markdown(f"### {card.category}")
        streamlit.markdown(colored(f"● {card.status_label}", _status_color(card.status_label)))
        if not card.configured:
            streamlit.caption(card.detail)
        else:
            streamlit.write(
                f"Strategies: {card.strategies_running}/{card.strategies_total} running"
            )
            row = streamlit.columns(2)
            row[0].metric("Paper P&L today", format_inr(card.realised_pnl_paper))
            row[1].metric("Live P&L today", format_inr(card.realised_pnl_live))
            streamlit.write(
                "Open positions: "
                f"{card.open_positions if card.open_positions is not None else '—'}"
            )
            streamlit.caption(
                f"Last heartbeat: {format_age(card.last_heartbeat_age_seconds)} ago · "
                f"Active errors: {card.active_error_count}"
            )
            if card.detail:
                streamlit.caption(card.detail)
        streamlit.page_link(card.page_path, label=f"Open {card.category} →")


def _render_thirty_day(streamlit: Any, rollup: ThirtyDayRollup | None) -> None:
    streamlit.subheader("30-day paper forward testing")
    if rollup is None:
        streamlit.info(
            "No runtime activity has been recorded yet — the 30-day clock has not started."
        )
        return
    cols = streamlit.columns(4)
    cols[0].metric("Trading days elapsed", rollup.trading_days_elapsed)
    cols[1].metric("Days executed", rollup.days_executed)
    cols[2].metric("Days with trades", rollup.days_with_trades)
    cols[3].metric("No-trade days", rollup.no_trade_days)
    cols2 = streamlit.columns(3)
    cols2[0].metric("Profitable days", rollup.profitable_days)
    cols2[1].metric("Loss-making days", rollup.loss_making_days)
    cols2[2].metric("Cumulative net P&L", format_inr(rollup.cumulative_net_pnl))
    if rollup.data_complete:
        streamlit.caption(f"✅ {rollup.completeness_note}")
    else:
        streamlit.warning(rollup.completeness_note)


def render(streamlit: Any, view: HomeView) -> None:
    top = streamlit.columns(4)
    top[0].metric("Market", view.market_status)
    # Time-only, not the full date — "2026-08-14 15:47 IST" clips in a
    # quarter-width metric column; the date rarely matters for a value that
    # refreshes every 30s, and the full ISO timestamp is one click away in
    # any export.
    top[1].metric("Last refresh (IST)", format_ist_time_only(view.generated_at_ist))
    top[2].metric("Configured strategies", view.total_strategies)
    top[3].metric("Open positions", view.open_positions)

    counts = streamlit.columns(4)
    counts[0].metric("Running", view.running_count)
    counts[1].metric("Degraded", view.degraded_count)
    counts[2].metric("Stopped", view.stopped_count)
    counts[3].metric("Disabled", view.disabled_count)

    pnl = streamlit.columns(3)
    pnl[0].metric("Realised P&L — paper", format_inr(view.realised_pnl_paper))
    pnl[1].metric("Realised P&L — live", format_inr(view.realised_pnl_live))
    pnl[2].metric("Orders today", view.orders_today)
    streamlit.caption(
        "Paper and live P&L are never combined into one figure. Unrealised "
        "P&L is shown only where a real mark is persisted (System Health / "
        "account-wide risk) — no paper mark-to-market is recorded today."
    )

    if view.database_healthy is None:
        database_label = "—"
    elif view.database_healthy:
        database_label = "ok"
    else:
        database_label = "problem"
    health = streamlit.columns(2)
    health[0].metric("Database", database_label)
    health[1].metric("Market feed", view.feed_status or "no data yet")

    if view.active_incidents:
        streamlit.subheader(f"Active incidents ({len(view.active_incidents)})")
        for incident in view.active_incidents:
            streamlit.error(
                f"{format_ist(incident.occurred_at)} — [{incident.component}] {incident.message}"
            )
    else:
        streamlit.success("No active incidents.")

    streamlit.subheader("Categories")
    cols = streamlit.columns(3)
    for col, card in zip(cols, view.category_cards, strict=True):
        with col:
            _render_category_card(streamlit, card)

    _render_thirty_day(streamlit, view.thirty_day)


def main() -> None:  # pragma: no cover - exercised manually via `streamlit run`
    import streamlit as st

    from common.config import load_paths

    st.set_page_config(page_title="algo_trading — Home", layout="wide", page_icon="📊")
    st.title("algo_trading")
    st.caption(
        "Read-only. Paper forward testing on live market data. "
        "Controlled-live code exists but every committed live gate is disabled."
    )

    if "auto_refresh" not in st.session_state:
        st.session_state["auto_refresh"] = True

    with st.sidebar:
        st.session_state["auto_refresh"] = st.checkbox(
            "Auto-refresh (30s)", value=st.session_state["auto_refresh"]
        )
        if st.button("Refresh now"):
            st.rerun()

    paths = load_paths()
    trading_date = date.today().isoformat()

    @st.fragment(run_every=30 if st.session_state["auto_refresh"] else None)
    def _body() -> None:
        view = load_home(
            config_root=paths.config_root,
            operational_root=paths.operational_root,
            trading_date=trading_date,
        )
        render(st, view)
        render_account_status(
            st, load_account_status(paths.account_shared_database_path, trading_date=trading_date)
        )

    _body()


if __name__ == "__main__":  # pragma: no cover
    main()
