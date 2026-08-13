"""Issue or revoke an exact, short-lived controlled-live confirmation.

This command cannot enable live trading by itself.  It only manages one of
the independent fail-closed preflight gates and writes append-only operator
evidence to the account-shared database.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from common.logging import redact_for_persistence
from common.persistence import migrate_account_shared_database, open_account_shared_database

ISSUE_PHRASE = "ISSUE LIVE CONFIRMATION"
REVOKE_PHRASE = "REVOKE LIVE CONFIRMATION"
MAX_TTL_MINUTES = 30


def issue_confirmation(
    database_path: Path,
    *,
    account_key: str,
    runtime_id: str,
    strategy_id: str,
    config_fingerprint: str,
    actor: str,
    reason: str,
    ttl_minutes: int,
    now: datetime,
) -> int:
    if not 1 <= ttl_minutes <= MAX_TTL_MINUTES:
        raise ValueError(f"ttl_minutes must be between 1 and {MAX_TTL_MINUTES}")
    _require_nonempty(account_key, runtime_id, strategy_id, config_fingerprint, actor, reason)
    safe_actor = redact_for_persistence(actor)
    safe_reason = redact_for_persistence(reason)
    database = open_account_shared_database(database_path)
    try:
        migrate_account_shared_database(database)
        expires_at = now + timedelta(minutes=ttl_minutes)
        with database.transaction(immediate=True) as conn:
            cursor = conn.execute(
                "INSERT INTO live_confirmations "
                "(account_key, runtime_id, strategy_id, config_fingerprint, confirmed_by, "
                "confirmed_at, expires_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT (account_key, runtime_id, strategy_id, config_fingerprint) "
                "DO UPDATE SET confirmed_by = excluded.confirmed_by, "
                "confirmed_at = excluded.confirmed_at, expires_at = excluded.expires_at, "
                "revoked_at = NULL",
                (
                    account_key,
                    runtime_id,
                    strategy_id,
                    config_fingerprint,
                    safe_actor,
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            row = conn.execute(
                "SELECT id FROM live_confirmations WHERE account_key = ? AND runtime_id = ? "
                "AND strategy_id = ? AND config_fingerprint = ?",
                (account_key, runtime_id, strategy_id, config_fingerprint),
            ).fetchone()
            confirmation_id = int(row["id"])
            conn.execute(
                "INSERT INTO live_confirmation_events "
                "(confirmation_id, action, account_key, runtime_id, strategy_id, "
                "config_fingerprint, actor, reason, occurred_at) "
                "VALUES (?, 'ISSUED', ?, ?, ?, ?, ?, ?, ?)",
                (
                    confirmation_id,
                    account_key,
                    runtime_id,
                    strategy_id,
                    config_fingerprint,
                    safe_actor,
                    safe_reason,
                    now.isoformat(),
                ),
            )
            del cursor
        return confirmation_id
    finally:
        database.close()


def revoke_confirmation(
    database_path: Path,
    *,
    account_key: str,
    runtime_id: str,
    strategy_id: str,
    config_fingerprint: str,
    actor: str,
    reason: str,
    now: datetime,
) -> int:
    _require_nonempty(account_key, runtime_id, strategy_id, config_fingerprint, actor, reason)
    safe_actor = redact_for_persistence(actor)
    safe_reason = redact_for_persistence(reason)
    database = open_account_shared_database(database_path)
    try:
        migrate_account_shared_database(database)
        with database.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT id, revoked_at FROM live_confirmations WHERE account_key = ? "
                "AND runtime_id = ? AND strategy_id = ? AND config_fingerprint = ?",
                (account_key, runtime_id, strategy_id, config_fingerprint),
            ).fetchone()
            if row is None:
                raise ValueError("no matching live confirmation exists")
            confirmation_id = int(row["id"])
            if row["revoked_at"] is not None:
                raise ValueError("matching live confirmation is already revoked")
            conn.execute(
                "UPDATE live_confirmations SET revoked_at = ? WHERE id = ?",
                (now.isoformat(), confirmation_id),
            )
            conn.execute(
                "INSERT INTO live_confirmation_events "
                "(confirmation_id, action, account_key, runtime_id, strategy_id, "
                "config_fingerprint, actor, reason, occurred_at) "
                "VALUES (?, 'REVOKED', ?, ?, ?, ?, ?, ?, ?)",
                (
                    confirmation_id,
                    account_key,
                    runtime_id,
                    strategy_id,
                    config_fingerprint,
                    safe_actor,
                    safe_reason,
                    now.isoformat(),
                ),
            )
        return confirmation_id
    finally:
        database.close()


def _require_nonempty(*values: str) -> None:
    if any(not value.strip() for value in values):
        raise ValueError("account, runtime, strategy, fingerprint, actor and reason are required")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("issue", "revoke"))
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--account-key", required=True)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--strategy-id", required=True)
    parser.add_argument("--config-fingerprint", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--ttl-minutes", type=int, default=15)
    parser.add_argument("--confirm", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    expected = ISSUE_PHRASE if args.action == "issue" else REVOKE_PHRASE
    if args.confirm != expected:
        raise SystemExit(f"refused: --confirm must exactly equal {expected!r}")
    kwargs = dict(
        account_key=args.account_key,
        runtime_id=args.runtime_id,
        strategy_id=args.strategy_id,
        config_fingerprint=args.config_fingerprint,
        actor=args.actor,
        reason=args.reason,
        now=datetime.now(UTC),
    )
    if args.action == "issue":
        confirmation_id = issue_confirmation(args.database, ttl_minutes=args.ttl_minutes, **kwargs)
    else:
        confirmation_id = revoke_confirmation(args.database, **kwargs)
    print(f"{args.action} recorded for live confirmation id={confirmation_id}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
