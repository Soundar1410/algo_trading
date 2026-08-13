"""Reconciliation runner (spec 2189-2212's full sequence), persisted to one
runtime group's own ``reconciliation_runs``/``reconciliation_mismatches``
tables (migration 0007) — reconciliation compares *this* group's local
state against the broker, so it lives where that local state already does.

``run()`` never raises on a broker failure — a failed fetch is itself a
reconciliation failure, persisted as such, with ``entries_blocked=True``.
The one thing this runner refuses to do silently is guess: a broker call
that fails leaves the run ``failed``, not ``completed`` with an empty
mismatch list (which would look identical to "reconciled clean" to a
careless caller).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from common.broker.base import Broker
from common.logging import get_logger, redact_for_persistence
from common.persistence.database import Database

from .compare import (
    LocalOrderState,
    LocalPositionState,
    Mismatch,
    compare_orders,
    compare_positions,
)
from .recovery import RecoveryResolution, load_local_reconciliation_state, recover_broker_evidence
from .snapshot import BrokerSnapshot, fetch_broker_snapshot

if TYPE_CHECKING:
    from common.execution.repository import ExecutionRepository

log = get_logger(__name__)

#: Default price-difference tolerance (index points) before a position's
#: average-price gap is even worth surfacing as PRICE_MISMATCH. Callers
#: needing a different tolerance pass their own — this is not silently
#: authoritative, per spec's "the tolerance must be explicit".
DEFAULT_PRICE_TOLERANCE = 0.5


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    run_id: int
    status: str  # 'completed' or 'failed'
    critical_mismatch_count: int
    entries_blocked: bool
    mismatches: tuple[Mismatch, ...]
    error_message: str | None = None
    broker_snapshot: BrokerSnapshot | None = None


class ReconciliationRunner:
    def __init__(
        self,
        database: Database,
        *,
        price_tolerance: float = DEFAULT_PRICE_TOLERANCE,
        recovery_repository: ExecutionRepository | None = None,
    ) -> None:
        self._db = database
        self._price_tolerance = price_tolerance
        self._recovery_repository = recovery_repository

    def run(
        self,
        *,
        runtime_id: str,
        strategy_id: str | None,
        broker: Broker,
        local_orders: Sequence[LocalOrderState],
        local_positions: Sequence[LocalPositionState],
        trigger: str = "manual",
    ) -> ReconciliationResult:
        run_id = self._start_run(
            runtime_id=runtime_id, strategy_id=strategy_id, trigger=trigger, now=datetime.now(UTC)
        )

        try:
            broker_snapshot = fetch_broker_snapshot(broker)
        except Exception as exc:
            log.error("reconciliation run %d failed to fetch broker snapshot: %s", run_id, exc)
            self._fail_run(run_id, error_message=str(exc), now=datetime.now(UTC))
            return ReconciliationResult(
                run_id=run_id,
                status="failed",
                critical_mismatch_count=0,
                entries_blocked=True,
                mismatches=(),
                error_message=str(exc),
                broker_snapshot=None,
            )

        recovery_mismatches: list[Mismatch] = []
        resolutions: tuple[RecoveryResolution, ...] = ()
        if self._recovery_repository is not None:
            if strategy_id is None:
                self._fail_run(
                    run_id,
                    error_message="broker-evidence recovery requires a strategy_id",
                    now=datetime.now(UTC),
                )
                return ReconciliationResult(
                    run_id=run_id,
                    status="failed",
                    critical_mismatch_count=0,
                    entries_blocked=True,
                    mismatches=(),
                    error_message="broker-evidence recovery requires a strategy_id",
                    broker_snapshot=broker_snapshot,
                )
            try:
                recovery = recover_broker_evidence(
                    self._recovery_repository,
                    runtime_id=runtime_id,
                    strategy_id=strategy_id,
                    snapshot=broker_snapshot,
                )
            except Exception as exc:
                log.exception("reconciliation run %d failed to apply broker evidence", run_id)
                self._fail_run(run_id, error_message=str(exc), now=datetime.now(UTC))
                return ReconciliationResult(
                    run_id=run_id,
                    status="failed",
                    critical_mismatch_count=0,
                    entries_blocked=True,
                    mismatches=(),
                    error_message=str(exc),
                    broker_snapshot=broker_snapshot,
                )
            recovery_mismatches.extend(recovery.mismatches)
            resolutions = recovery.resolutions
            local_orders, local_positions = load_local_reconciliation_state(
                self._recovery_repository, strategy_id=strategy_id
            )

        order_mismatches = compare_orders(local_orders, broker_snapshot.orders)
        position_mismatches = compare_positions(
            local_positions, broker_snapshot.positions, price_tolerance=self._price_tolerance
        )
        mismatches = [*recovery_mismatches, *order_mismatches, *position_mismatches]
        critical_count = sum(1 for m in mismatches if m.is_critical)
        entries_blocked = critical_count > 0

        now = datetime.now(UTC)
        self._persist_mismatches(
            run_id=run_id,
            runtime_id=runtime_id,
            strategy_id=strategy_id or "",
            mismatches=mismatches,
            now=now,
        )
        self._persist_resolutions(
            run_id=run_id,
            runtime_id=runtime_id,
            strategy_id=strategy_id or "",
            resolutions=resolutions,
            now=now,
        )
        self._complete_run(
            run_id,
            critical_count=critical_count,
            entries_blocked=entries_blocked,
            broker_orders_fetched=len(broker_snapshot.orders),
            broker_trades_fetched=len(broker_snapshot.trades),
            broker_positions_fetched=len(broker_snapshot.positions),
            now=now,
        )
        return ReconciliationResult(
            run_id=run_id,
            status="completed",
            critical_mismatch_count=critical_count,
            entries_blocked=entries_blocked,
            mismatches=tuple(mismatches),
            broker_snapshot=broker_snapshot,
        )

    # ------------------------------------------------------------- persistence
    def _start_run(
        self, *, runtime_id: str, strategy_id: str | None, trigger: str, now: datetime
    ) -> int:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO reconciliation_runs (runtime_id, strategy_id, execution_mode, "
                "trigger_source, started_at, status) VALUES (?, ?, 'live', ?, ?, 'running')",
                (runtime_id, strategy_id, trigger, now.isoformat()),
            )
            return int(cursor.lastrowid)  # type: ignore[arg-type]

    def _fail_run(self, run_id: int, *, error_message: str, now: datetime) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE reconciliation_runs SET status = 'failed', completed_at = ?, "
                "entries_blocked = 1, error_message = ? WHERE id = ?",
                (now.isoformat(), error_message, run_id),
            )

    def _complete_run(
        self,
        run_id: int,
        *,
        critical_count: int,
        entries_blocked: bool,
        broker_orders_fetched: int,
        broker_trades_fetched: int,
        broker_positions_fetched: int,
        now: datetime,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE reconciliation_runs SET status = 'completed', completed_at = ?, "
                "critical_mismatch_count = ?, entries_blocked = ?, broker_orders_fetched = ?, "
                "broker_trades_fetched = ?, broker_positions_fetched = ? WHERE id = ?",
                (
                    now.isoformat(),
                    critical_count,
                    1 if entries_blocked else 0,
                    broker_orders_fetched,
                    broker_trades_fetched,
                    broker_positions_fetched,
                    run_id,
                ),
            )

    def _persist_mismatches(
        self,
        *,
        run_id: int,
        runtime_id: str,
        strategy_id: str,
        mismatches: Sequence[Mismatch],
        now: datetime,
    ) -> None:
        if not mismatches:
            return
        with self._db.transaction() as conn:
            for mismatch in mismatches:
                conn.execute(
                    "INSERT INTO reconciliation_mismatches (run_id, runtime_id, strategy_id, "
                    "execution_mode, category, is_critical, correlation_id, broker_order_id, "
                    "security_id, local_quantity, broker_quantity, local_side, broker_side, "
                    "local_price, broker_price, detail, detected_at) VALUES "
                    "(?, ?, ?, 'live', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        runtime_id,
                        strategy_id,
                        mismatch.category,
                        1 if mismatch.is_critical else 0,
                        mismatch.correlation_id,
                        mismatch.broker_order_id,
                        mismatch.security_id,
                        mismatch.local_quantity,
                        mismatch.broker_quantity,
                        mismatch.local_side,
                        mismatch.broker_side,
                        mismatch.local_price,
                        mismatch.broker_price,
                        # Free text, never routed through a logging handler —
                        # see redact_for_persistence's own docstring for why
                        # this call site redacts explicitly rather than
                        # relying on SecretRedactingFilter alone.
                        redact_for_persistence(mismatch.detail),
                        now.isoformat(),
                    ),
                )

    def _persist_resolutions(
        self,
        *,
        run_id: int,
        runtime_id: str,
        strategy_id: str,
        resolutions: Sequence[RecoveryResolution],
        now: datetime,
    ) -> None:
        if not resolutions:
            return
        with self._db.transaction() as conn:
            for resolution in resolutions:
                conn.execute(
                    "INSERT INTO reconciliation_mismatches "
                    "(run_id, runtime_id, strategy_id, execution_mode, category, is_critical, "
                    "correlation_id, broker_order_id, detail, resolution_action, "
                    "resolution_reason, resolved_at, detected_at) "
                    "VALUES (?, ?, ?, 'live', ?, 0, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        runtime_id,
                        strategy_id,
                        resolution.category,
                        resolution.correlation_id,
                        resolution.broker_order_id,
                        redact_for_persistence(resolution.reason),
                        resolution.action,
                        redact_for_persistence(resolution.reason),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

    # ---------------------------------------------------------------- queries
    def latest_run(self, *, runtime_id: str, strategy_id: str | None) -> sqlite3.Row | None:
        conn = self._db.connect()
        row: sqlite3.Row | None
        if strategy_id is None:
            row = conn.execute(
                "SELECT * FROM reconciliation_runs WHERE runtime_id = ? AND strategy_id IS NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (runtime_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM reconciliation_runs WHERE runtime_id = ? AND strategy_id = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (runtime_id, strategy_id),
            ).fetchone()
        return row

    def open_critical_mismatches(self, *, runtime_id: str, strategy_id: str) -> list[sqlite3.Row]:
        return (
            self._db.connect()
            .execute(
                "SELECT * FROM reconciliation_mismatches WHERE runtime_id = ? AND strategy_id = ? "
                "AND is_critical = 1 AND resolved_at IS NULL ORDER BY detected_at DESC",
                (runtime_id, strategy_id),
            )
            .fetchall()
        )
