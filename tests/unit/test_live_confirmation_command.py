from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from common.persistence import migrate_account_shared_database, open_account_shared_database
from scripts.live_confirmation import issue_confirmation, main, revoke_confirmation

NOW = datetime(2026, 8, 13, 4, 0, tzinfo=UTC)


def _kwargs() -> dict[str, str]:
    return {
        "account_key": "account-hmac",
        "runtime_id": "intraday_options",
        "strategy_id": "ema_cross_9_21_buy",
        "config_fingerprint": "fingerprint-1",
        "actor": "operator@example",
        "reason": "approved controlled-live rehearsal",
    }


def test_issue_and_revoke_are_audited_and_never_delete_evidence(tmp_path: Path) -> None:
    path = tmp_path / "account.db"
    confirmation_id = issue_confirmation(path, ttl_minutes=15, now=NOW, **_kwargs())
    assert revoke_confirmation(path, now=NOW, **_kwargs()) == confirmation_id

    database = open_account_shared_database(path)
    migrate_account_shared_database(database)
    confirmation = (
        database.connect()
        .execute("SELECT * FROM live_confirmations WHERE id = ?", (confirmation_id,))
        .fetchone()
    )
    assert confirmation["revoked_at"] == NOW.isoformat()
    events = (
        database.connect()
        .execute("SELECT action, actor, reason FROM live_confirmation_events ORDER BY id")
        .fetchall()
    )
    assert [row["action"] for row in events] == ["ISSUED", "REVOKED"]
    assert all(row["actor"] == _kwargs()["actor"] for row in events)
    assert all(row["reason"] == _kwargs()["reason"] for row in events)


@pytest.mark.parametrize("ttl", [0, 31])
def test_confirmation_ttl_is_bounded(tmp_path: Path, ttl: int) -> None:
    with pytest.raises(ValueError, match="ttl_minutes"):
        issue_confirmation(tmp_path / "account.db", ttl_minutes=ttl, now=NOW, **_kwargs())


def test_cli_refuses_without_the_exact_action_phrase(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="--confirm must exactly equal"):
        main(
            [
                "issue",
                "--database",
                str(tmp_path / "account.db"),
                "--account-key",
                "account-hmac",
                "--runtime-id",
                "intraday_options",
                "--strategy-id",
                "ema_cross_9_21_buy",
                "--config-fingerprint",
                "fp",
                "--actor",
                "operator",
                "--reason",
                "test",
                "--confirm",
                "yes",
            ]
        )
