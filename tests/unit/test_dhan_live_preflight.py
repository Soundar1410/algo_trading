"""Production preflight orchestration, with every external input faked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from common.broker.dhan_preflight import (
    PROFILE_ENDPOINT,
    REGISTERED_IP_ENDPOINT,
    run_configured_live_preflight,
)
from common.broker.live_preflight import FakeEgressIpProvider, derive_account_key
from common.persistence import migrate_account_shared_database, open_account_shared_database

NOW = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)
CLIENT_ID = "client-for-test"
PEPPER = "pepper-for-test"


class _ReadClient:
    def __init__(self, response=None) -> None:  # type: ignore[no-untyped-def]
        self.response = response or {"status": "success", "data": []}
        self.calls = 0

    def get_order_list(self):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self.response


def _database(tmp_path: Path):  # type: ignore[no-untyped-def]
    database = open_account_shared_database(tmp_path / "account.db")
    migrate_account_shared_database(database)
    account_key = derive_account_key(CLIENT_ID, PEPPER)
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO live_confirmations "
            "(account_key, runtime_id, strategy_id, config_fingerprint, confirmed_by, "
            "confirmed_at, expires_at) VALUES (?, 'intraday_options', 'st01', 'fp1', "
            "'test-operator', ?, ?)",
            (account_key, NOW.isoformat(), (NOW + timedelta(hours=1)).isoformat()),
        )
    return database


def _http_get(url: str, **kwargs):  # type: ignore[no-untyped-def]
    del kwargs
    if url == PROFILE_ENDPOINT:
        return httpx.Response(200, json={"dhanClientId": CLIENT_ID})
    if url == REGISTERED_IP_ENDPOINT:
        return httpx.Response(
            200,
            json={"data": {"primaryIP": "203.0.113.10", "secondaryIP": ""}},
        )
    raise AssertionError(f"unexpected URL {url}")


def test_every_real_preflight_input_can_pass_without_a_network_call(tmp_path: Path):
    database = _database(tmp_path)
    client = _ReadClient()
    read_reservations: list[int] = []

    outcome = run_configured_live_preflight(
        account_database=database,
        client_id=CLIENT_ID,
        access_token="test-token",
        account_identity_pepper=PEPPER,
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp1",
        expected_static_ip="203.0.113.10",
        egress_provider=FakeEgressIpProvider("203.0.113.10"),
        order_client=client,
        before_read=lambda: read_reservations.append(1),
        now=NOW,
        http_get=_http_get,
    )

    assert outcome.passed
    assert client.calls == 1
    assert read_reservations == [1, 1, 1]


def test_missing_approved_egress_provider_blocks_the_production_outcome(tmp_path: Path):
    outcome = run_configured_live_preflight(
        account_database=_database(tmp_path),
        client_id=CLIENT_ID,
        access_token="test-token",
        account_identity_pepper=PEPPER,
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp1",
        expected_static_ip="203.0.113.10",
        egress_provider=None,
        order_client=_ReadClient(),
        before_read=lambda: None,
        now=NOW,
        http_get=_http_get,
    )

    assert not outcome.passed
    assert any("egress IP provider" in reason for reason in outcome.blocked_reasons)


def test_failed_broker_read_is_blocked_not_interpreted_as_connectivity(tmp_path: Path):
    outcome = run_configured_live_preflight(
        account_database=_database(tmp_path),
        client_id=CLIENT_ID,
        access_token="test-token",
        account_identity_pepper=PEPPER,
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp1",
        expected_static_ip="203.0.113.10",
        egress_provider=FakeEgressIpProvider("203.0.113.10"),
        order_client=_ReadClient({"status": "failure", "data": ""}),
        before_read=lambda: None,
        now=NOW,
        http_get=_http_get,
    )

    assert not outcome.connectivity.passed
    assert not outcome.passed
