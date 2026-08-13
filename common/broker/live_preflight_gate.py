"""Preflight freshness: timestamped, TTL-bound, revalidated inside the
worker boundary — never trusted from a value merely computed once in a
parent/supervisor process and passed down.

A stale preflight result must never authorize live execution. This module
is the mechanical enforcement of that: every live worker calls
:meth:`LivePreflightGate.ensure_fresh` itself, at startup, before its first
live submission, and (via :meth:`is_fresh`) before every subsequent one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from common.persistence.database import Database

from .live_preflight import LivePreflightOutcome, PreflightCheckResult


@dataclass(frozen=True, slots=True)
class StoredPreflightResult:
    """What :meth:`LivePreflightGate.current` returns: enough to judge
    freshness and overall pass/fail, reconstructed from the persisted row.
    Per-check reason text is not reconstructed (the row's ``detail`` column
    carries the combined explanation) — freshness/authorization decisions
    only ever need the pass/fail shape and the timestamp.
    """

    account_key: str
    runtime_id: str
    strategy_id: str
    config_fingerprint: str
    checked_at: datetime
    passed: bool
    detail: str | None


class LivePreflightGate:
    """Wraps the account-shared ``live_preflight_results`` table."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def current(
        self, *, account_key: str, runtime_id: str, strategy_id: str
    ) -> StoredPreflightResult | None:
        """The most recent preflight result for this worker, regardless of
        which config fingerprint it was checked against — callers compare
        the fingerprint themselves (a change invalidates freshness, it does
        not simply vanish the row)."""
        row = self._db.connect().execute(
            "SELECT account_key, runtime_id, strategy_id, config_fingerprint, checked_at, "
            "overall_result, detail FROM live_preflight_results "
            "WHERE account_key = ? AND runtime_id = ? AND strategy_id = ? "
            "ORDER BY checked_at DESC LIMIT 1",
            (account_key, runtime_id, strategy_id),
        ).fetchone()
        if row is None:
            return None
        return StoredPreflightResult(
            account_key=row["account_key"],
            runtime_id=row["runtime_id"],
            strategy_id=row["strategy_id"],
            config_fingerprint=row["config_fingerprint"],
            checked_at=datetime.fromisoformat(row["checked_at"]),
            passed=row["overall_result"] == "passed",
            detail=row["detail"],
        )

    @staticmethod
    def is_fresh(
        result: StoredPreflightResult, *, now: datetime, ttl_seconds: float, config_fingerprint: str
    ) -> bool:
        """Fresh only when both the clock and the configuration agree: a
        config change invalidates a preflight result immediately, TTL or not."""
        if result.config_fingerprint != config_fingerprint:
            return False
        age = (now - result.checked_at).total_seconds()
        return 0 <= age <= ttl_seconds

    def record(self, outcome: LivePreflightOutcome) -> None:
        detail = "; ".join(outcome.blocked_reasons) if outcome.blocked_reasons else None
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO live_preflight_results (account_key, runtime_id, strategy_id, "
                "config_fingerprint, checked_at, static_ip_result, account_identity_result, "
                "shared_db_health_result, token_result, connectivity_result, "
                "confirmation_result, overall_result, detail) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    outcome.account_key,
                    outcome.runtime_id,
                    outcome.strategy_id,
                    outcome.config_fingerprint,
                    outcome.checked_at.isoformat(),
                    _result_word(outcome.static_ip),
                    _result_word(outcome.account_identity),
                    _result_word(outcome.shared_db_health),
                    _result_word(outcome.token),
                    _result_word(outcome.connectivity),
                    _result_word(outcome.confirmation),
                    "passed" if outcome.passed else "blocked",
                    detail,
                ),
            )

    def ensure_fresh(
        self,
        *,
        account_key: str,
        runtime_id: str,
        strategy_id: str,
        config_fingerprint: str,
        now: datetime,
        ttl_seconds: float,
        run_check: Callable[[], LivePreflightOutcome],
    ) -> StoredPreflightResult:
        """Return a fresh, authoritative preflight result — running the real
        check (inside *this* process) and persisting it whenever the stored
        one is missing, stale, or checked against a different configuration.
        A failed re-run is never fabricated as a pass: whatever ``run_check``
        returns is recorded and returned as-is.
        """
        existing = self.current(
            account_key=account_key, runtime_id=runtime_id, strategy_id=strategy_id
        )
        if existing is not None and self.is_fresh(
            existing, now=now, ttl_seconds=ttl_seconds, config_fingerprint=config_fingerprint
        ):
            return existing

        outcome = run_check()
        self.record(outcome)
        return StoredPreflightResult(
            account_key=outcome.account_key,
            runtime_id=outcome.runtime_id,
            strategy_id=outcome.strategy_id,
            config_fingerprint=outcome.config_fingerprint,
            checked_at=outcome.checked_at,
            passed=outcome.passed,
            detail="; ".join(outcome.blocked_reasons) if outcome.blocked_reasons else None,
        )


def _result_word(result: PreflightCheckResult) -> str:
    return "passed" if result.passed else "blocked"
