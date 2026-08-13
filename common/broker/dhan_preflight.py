"""Production live-preflight orchestration over read-only Dhan probes.

The pure classifiers live in :mod:`common.broker.live_preflight`.  This module
is their production data-source adapter: it performs only read requests, keeps
HTTP/SDK response bodies out of logs, and returns the same deterministic
``LivePreflightOutcome`` used by tests.  It deliberately does not ship an
egress-IP provider; the configured ``module:attribute`` provider must be an
operator-approved local dependency or static-IP validation blocks closed.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Any, Protocol

import httpx

from common.persistence.database import Database

from .live_preflight import (
    EgressIpProvider,
    LiveConfirmationRecord,
    LivePreflightOutcome,
    check_account_identity,
    check_broker_connectivity,
    check_live_confirmation,
    check_shared_db_health,
    check_static_ip,
    check_token_validity,
    derive_account_key,
    run_live_preflight,
)

PROFILE_ENDPOINT = "https://api.dhan.co/v2/profile"
REGISTERED_IP_ENDPOINT = "https://api.dhan.co/v2/ip/getIP"
DEFAULT_TIMEOUT_SECONDS = 10.0


class DhanReadClient(Protocol):
    def get_order_list(self) -> dict[str, Any]: ...


HttpGet = Callable[..., httpx.Response]


def _body(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200:
        raise RuntimeError(f"Dhan read returned HTTP {response.status_code}")
    try:
        value = response.json()
    except ValueError as exc:
        raise RuntimeError("Dhan read returned a non-JSON response") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Dhan read returned an unexpected response shape")
    data = value.get("data")
    return data if isinstance(data, dict) else value


def _observed_client_id(body: dict[str, Any]) -> str:
    value = body.get("dhanClientId") or body.get("clientId")
    if value is None or not str(value).strip():
        raise RuntimeError("Dhan profile did not identify the authenticated account")
    return str(value)


def _registered_ips(body: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("primaryIP", "secondaryIP", "primaryIp", "secondaryIp"):
        value = body.get(key)
        if value is not None and str(value).strip():
            values.append(str(value).strip())
    return tuple(dict.fromkeys(values))


def _confirmation(
    database: Database,
    *,
    account_key: str,
    runtime_id: str,
    strategy_id: str,
    config_fingerprint: str,
) -> LiveConfirmationRecord | None:
    row = database.connect().execute(
        "SELECT account_key, runtime_id, strategy_id, config_fingerprint, "
        "confirmed_by, confirmed_at, expires_at, revoked_at FROM live_confirmations "
        "WHERE account_key = ? AND runtime_id = ? AND strategy_id = ? "
        "AND config_fingerprint = ? ORDER BY confirmed_at DESC LIMIT 1",
        (account_key, runtime_id, strategy_id, config_fingerprint),
    ).fetchone()
    if row is None:
        return None
    return LiveConfirmationRecord(
        account_key=str(row["account_key"]),
        runtime_id=str(row["runtime_id"]),
        strategy_id=str(row["strategy_id"]),
        config_fingerprint=str(row["config_fingerprint"]),
        confirmed_by=str(row["confirmed_by"]),
        confirmed_at=datetime.fromisoformat(row["confirmed_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        revoked_at=(
            datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None
        ),
    )


def _shared_database_health(database: Database) -> tuple[bool, bool]:
    integrity_ok = not database.integrity_check()
    write_probe_ok = False
    if integrity_ok:
        conn = database.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Acquiring the write transaction is the probe.  The no-op update
            # also forces SQLite to compile a write against a real migrated
            # table without changing account state.
            conn.execute(
                "UPDATE live_account_state_provenance SET established_at = established_at "
                "WHERE 0"
            )
            conn.rollback()
            write_probe_ok = True
        except Exception:
            conn.rollback()
    return integrity_ok, write_probe_ok


def run_configured_live_preflight(
    *,
    account_database: Database,
    client_id: str,
    access_token: str,
    account_identity_pepper: str,
    runtime_id: str,
    strategy_id: str,
    config_fingerprint: str,
    expected_static_ip: str | None,
    egress_provider: EgressIpProvider | None,
    order_client: DhanReadClient,
    before_read: Callable[[], None],
    now: datetime,
    http_get: HttpGet | None = None,
) -> LivePreflightOutcome:
    """Run every currently-configured production preflight check.

    Failures are classified, not raised, and response bodies are never logged.
    ``before_read`` is the account-shared READ rate-limit reservation; even the
    connectivity probe cannot bypass the cross-process limiter.
    """
    account_key = derive_account_key(client_id, account_identity_pepper)
    getter = http_get or httpx.get
    headers = {"access-token": access_token, "dhanClientId": client_id}

    observed_client_id = ""
    token_valid = False
    token_reason: str | None = None
    try:
        before_read()
        profile = _body(
            getter(PROFILE_ENDPOINT, headers=headers, timeout=DEFAULT_TIMEOUT_SECONDS)
        )
        observed_client_id = _observed_client_id(profile)
        token_valid = True
    except Exception:
        token_reason = "Dhan profile/token validation failed"

    registered_ips: tuple[str, ...] = ()
    with suppress(Exception):
        before_read()
        registered_ips = _registered_ips(
            _body(
                getter(
                    REGISTERED_IP_ENDPOINT,
                    headers=headers,
                    timeout=DEFAULT_TIMEOUT_SECONDS,
                )
            )
        )

    connected = False
    connectivity_reason: str | None = None
    try:
        before_read()
        raw = order_client.get_order_list()
        connected = raw.get("status") == "success" and isinstance(raw.get("data"), list)
        if not connected:
            connectivity_reason = "Dhan order-book connectivity probe failed"
    except Exception:
        connectivity_reason = "Dhan order-book connectivity probe could not complete"

    integrity_ok, write_probe_ok = _shared_database_health(account_database)
    confirmation = _confirmation(
        account_database,
        account_key=account_key,
        runtime_id=runtime_id,
        strategy_id=strategy_id,
        config_fingerprint=config_fingerprint,
    )
    return run_live_preflight(
        account_key=account_key,
        runtime_id=runtime_id,
        strategy_id=strategy_id,
        config_fingerprint=config_fingerprint,
        now=now,
        static_ip=check_static_ip(
            expected_ip=expected_static_ip,
            dhan_registered_ips=registered_ips,
            egress_provider=egress_provider,
        ),
        account_identity=check_account_identity(
            expected_account_key=account_key,
            observed_client_id=observed_client_id,
            pepper=account_identity_pepper,
        ),
        shared_db_health=check_shared_db_health(
            integrity_ok=integrity_ok, write_probe_ok=write_probe_ok
        ),
        token=check_token_validity(token_valid=token_valid, reason=token_reason),
        connectivity=check_broker_connectivity(
            connected=connected, reason=connectivity_reason
        ),
        confirmation=check_live_confirmation(
            confirmation,
            account_key=account_key,
            runtime_id=runtime_id,
            strategy_id=strategy_id,
            config_fingerprint=config_fingerprint,
            now=now,
        ),
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "PROFILE_ENDPOINT",
    "REGISTERED_IP_ENDPOINT",
    "DhanReadClient",
    "run_configured_live_preflight",
]
