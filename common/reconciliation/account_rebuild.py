"""Account-wide state rebuild (architecture report §4.4).

A missing, recreated, empty or corrupted account-shared database must
never be interpreted as zero exposure and therefore safe to trade. Every
live worker's preflight checks ``live_account_state_provenance`` before
trusting the shared ledger's totals for anything — if
``reconciliation_status != 'reconciled'`` (including the very first time an
``account_key`` is ever seen, which defaults to ``never_reconciled``, not
an implicit pass), new live entries for the *whole account* stay blocked
until :func:`rebuild_account_shared_state` succeeds.

Guarded by its own ``filelock`` (the established cross-process coordination
primitive in this repo — see ``common.persistence.migrations``,
``common.process.locks``) so concurrently-starting workers across every
runtime group don't all attempt the rebuild at once: one performs it,
others block until it completes or fails. A failed rebuild leaves
provenance ``failed``, still blocking everyone — never silently proceeding.

"Reconciled" here means "the account-wide picture is now known to be
trustworthy," not "no mismatches exist." Critical mismatches found during
the rebuild are persisted through the normal per-group
:class:`~common.reconciliation.runner.ReconciliationRunner` machinery and
block new entries through the normal per-strategy gates — this module's
own job is narrower: proving the *shared* state was actually reconstructed
from the broker, not assumed empty.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock, Timeout

from common.broker.base import Broker
from common.logging import get_logger
from common.persistence.database import Database

from .compare import LocalOrderState, LocalPositionState
from .runner import ReconciliationRunner

log = get_logger(__name__)

DEFAULT_REBUILD_LOCK_TIMEOUT_SECONDS = 30.0


class ProvenanceStatus:
    NEVER_RECONCILED = "never_reconciled"
    RECONCILED = "reconciled"
    STALE = "stale"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RuntimeGroupSnapshot:
    """One runtime group's broker connection and local state — everything
    the account-wide rebuild needs to reconcile that group."""

    runtime_id: str
    broker: Broker
    local_orders: Sequence[LocalOrderState]
    local_positions: Sequence[LocalPositionState]
    reconciliation_runner: ReconciliationRunner


@dataclass(frozen=True, slots=True)
class AccountRebuildResult:
    status: str  # 'reconciled' or 'failed'
    critical_mismatch_total: int
    error_message: str | None = None


def get_provenance(database: Database, *, account_key: str) -> str:
    """The account-shared database's own honesty check: a fresh/empty file
    has no provenance row at all, which must read as ``never_reconciled``
    (blocking), never as an implicit "nothing to reconcile"."""
    row = database.connect().execute(
        "SELECT reconciliation_status FROM live_account_state_provenance WHERE account_key = ?",
        (account_key,),
    ).fetchone()
    if row is None:
        return ProvenanceStatus.NEVER_RECONCILED
    return str(row["reconciliation_status"])


def rebuild_account_shared_state(
    account_database: Database,
    *,
    account_key: str,
    runtime_groups: Sequence[RuntimeGroupSnapshot],
    lock_path: Path,
    lock_timeout_seconds: float = DEFAULT_REBUILD_LOCK_TIMEOUT_SECONDS,
) -> AccountRebuildResult:
    """Reconcile every configured runtime group against its own broker,
    then mark the account-shared state's provenance ``reconciled`` — only
    after this may preflight checks and reservation transactions trust it.

    ``runtime_groups`` is a fixed, config-derived list supplied by the
    caller — never discovered dynamically mid-incident, so a group that is
    unreachable/misconfigured at exactly the wrong moment cannot cause this
    function to silently skip reconciling it.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_path), timeout=lock_timeout_seconds)
    try:
        with lock:
            return _rebuild_locked(
                account_database, account_key=account_key, runtime_groups=runtime_groups
            )
    except Timeout:
        return AccountRebuildResult(
            status="failed",
            critical_mismatch_total=0,
            error_message=(
                f"timed out after {lock_timeout_seconds}s waiting for the account rebuild "
                f"lock at {lock_path} — another process is probably rebuilding it"
            ),
        )


def _rebuild_locked(
    account_database: Database,
    *,
    account_key: str,
    runtime_groups: Sequence[RuntimeGroupSnapshot],
) -> AccountRebuildResult:
    total_critical = 0
    for group in runtime_groups:
        result = group.reconciliation_runner.run(
            runtime_id=group.runtime_id,
            strategy_id=None,
            broker=group.broker,
            local_orders=group.local_orders,
            local_positions=group.local_positions,
            trigger="startup",
        )
        if result.status != "completed":
            log.error(
                "account rebuild for account_key=%s failed at runtime group %s: %s",
                account_key,
                group.runtime_id,
                result.error_message,
            )
            _mark_provenance(
                account_database, account_key=account_key, status=ProvenanceStatus.FAILED
            )
            return AccountRebuildResult(
                status="failed",
                critical_mismatch_total=total_critical,
                error_message=(
                    f"reconciliation failed for runtime group {group.runtime_id!r}: "
                    f"{result.error_message}"
                ),
            )
        total_critical += result.critical_mismatch_count

    _mark_provenance(account_database, account_key=account_key, status=ProvenanceStatus.RECONCILED)
    log.info(
        "account rebuild for account_key=%s completed across %d runtime group(s), "
        "%d critical mismatch(es) found",
        account_key,
        len(runtime_groups),
        total_critical,
    )
    return AccountRebuildResult(status="reconciled", critical_mismatch_total=total_critical)


def _mark_provenance(database: Database, *, account_key: str, status: str) -> None:
    now = datetime.now(UTC).isoformat()
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO live_account_state_provenance (account_key, reconciliation_status, "
            "last_reconciled_at, established_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (account_key) DO UPDATE SET "
            "reconciliation_status = excluded.reconciliation_status, "
            "last_reconciled_at = excluded.last_reconciled_at",
            (account_key, status, now, now),
        )
