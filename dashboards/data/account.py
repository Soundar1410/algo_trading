"""Read-model: reconciliation, account-wide risk and the live-gate matrix.

Moved out of ``dashboards/app.py`` (Phase 7 Part 3) unchanged in behaviour,
plus the full live-gate matrix and the disabled-strategy count the spec's
System Health page and Home page both need — so Home, Intraday Options'
Health tab and System Health all read the same functions rather than three
copies of the same SQL/config logic.

Same two "read-only, no side effect" rules as every other module in this
package: every database read goes through
:func:`~common.persistence.database.connect_readonly`; the one deliberate
exception is config (``load_global_config``/``load_runtime_config``/
``discover_enabled_strategies``/``effective_live_gate``), which opens no
database, no broker, no feed — see ``dashboards/app.py``'s module docstring
for the full argument, unchanged here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

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
from common.persistence import DatabaseError, connect_readonly
from dashboards._shared import SnapshotUnavailable

#: Spec section 9 requires reconciliation status on the Master page. Shown
#: for a card built without ever calling :func:`load_reconciliation_status` —
#: an explicit "not read" state, never confused with "nothing to reconcile".
RECONCILIATION_STATUS = "Not implemented (Phase 10 — controlled live)"


@dataclass(frozen=True)
class ReconciliationStatus:
    """The most recent reconciliation run for one runtime group. Read-only,
    straight off ``reconciliation_runs`` — never re-runs reconciliation
    itself (the dashboard must never own trading state)."""

    run_status: str  # 'running' | 'completed' | 'failed'
    critical_mismatch_count: int
    entries_blocked: bool
    started_at: str
    completed_at: str | None


def load_reconciliation_status(
    database_path: Path | str,
) -> ReconciliationStatus | SnapshotUnavailable | None:
    """The latest ``reconciliation_runs`` row, or why there is nothing to
    show. ``None`` means no reconciliation has ever run for this group —
    distinct from :class:`SnapshotUnavailable` (the database could not be
    read at all)."""
    db_path = Path(database_path)
    if not db_path.is_file():
        return SnapshotUnavailable(f"No database yet at {db_path}. Start the supervisor first.")
    try:
        conn = connect_readonly(db_path)
    except DatabaseError as exc:
        return SnapshotUnavailable(str(exc))
    try:
        row = conn.execute(
            "SELECT status, critical_mismatch_count, entries_blocked, started_at, "
            "completed_at FROM reconciliation_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as exc:
        return SnapshotUnavailable(f"Database not ready ({type(exc).__name__}): {exc}")
    finally:
        conn.close()
    if row is None:
        return None
    return ReconciliationStatus(
        run_status=row["status"],
        critical_mismatch_count=row["critical_mismatch_count"],
        entries_blocked=bool(row["entries_blocked"]),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def load_reconciliation_mismatches(
    database_path: Path | str, *, run_id: int, limit: int = 50
) -> tuple[sqlite3.Row, ...] | SnapshotUnavailable:
    """Every mismatch row for one reconciliation run, most recent first."""
    db_path = Path(database_path)
    if not db_path.is_file():
        return SnapshotUnavailable(f"No database yet at {db_path}.")
    try:
        conn = connect_readonly(db_path)
    except DatabaseError as exc:
        return SnapshotUnavailable(str(exc))
    try:
        rows = conn.execute(
            "SELECT * FROM reconciliation_mismatches WHERE run_id = ? "
            "ORDER BY detected_at DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()
    except sqlite3.Error as exc:
        return SnapshotUnavailable(f"Database not ready ({type(exc).__name__}): {exc}")
    finally:
        conn.close()
    return tuple(rows)


#: Reservation states whose projected_capital still counts as active
#: exposure — mirrors common.risk.account_reservations.ACTIVE_STATES,
#: repeated here (not imported) for the same reason
#: common.risk.account_risk repeats it: this module's SQL needs the literal
#: tuple, and this read-only page must not import common.risk (a risk-engine
#: module with its own Database-typed API this page cannot satisfy).
_ACTIVE_RESERVATION_STATES = (
    "RESERVED",
    "SUBMITTED",
    "ACKNOWLEDGED",
    "PARTIALLY_FILLED",
    "UNKNOWN",
    "RECONCILED",
)


@dataclass(frozen=True)
class AccountRow:
    """One Dhan account's account-wide risk and rate-limit picture — spans
    every runtime group sharing this ``account_key``, read from the *shared*
    account database, not any one runtime group's own database.

    ``has_unmarked_position`` is deliberately narrower than
    ``common.risk.account_risk.AccountExposureSnapshot.mtm_stale``: it means
    only "no mark has ever been recorded for this open position", not
    "the mark is older than a configured freshness bound".
    """

    account_key: str
    reconciliation_status: str
    realised_pnl_today: float
    unrealised_pnl: float
    has_unmarked_position: bool
    open_position_count: int
    open_positions_capital: float
    reserved_capital: float
    new_order_count_current_window: int


@dataclass(frozen=True)
class AccountWideStatus:
    trading_date: str
    accounts: tuple[AccountRow, ...]


def _account_row(conn: sqlite3.Connection, account_key: str, trading_date: str) -> AccountRow:
    provenance_row = conn.execute(
        "SELECT reconciliation_status FROM live_account_state_provenance WHERE account_key = ?",
        (account_key,),
    ).fetchone()
    reconciliation_status = (
        provenance_row["reconciliation_status"] if provenance_row else "never_reconciled"
    )

    realised = conn.execute(
        "SELECT COALESCE(SUM(realised_pnl_delta), 0) AS total FROM live_realised_pnl_events "
        "WHERE account_key = ? AND trading_date = ?",
        (account_key, trading_date),
    ).fetchone()["total"]

    positions = conn.execute(
        "SELECT security_id, deployed_capital FROM live_open_positions WHERE account_key = ?",
        (account_key,),
    ).fetchall()
    mtm_by_security = {
        row["security_id"]: row
        for row in conn.execute(
            "SELECT security_id, unrealised_pnl FROM live_position_mtm WHERE account_key = ?",
            (account_key,),
        )
    }
    unrealised = 0.0
    has_unmarked_position = False
    for position in positions:
        mark = mtm_by_security.get(position["security_id"])
        if mark is None:
            has_unmarked_position = True
            continue
        unrealised += mark["unrealised_pnl"]

    placeholders = ",".join("?" * len(_ACTIVE_RESERVATION_STATES))
    reserved = conn.execute(
        "SELECT COALESCE(SUM(projected_capital), 0) AS total FROM live_risk_reservations "
        f"WHERE account_key = ? AND state IN ({placeholders})",
        (account_key, *_ACTIVE_RESERVATION_STATES),
    ).fetchone()["total"]

    window_row = conn.execute(
        "SELECT count FROM live_order_rate_windows WHERE account_key = ? "
        "AND call_class = 'new_order' ORDER BY window_start DESC LIMIT 1",
        (account_key,),
    ).fetchone()

    return AccountRow(
        account_key=account_key,
        reconciliation_status=reconciliation_status,
        realised_pnl_today=realised,
        unrealised_pnl=unrealised,
        has_unmarked_position=has_unmarked_position,
        open_position_count=len(positions),
        open_positions_capital=sum(p["deployed_capital"] for p in positions),
        reserved_capital=reserved,
        new_order_count_current_window=window_row["count"] if window_row else 0,
    )


def load_account_status(
    database_path: Path | str, *, trading_date: str
) -> AccountWideStatus | SnapshotUnavailable:
    """Account-wide risk and rate-limit state from the *shared* account
    database. A missing file means no live worker has ever run yet — never
    "zero accounts, all clear"."""
    db_path = Path(database_path)
    if not db_path.is_file():
        return SnapshotUnavailable(f"No account-shared database yet at {db_path}.")
    try:
        conn = connect_readonly(db_path)
    except DatabaseError as exc:
        return SnapshotUnavailable(str(exc))
    try:
        keys: set[str] = set()
        for table in (
            "live_account_state_provenance",
            "live_risk_reservations",
            "live_open_positions",
        ):
            keys.update(
                row["account_key"]
                for row in conn.execute(f"SELECT DISTINCT account_key FROM {table}")
            )
        accounts = tuple(_account_row(conn, key, trading_date) for key in sorted(keys))
    except sqlite3.Error as exc:
        return SnapshotUnavailable(f"Database not ready ({type(exc).__name__}): {exc}")
    finally:
        conn.close()
    return AccountWideStatus(trading_date=trading_date, accounts=accounts)


@dataclass(frozen=True)
class ConfigUnavailable:
    """Why a config-backed section has nothing to show. Rendered as a
    message, never raised — a broken YAML file must degrade only that
    section, never the rest of a page."""

    reason: str


@dataclass(frozen=True)
class StrategyLiveGate:
    """One live-mode strategy's gate outcome, from the real production check."""

    strategy_id: str
    allowed: bool
    blocked_reasons: tuple[str, ...]


@dataclass(frozen=True)
class LiveGateStatus:
    """Global and runtime live-gate status, plus every currently-enabled
    live-mode strategy's own gate outcome (the supervisor's own admission
    gate, not a re-derivation of it)."""

    global_live_trading_enabled: bool
    runtime_live_execution_allowed: bool
    live_strategies: tuple[StrategyLiveGate, ...]


def load_live_gate_status(
    config_root: Path | str, runtime_id: str, settings: Settings | None = None
) -> LiveGateStatus | ConfigUnavailable:
    """Global/runtime live-gate flags, plus every enabled live-mode
    strategy's real gate outcome — a pure config read, no database involved."""
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


@dataclass(frozen=True)
class LiveGateMatrixRow:
    """One configured strategy's full live-gate picture (System Health's
    "live-gate matrix", spec section on System Health).

    ``effective_result`` is a prominent, unambiguous label — never a bare
    boolean an operator has to interpret under time pressure.
    """

    strategy_id: str
    strategy_enabled: bool
    mode: str  # "paper" | "live"
    live_approved: bool
    global_live_trading_enabled: bool
    runtime_live_execution_allowed: bool
    effective_result: str
    blocked_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class LiveGateMatrix:
    rows: tuple[LiveGateMatrixRow, ...]

    @property
    def disabled_count(self) -> int:
        return sum(1 for row in self.rows if not row.strategy_enabled)

    @property
    def enabled_count(self) -> int:
        return sum(1 for row in self.rows if row.strategy_enabled)


def _raw_strategy_files(config_root: Path) -> list[dict[str, object]]:
    """Every ``config/strategies/*.yaml`` file's own ``strategy_id``/
    ``enabled``/``mode``/``live_approved`` fields, parsed directly rather
    than through :func:`~common.config.discover_enabled_strategies` — that
    function returns only *enabled* strategies (by design; see its own
    docstring), so it cannot answer "how many are disabled" either. A file
    that fails to parse is skipped, not guessed at — this table is a
    completeness view, not a safety gate."""
    strategies_dir = config_root / "strategies"
    if not strategies_dir.is_dir():
        return []
    rows: list[dict[str, object]] = []
    for path in sorted(strategies_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(data, dict) or "strategy_id" not in data:
            continue
        rows.append(data)
    return rows


def load_live_gate_matrix(
    config_root: Path | str, runtime_id: str, settings: Settings | None = None
) -> LiveGateMatrix | ConfigUnavailable:
    """Every configured strategy (enabled or not, paper or live) with its
    full gate picture. Live-mode enabled strategies reuse
    :func:`~common.config.effective_live_gate` itself — the exact production
    check — rather than re-deriving its AND-chain by hand.
    """
    settings = settings if settings is not None else load_settings()
    config_root = Path(config_root)
    try:
        global_config = load_global_config(config_root)
        runtime_config = load_runtime_config(config_root, runtime_id)
        live_gates = {
            cfg.strategy.strategy_id: effective_live_gate(cfg)
            for cfg in discover_enabled_strategies(config_root, runtime_id, settings=settings)
            if cfg.strategy.mode is ExecutionMode.LIVE
        }
    except ConfigError as exc:
        return ConfigUnavailable(str(exc))

    rows = []
    for data in _raw_strategy_files(config_root):
        strategy_id = str(data.get("strategy_id"))
        enabled = bool(data.get("enabled", False))
        mode = str(data.get("mode", "paper"))
        live_approved = bool(data.get("live_approved", False))
        gate = live_gates.get(strategy_id)
        if not enabled:
            effective_result = "DISABLED"
            reasons: tuple[str, ...] = ()
        elif mode != "live":
            effective_result = "PAPER — simulated"
            reasons = ()
        elif gate is not None and gate.allowed:
            effective_result = "LIVE — real money"
            reasons = ()
        elif gate is not None:
            effective_result = "BLOCKED — fail-closed"
            reasons = gate.blocked_reasons
        else:
            # enabled + mode: live but not returned by discover_enabled_strategies
            # (e.g. it failed ResolvedConfig validation) — fail closed, not silent.
            effective_result = "BLOCKED — fail-closed"
            reasons = ("strategy configuration could not be resolved",)
        rows.append(
            LiveGateMatrixRow(
                strategy_id=strategy_id,
                strategy_enabled=enabled,
                mode=mode,
                live_approved=live_approved,
                global_live_trading_enabled=global_config.live_trading_enabled,
                runtime_live_execution_allowed=runtime_config.live_execution_allowed,
                effective_result=effective_result,
                blocked_reasons=reasons,
            )
        )
    return LiveGateMatrix(rows=tuple(rows))


# ==================================================================== render
#: Shared by ``dashboards/app.py`` and ``dashboards/system_health.py`` so the
#: account-wide section renders identically wherever it appears. Takes the
#: streamlit module as a duck-typed parameter (no import here) — the same
#: testability convention every ``render()`` in this package follows.
def render_account_status(streamlit: Any, status: AccountWideStatus | SnapshotUnavailable) -> None:
    streamlit.markdown("**Account-wide risk** (shared across every runtime group)")
    if isinstance(status, SnapshotUnavailable):
        streamlit.caption(f"Account-wide risk: unavailable ({status.reason})")
        return
    if not status.accounts:
        streamlit.caption("No live worker has recorded any account-shared state yet.")
        return
    for account in status.accounts:
        label = f"{account.account_key[:12]}…"
        if account.reconciliation_status != "reconciled":
            streamlit.warning(
                f"{label}: reconciliation status is {account.reconciliation_status!r} — "
                "new live entries are blocked account-wide until a full rebuild succeeds"
            )
        row = streamlit.columns(4)
        row[0].metric(
            "Daily P&L", f"{account.realised_pnl_today + account.unrealised_pnl:,.2f}"
        )
        row[1].metric("Open positions", account.open_position_count)
        row[2].metric(
            "Reserved + deployed capital",
            f"{account.reserved_capital + account.open_positions_capital:,.2f}",
        )
        row[3].metric("New-order calls (current window)", account.new_order_count_current_window)
        if account.has_unmarked_position:
            streamlit.caption(f"{label}: at least one open position has no mark-to-market yet.")


def render_reconciliation_status(
    streamlit: Any, status: ReconciliationStatus | SnapshotUnavailable | None
) -> None:
    """Read-only reflection of the latest ``reconciliation_runs`` row —
    never triggers a reconciliation run itself."""
    if status is None:
        streamlit.caption(f"Reconciliation: {RECONCILIATION_STATUS}")
        return
    if isinstance(status, SnapshotUnavailable):
        streamlit.caption(f"Reconciliation: unavailable ({status.reason})")
        return
    if status.run_status == "failed":
        streamlit.error(f"Reconciliation: last run FAILED at {status.started_at}")
    elif status.entries_blocked:
        streamlit.warning(
            f"Reconciliation: {status.critical_mismatch_count} critical mismatch(es) — "
            "new live entries blocked"
        )
    else:
        streamlit.caption(
            f"Reconciliation: {status.run_status}, "
            f"{status.critical_mismatch_count} critical mismatch(es), "
            f"last run {status.completed_at or status.started_at}"
        )


def render_live_gate_matrix(streamlit: Any, matrix: LiveGateMatrix | ConfigUnavailable) -> None:
    streamlit.markdown("**Live-gate matrix**")
    if isinstance(matrix, ConfigUnavailable):
        streamlit.warning(f"Live-gate matrix unavailable: {matrix.reason}")
        return
    if not matrix.rows:
        streamlit.caption("No strategy is configured.")
        return
    table = [
        {
            "Strategy": row.strategy_id,
            "Enabled": row.strategy_enabled,
            "Mode": row.mode,
            "Live approved": row.live_approved,
            "Global live_trading_enabled": row.global_live_trading_enabled,
            "Runtime live_execution_allowed": row.runtime_live_execution_allowed,
            "Effective result": row.effective_result,
        }
        for row in matrix.rows
    ]
    streamlit.dataframe(table, hide_index=True, width="stretch")
    for row in matrix.rows:
        if row.blocked_reasons:
            streamlit.caption(f"{row.strategy_id}: {'; '.join(row.blocked_reasons)}")
