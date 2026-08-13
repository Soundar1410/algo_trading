"""Live preflight sub-checks: pure functions, no network, deterministic.

Every check here is exercised in isolation and then combined via
``run_live_preflight`` — the orchestrator is "blocked unless every check
passed", never partial credit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from common.broker.live_preflight import (
    FakeEgressIpProvider,
    LiveConfirmationRecord,
    check_account_identity,
    check_broker_connectivity,
    check_live_confirmation,
    check_shared_db_health,
    check_static_ip,
    check_token_validity,
    derive_account_key,
    run_live_preflight,
)

NOW = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)


# --------------------------------------------------------------- account key
def test_derive_account_key_is_deterministic():
    assert derive_account_key("1234567", "pepper1") == derive_account_key("1234567", "pepper1")


def test_derive_account_key_differs_by_pepper():
    assert derive_account_key("1234567", "pepper1") != derive_account_key("1234567", "pepper2")


def test_derive_account_key_differs_by_client_id():
    assert derive_account_key("1111111", "pepper1") != derive_account_key("2222222", "pepper1")


def test_derive_account_key_never_contains_the_raw_client_id():
    key = derive_account_key("1234567", "pepper1")
    assert "1234567" not in key


def test_derive_account_key_rejects_empty_inputs():
    import pytest

    with pytest.raises(ValueError):
        derive_account_key("", "pepper1")
    with pytest.raises(ValueError):
        derive_account_key("1234567", "")


def test_check_account_identity_passes_when_the_authenticated_account_matches():
    expected = derive_account_key("1234567", "pepper1")
    result = check_account_identity(
        expected_account_key=expected, observed_client_id="1234567", pepper="pepper1"
    )
    assert result.passed


def test_check_account_identity_blocks_on_a_credential_mismatch():
    expected = derive_account_key("1234567", "pepper1")
    result = check_account_identity(
        expected_account_key=expected, observed_client_id="9999999", pepper="pepper1"
    )
    assert not result.passed
    assert "does not match" in result.reason


# ------------------------------------------------------------------ static IP
def test_static_ip_passes_when_expected_registered_and_observed_all_agree():
    result = check_static_ip(
        expected_ip="203.0.113.10",
        dhan_registered_ips=["203.0.113.10"],
        egress_provider=FakeEgressIpProvider("203.0.113.10"),
    )
    assert result.passed


def test_static_ip_blocks_when_no_expected_ip_configured():
    result = check_static_ip(
        expected_ip=None, dhan_registered_ips=[], egress_provider=FakeEgressIpProvider("1.2.3.4")
    )
    assert not result.passed
    assert "no expected_static_ip" in result.reason


def test_static_ip_blocks_when_no_provider_configured():
    """The safe shipped default: no provider means BLOCKED, never a pass
    from the whitelist check alone."""
    result = check_static_ip(
        expected_ip="203.0.113.10", dhan_registered_ips=["203.0.113.10"], egress_provider=None
    )
    assert not result.passed
    assert "no egress IP provider" in result.reason


def test_static_ip_blocks_when_provider_cannot_determine_the_ip():
    result = check_static_ip(
        expected_ip="203.0.113.10",
        dhan_registered_ips=["203.0.113.10"],
        egress_provider=FakeEgressIpProvider(None),
    )
    assert not result.passed
    assert "could not determine" in result.reason


def test_static_ip_blocks_on_observed_expected_mismatch():
    result = check_static_ip(
        expected_ip="203.0.113.10",
        dhan_registered_ips=["203.0.113.10", "203.0.113.99"],
        egress_provider=FakeEgressIpProvider("203.0.113.99"),
    )
    assert not result.passed
    assert "does not match" in result.reason


def test_static_ip_blocks_when_expected_ip_is_not_dhan_registered():
    result = check_static_ip(
        expected_ip="203.0.113.10",
        dhan_registered_ips=["198.51.100.1"],
        egress_provider=FakeEgressIpProvider("203.0.113.10"),
    )
    assert not result.passed
    assert "not present in Dhan" in result.reason


# --------------------------------------------------------------- token/conn
def test_check_token_validity_passes_and_blocks():
    assert check_token_validity(token_valid=True).passed
    blocked = check_token_validity(token_valid=False, reason="expired")
    assert not blocked.passed
    assert blocked.reason == "expired"


def test_check_broker_connectivity_passes_and_blocks():
    assert check_broker_connectivity(connected=True).passed
    blocked = check_broker_connectivity(connected=False, reason="timeout")
    assert not blocked.passed
    assert blocked.reason == "timeout"


# --------------------------------------------------------------- confirmation
def _confirmation(**overrides: object) -> LiveConfirmationRecord:
    base = dict(
        account_key="acct1",
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp1",
        confirmed_by="operator",
        confirmed_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=6),
        revoked_at=None,
    )
    base.update(overrides)
    return LiveConfirmationRecord(**base)  # type: ignore[arg-type]


def test_live_confirmation_passes_when_everything_matches_and_is_current():
    result = check_live_confirmation(
        _confirmation(),
        account_key="acct1",
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp1",
        now=NOW,
    )
    assert result.passed


def test_live_confirmation_blocks_when_none_exists():
    result = check_live_confirmation(
        None,
        account_key="acct1",
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp1",
        now=NOW,
    )
    assert not result.passed
    assert "no live confirmation" in result.reason


def test_live_confirmation_blocks_when_revoked():
    result = check_live_confirmation(
        _confirmation(revoked_at=NOW - timedelta(hours=1)),
        account_key="acct1",
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp1",
        now=NOW,
    )
    assert not result.passed
    assert "revoked" in result.reason


def test_live_confirmation_blocks_when_expired():
    result = check_live_confirmation(
        _confirmation(expires_at=NOW - timedelta(seconds=1)),
        account_key="acct1",
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp1",
        now=NOW,
    )
    assert not result.passed
    assert "expired" in result.reason


def test_live_confirmation_blocks_on_a_changed_config_fingerprint():
    """An old confirmation can never authorize a since-changed configuration."""
    result = check_live_confirmation(
        _confirmation(config_fingerprint="fp1_old"),
        account_key="acct1",
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp2_new",
        now=NOW,
    )
    assert not result.passed
    assert "does not match" in result.reason


def test_live_confirmation_blocks_on_a_different_strategy_or_runtime_or_account():
    matching = _confirmation()
    base_call = {
        "account_key": "acct1",
        "runtime_id": "intraday_options",
        "strategy_id": "st01",
        "config_fingerprint": "fp1",
        "now": NOW,
    }
    for override in (
        {"strategy_id": "st99"},
        {"runtime_id": "positional_options"},
        {"account_key": "acct2"},
    ):
        call = {**base_call, **override}
        result = check_live_confirmation(matching, **call)  # type: ignore[arg-type]
        assert not result.passed


# --------------------------------------------------------------- shared DB
def test_shared_db_health_passes_when_both_checks_pass():
    assert check_shared_db_health(integrity_ok=True, write_probe_ok=True).passed


def test_shared_db_health_blocks_on_integrity_failure():
    result = check_shared_db_health(integrity_ok=False, write_probe_ok=True)
    assert not result.passed
    assert "integrity" in result.reason


def test_shared_db_health_blocks_on_write_probe_failure():
    result = check_shared_db_health(integrity_ok=True, write_probe_ok=False)
    assert not result.passed
    assert "writable" in result.reason


# --------------------------------------------------------------- orchestrator
def _passing_outcome():
    passing = check_token_validity(token_valid=True)
    return run_live_preflight(
        account_key="acct1",
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp1",
        now=NOW,
        static_ip=passing,
        account_identity=passing,
        shared_db_health=passing,
        token=passing,
        connectivity=passing,
        confirmation=passing,
    )


def test_run_live_preflight_passes_when_every_check_passes():
    outcome = _passing_outcome()
    assert outcome.passed
    assert outcome.blocked_reasons == ()


def test_run_live_preflight_blocks_when_any_single_check_fails():
    """Every sub-check alone is enough to block — same discipline as the
    existing config-level live gate (test_broker_factory.py)."""
    failing = check_token_validity(token_valid=False, reason="boom")
    for field in (
        "static_ip",
        "account_identity",
        "shared_db_health",
        "token",
        "connectivity",
        "confirmation",
    ):
        passing = check_token_validity(token_valid=True)
        kwargs = {
            "static_ip": passing,
            "account_identity": passing,
            "shared_db_health": passing,
            "token": passing,
            "connectivity": passing,
            "confirmation": passing,
        }
        kwargs[field] = failing
        outcome = run_live_preflight(
            account_key="acct1",
            runtime_id="intraday_options",
            strategy_id="st01",
            config_fingerprint="fp1",
            now=NOW,
            **kwargs,  # type: ignore[arg-type]
        )
        assert not outcome.passed, f"{field} alone should have blocked"
        assert any(field in reason for reason in outcome.blocked_reasons)
