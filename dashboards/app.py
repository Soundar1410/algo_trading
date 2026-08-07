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

**Live-gate status is the one deliberate exception to "the database only".**
Global/runtime/strategy live-gate flags live in ``config/*.yaml``, not SQLite
— :func:`~common.config.effective_live_gate` takes a ``ResolvedConfig``, which
only a config read can produce. This is not the resource the two constraints
above are about: it opens no database write connection, no broker, no feed —
:mod:`common.config` (and everything it imports transitively) depends only on
``pydantic``/``yaml``/the standard library, verified directly rather than
assumed. ``tests/unit/test_dashboard.py``'s AST check still passes unmodified,
because it looks for broker/feed/write-``Database`` imports specifically, not
for config reads. Isolated into its own loader and its own failure type
(:class:`ConfigUnavailable`) so a broken YAML file degrades only this section,
never the snapshot-backed rest of the page.

Data functions are importable and tested directly; Streamlit is imported
lazily so the test suite never needs it at collection time.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from common.config import (
    ConfigError,
    ExecutionMode,
    Settings,
    discover_enabled_strategies,
    effective_live_gate,
    load_global_config,
    load_runtime_config,
    load_settings,
)
from common.health import HealthSnapshot

from ._shared import SnapshotUnavailable, load_snapshot

#: Spec section 9 requires reconciliation status on the Master page.
#: Reconciliation itself is Phase 10 throughout the spec (controlled-live
#: only) — shown explicitly rather than as a blank so an operator does not
#: mistake "not built yet" for "nothing to reconcile".
RECONCILIATION_STATUS = "Not implemented (Phase 10 — controlled live)"


@dataclass(frozen=True)
class ConfigUnavailable:
    """Why the live-gate section has nothing to show. Rendered as a message,
    never raised — a broken YAML file must degrade only this section, not the
    snapshot-backed rest of the page."""

    reason: str


@dataclass(frozen=True)
class StrategyLiveGate:
    """One live-mode strategy's gate outcome, from the real production check."""

    strategy_id: str
    allowed: bool
    blocked_reasons: tuple[str, ...]


@dataclass(frozen=True)
class LiveGateStatus:
    """Global and runtime live-gate status (spec's Master-page bullet),
    plus every currently-enabled live-mode strategy's own gate outcome.

    ``global_live_trading_enabled``/``runtime_live_execution_allowed`` are the
    two account/runtime-level permissions read directly off config — the
    layer the spec bullet names specifically as distinct from strategy-level.
    ``live_strategies`` reuses :func:`~common.config.effective_live_gate`
    itself (not a re-derivation of its AND-chain) for every *enabled*
    strategy whose ``mode`` is ``live``, so what this page shows is exactly
    what the supervisor's own admission gate would decide.
    """

    global_live_trading_enabled: bool
    runtime_live_execution_allowed: bool
    live_strategies: tuple[StrategyLiveGate, ...]


@dataclass(frozen=True)
class RuntimeCard:
    """One runtime group's summary — the Master page's one card.

    Deliberately singular today: this platform has exactly one real runtime
    (``intraday_options``); ``positional_options`` and ``intraday_stocks``
    have no supervisor, no database and their own stub pages. ``load_master``
    takes one ``runtime_id`` rather than discovering "every configured
    runtime" because that would mean globbing ``config/runtimes/*.yaml`` for a
    list that has exactly one real entry today. A future second runtime calls
    this once per group, the same way ``main`` below would loop.

    ``disabled_count`` is deliberately absent even though this page now reads
    some config (see ``live_gate`` below): :func:`~common.config.
    discover_enabled_strategies` returns only *enabled* strategies by design,
    so it cannot answer "how many are disabled" either — that would still
    need a third read (globbing every ``config/strategies/*.yaml`` file and
    checking each), for one field spec asks for and runtime state alone
    cannot answer. Not shown, not faked as zero.

    ``live_gate`` is ``None`` when the caller did not ask for it (``load_
    master`` without ``config_root`` — every existing call before this field
    existed keeps working unchanged), :class:`ConfigUnavailable` when config
    could not be read, otherwise a real :class:`LiveGateStatus`.
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
    live_gate: LiveGateStatus | ConfigUnavailable | None = None


def load_live_gate_status(
    config_root: Path | str, runtime_id: str, settings: Settings | None = None
) -> LiveGateStatus | ConfigUnavailable:
    """Global/runtime live-gate flags, plus every enabled live-mode strategy's
    real gate outcome — a pure config read, no database involved.

    A missing or malformed ``config/runtimes/<runtime_id>.yaml`` (the runtime
    simply not configured yet, or a typo) is exactly as unexceptional here as
    a missing database is to :func:`~dashboards._shared.load_snapshot` —
    caught and returned as data, not raised.
    """
    settings = settings if settings is not None else load_settings()
    config_root = Path(config_root)
    try:
        global_config = load_global_config(config_root)
        runtime_config = load_runtime_config(config_root, runtime_id)
        live_strategies = tuple(
            StrategyLiveGate(
                strategy_id=cfg.strategy.strategy_id,
                allowed=(gate := effective_live_gate(cfg)).allowed,
                blocked_reasons=gate.blocked_reasons,
            )
            for cfg in discover_enabled_strategies(config_root, runtime_id, settings=settings)
            if cfg.strategy.mode is ExecutionMode.LIVE
        )
    except ConfigError as exc:
        return ConfigUnavailable(str(exc))
    return LiveGateStatus(
        global_live_trading_enabled=global_config.live_trading_enabled,
        runtime_live_execution_allowed=runtime_config.live_execution_allowed,
        live_strategies=live_strategies,
    )


def load_master(
    database_path: Path | str,
    runtime_id: str,
    trading_date: str,
    *,
    config_root: Path | str | None = None,
    settings: Settings | None = None,
) -> RuntimeCard | SnapshotUnavailable:
    """Build the one runtime card this page shows, or say why it cannot.

    ``config_root`` is optional and additive: omitted (every call site before
    live-gate status existed, and every existing test), the card's
    ``live_gate`` stays ``None`` and the section is not shown — a config
    read failure can never turn into "no snapshot either" by omitting it.
    """
    result = load_snapshot(database_path, runtime_id, trading_date)
    if isinstance(result, SnapshotUnavailable):
        return result
    card = _card_from_snapshot(result)
    if config_root is None:
        return card
    live_gate = load_live_gate_status(config_root, runtime_id, settings)
    return replace(card, live_gate=live_gate)


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


def _render_live_gate(streamlit: Any, live_gate: LiveGateStatus | ConfigUnavailable | None) -> None:
    """The live-gate section. A no-op when the caller never asked for it —
    see ``RuntimeCard.live_gate``'s docstring for why that is the default."""
    if live_gate is None:
        return
    streamlit.markdown("**Live-gate status**")
    if isinstance(live_gate, ConfigUnavailable):
        streamlit.warning(f"Live-gate status unavailable: {live_gate.reason}")
        return

    gate = streamlit.columns(2)
    gate[0].metric(
        "Global live trading",
        "enabled" if live_gate.global_live_trading_enabled else "disabled",
    )
    gate[1].metric(
        "Runtime live execution",
        "allowed" if live_gate.runtime_live_execution_allowed else "not allowed",
    )
    if not live_gate.live_strategies:
        streamlit.caption("No enabled strategy is configured for live mode.")
        return
    for strategy in live_gate.live_strategies:
        if strategy.allowed:
            streamlit.write(f"{strategy.strategy_id}: live-approved")
        else:
            reasons = "; ".join(strategy.blocked_reasons)
            streamlit.warning(f"{strategy.strategy_id}: blocked — {reasons}")


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

    _render_live_gate(streamlit, card.live_gate)

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

    render(
        st,
        load_master(database_path, runtime_id, trading_date, config_root=paths.config_root),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
