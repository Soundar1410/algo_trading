"""Live preflight: every check the safety boundary (spec section 10) names,
each a pure function over already-obtained data — no ``dhanhq``/network call
lives in this module, so every one of these is deterministically testable
without contacting Dhan. The SDK-calling adapters that *produce* the data
these functions consume live in :mod:`common.broker.dhan_live`.

Every check returns a :class:`PreflightCheckResult`, and
:func:`run_live_preflight` combines them into one
:class:`LivePreflightOutcome` — ``overall_result`` is ``blocked`` unless
every sub-check passed. There is no partial-credit path.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PreflightCheckResult:
    passed: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.passed


def _blocked(reason: str) -> PreflightCheckResult:
    return PreflightCheckResult(False, reason)


_PASSED = PreflightCheckResult(True)

#: How many bytes of the HMAC digest become the account key. Long enough
#: that two distinct accounts colliding is not a practical concern; short
#: enough to stay a convenient join key across every account-shared table.
_ACCOUNT_KEY_DIGEST_BYTES = 16


# ------------------------------------------------------------ account identity
def derive_account_key(client_id: str, pepper: str) -> str:
    """The stable, non-secret partition key for one Dhan account.

    A *keyed* hash (HMAC), not a bare ``sha256(client_id)``: a Dhan client ID
    is a low-entropy numeric string, so an unsalted hash of it would be
    brute-forceable/rainbow-tableable back to the original — defeating "the
    raw client ID is never persisted". HMAC with a secret, per-installation
    pepper (``Settings.account_identity_pepper``, ``.env``-only) makes that
    infeasible. Every runtime group on the same machine reads the same
    ``.env``, so every group authenticated to the same real account
    deterministically derives the *same* key — an operator cannot
    accidentally split one account's limits across two keys by typing a
    different label in two config files, because no config field is
    involved in this derivation at all.
    """
    if not client_id:
        raise ValueError("client_id must not be empty")
    if not pepper:
        raise ValueError("pepper must not be empty")
    digest = hmac.new(pepper.encode("utf-8"), client_id.encode("utf-8"), hashlib.sha256).digest()
    return digest[:_ACCOUNT_KEY_DIGEST_BYTES].hex()


def check_account_identity(
    *, expected_account_key: str, observed_client_id: str, pepper: str
) -> PreflightCheckResult:
    """Confirm the *authenticated* account matches what this worker expects.

    Catches a credential/config mismatch (an operator's runtime accidentally
    pointed at a different real account than intended) at the moment it
    would matter, rather than silently aggregating two accounts' exposure
    under one key.
    """
    try:
        observed_key = derive_account_key(observed_client_id, pepper)
    except ValueError as exc:
        return _blocked(f"cannot derive account identity: {exc}")
    if observed_key != expected_account_key:
        return _blocked(
            "authenticated account does not match this worker's expected account_key "
            "(credential/config mismatch — refusing rather than silently trading "
            "under the wrong account's risk limits)"
        )
    return _PASSED


# ----------------------------------------------------------------- static IP
class EgressIpProvider(Protocol):
    """Independently observes this process's current public egress IP.

    No implementation ships with this task (architecture report §6/§13): the
    shipped default is ``egress_provider=None``, which fails
    :func:`check_static_ip` closed rather than trusting the Dhan whitelist
    alone. A real provider (an external IP-echo service, or a local
    operator-infra source) is a separate, later, explicitly-approved choice.
    """

    def current_public_ip(self) -> str | None: ...


class FakeEgressIpProvider:
    """Deterministic, network-free test double."""

    def __init__(self, ip: str | None) -> None:
        self._ip = ip

    def current_public_ip(self) -> str | None:
        return self._ip


def load_egress_ip_provider(reference: str | None) -> EgressIpProvider | None:
    """Load an operator-approved provider from ``"module:attribute"``.

    No provider implementation is shipped: choosing an external IP-echo
    service or local infrastructure source remains a separate operational
    approval.  This loader closes the runtime wiring seam without making that
    choice.  A missing, malformed, raising, or incompatible reference returns
    ``None`` so :func:`check_static_ip` blocks; it never substitutes a network
    service or trusts the configured IP by itself.
    """
    if not reference:
        return None
    module_name, separator, attribute_name = reference.partition(":")
    if not separator or not module_name or not attribute_name:
        return None
    try:
        candidate = getattr(import_module(module_name), attribute_name)
        provider = candidate() if callable(candidate) else candidate
        method = getattr(provider, "current_public_ip", None)
    except Exception:
        return None
    return provider if callable(method) else None


def check_static_ip(
    *,
    expected_ip: str | None,
    dhan_registered_ips: Sequence[str],
    egress_provider: EgressIpProvider | None,
) -> PreflightCheckResult:
    """All three of configured/registered/observed must agree.

    Checking only "is ``expected_ip`` in Dhan's registered list" proves Dhan
    believes some IP is whitelisted — it does not prove *this process* is
    currently egressing from it. All three facts are checked independently.
    """
    if not expected_ip:
        return _blocked("no expected_static_ip configured")
    if egress_provider is None:
        return _blocked(
            "no egress IP provider configured — static IP cannot be independently "
            "verified; blocked rather than trusting the Dhan whitelist alone"
        )
    observed = egress_provider.current_public_ip()
    if observed is None:
        return _blocked("egress IP provider could not determine the current public IP")
    if observed != expected_ip:
        return _blocked(
            f"observed egress IP {observed!r} does not match configured "
            f"expected_static_ip {expected_ip!r}"
        )
    if expected_ip not in dhan_registered_ips:
        return _blocked(
            f"expected_static_ip {expected_ip!r} is not present in Dhan's registered "
            "IP whitelist"
        )
    if observed not in dhan_registered_ips:
        return _blocked(
            f"observed egress IP {observed!r} is not present in Dhan's registered "
            "IP whitelist"
        )
    return _PASSED


# ------------------------------------------------------------------ token/conn
def check_token_validity(*, token_valid: bool, reason: str | None = None) -> PreflightCheckResult:
    """Packages an already-computed token-validity fact (the auth bootstrap's
    job, ``common.authentication``) into the shared preflight result shape."""
    if token_valid:
        return _PASSED
    return _blocked(reason or "Dhan access token is not valid or not present")


def check_broker_connectivity(
    *, connected: bool, reason: str | None = None
) -> PreflightCheckResult:
    if connected:
        return _PASSED
    return _blocked(reason or "broker connectivity check failed")


# --------------------------------------------------------------- confirmation
@dataclass(frozen=True, slots=True)
class LiveConfirmationRecord:
    account_key: str
    runtime_id: str
    strategy_id: str
    config_fingerprint: str
    confirmed_by: str
    confirmed_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


def check_live_confirmation(
    record: LiveConfirmationRecord | None,
    *,
    account_key: str,
    runtime_id: str,
    strategy_id: str,
    config_fingerprint: str,
    now: datetime,
) -> PreflightCheckResult:
    """All five of account/runtime/strategy/config-fingerprint/expiry must
    match and be current — an old confirmation for a since-changed
    configuration simply does not match, and is refused, not reused."""
    if record is None:
        return _blocked(
            "no live confirmation on file for this account/runtime/strategy/config "
            "fingerprint — an operator must confirm live trading explicitly"
        )
    if record.revoked_at is not None:
        return _blocked("live confirmation was revoked")
    if (
        record.account_key != account_key
        or record.runtime_id != runtime_id
        or record.strategy_id != strategy_id
        or record.config_fingerprint != config_fingerprint
    ):
        return _blocked(
            "live confirmation does not match this exact account/runtime/strategy/"
            "config fingerprint — a confirmation never carries over a configuration change"
        )
    if now >= record.expires_at:
        return _blocked(f"live confirmation expired at {record.expires_at.isoformat()}")
    return _PASSED


# --------------------------------------------------------------- shared DB
def check_shared_db_health(*, integrity_ok: bool, write_probe_ok: bool) -> PreflightCheckResult:
    """The account-shared database must be reachable, structurally sound and
    writable before its exposure/rate-limit numbers can be trusted.

    Takes already-computed booleans rather than a live ``Database`` so the
    orchestrator (:func:`run_live_preflight`) controls exactly what "health"
    means for a given caller (e.g. whether to run a real write probe) and
    this function stays a pure, trivially-testable classifier.
    """
    if not integrity_ok:
        return _blocked("account-shared database failed its integrity check")
    if not write_probe_ok:
        return _blocked("account-shared database is not currently writable")
    return _PASSED


# --------------------------------------------------------------- orchestrator
@dataclass(frozen=True, slots=True)
class LivePreflightOutcome:
    account_key: str
    runtime_id: str
    strategy_id: str
    config_fingerprint: str
    checked_at: datetime
    static_ip: PreflightCheckResult
    account_identity: PreflightCheckResult
    shared_db_health: PreflightCheckResult
    token: PreflightCheckResult
    connectivity: PreflightCheckResult
    confirmation: PreflightCheckResult

    @property
    def passed(self) -> bool:
        return all(
            r.passed
            for r in (
                self.static_ip,
                self.account_identity,
                self.shared_db_health,
                self.token,
                self.connectivity,
                self.confirmation,
            )
        )

    @property
    def blocked_reasons(self) -> tuple[str, ...]:
        return tuple(
            f"{name}: {result.reason}"
            for name, result in (
                ("static_ip", self.static_ip),
                ("account_identity", self.account_identity),
                ("shared_db_health", self.shared_db_health),
                ("token", self.token),
                ("connectivity", self.connectivity),
                ("confirmation", self.confirmation),
            )
            if not result.passed
        )


def run_live_preflight(
    *,
    account_key: str,
    runtime_id: str,
    strategy_id: str,
    config_fingerprint: str,
    now: datetime,
    static_ip: PreflightCheckResult,
    account_identity: PreflightCheckResult,
    shared_db_health: PreflightCheckResult,
    token: PreflightCheckResult,
    connectivity: PreflightCheckResult,
    confirmation: PreflightCheckResult,
) -> LivePreflightOutcome:
    """Combine every sub-check into one timestamped, all-or-nothing outcome.

    Deliberately takes already-computed :class:`PreflightCheckResult`
    values rather than running the checks itself: each sub-check has a
    different data source (some pure, some SDK-backed), and forcing them
    through one function signature here would either hide that or force
    this function to depend on the SDK. The caller (a worker, or a test)
    assembles the inputs; this function only combines them and stamps the
    result — see ``common.broker.live_preflight_gate.LivePreflightGate``
    for the freshness/TTL/persistence layer built on top of this.
    """
    return LivePreflightOutcome(
        account_key=account_key,
        runtime_id=runtime_id,
        strategy_id=strategy_id,
        config_fingerprint=config_fingerprint,
        checked_at=now,
        static_ip=static_ip,
        account_identity=account_identity,
        shared_db_health=shared_db_health,
        token=token,
        connectivity=connectivity,
        confirmation=confirmation,
    )
