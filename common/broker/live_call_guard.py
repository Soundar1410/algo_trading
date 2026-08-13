"""Per-call live safety boundary: fresh preflight plus shared rate capacity."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from common.config.models import RateLimitCallClass, RateLimitRule

from .base import BrokerError
from .live_preflight import LivePreflightOutcome
from .live_preflight_gate import LivePreflightGate
from .live_rate_limiter import LiveOrderRateLimiter, rule_for


class GuardedLiveCall:
    """Authorise exactly one Dhan call, or raise before the SDK is invoked."""

    def __init__(
        self,
        *,
        preflight_gate: LivePreflightGate,
        rate_limiter: LiveOrderRateLimiter,
        account_key: str,
        runtime_id: str,
        strategy_id: str,
        config_fingerprint: str,
        preflight_ttl_seconds: float,
        rate_rules: tuple[RateLimitRule, ...],
        run_preflight: Callable[[], LivePreflightOutcome],
        entry_ready: Callable[[], bool] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._preflight_gate = preflight_gate
        self._rate_limiter = rate_limiter
        self._account_key = account_key
        self._runtime_id = runtime_id
        self._strategy_id = strategy_id
        self._config_fingerprint = config_fingerprint
        self._preflight_ttl_seconds = preflight_ttl_seconds
        self._rate_rules = rate_rules
        self._run_preflight = run_preflight
        self._entry_ready = entry_ready or (lambda: True)
        self._now = now or (lambda: datetime.now(UTC))

    def ensure_preflight(self, *, force: bool = False) -> None:
        now = self._now()
        result = self._preflight_gate.ensure_fresh(
            account_key=self._account_key,
            runtime_id=self._runtime_id,
            strategy_id=self._strategy_id,
            config_fingerprint=self._config_fingerprint,
            now=now,
            ttl_seconds=self._preflight_ttl_seconds,
            run_check=self._run_preflight,
            force=force,
        )
        if not result.passed:
            raise BrokerError(
                f"live preflight is blocked for {self._strategy_id!r}: "
                f"{result.detail or 'one or more checks failed'}"
            )

    def before_call(self, call_class: RateLimitCallClass, *, risk_reducing: bool = False) -> None:
        # A live exit must remain possible when an entry-only trust condition
        # degrades (confirmation expiry, shared provenance or update-stream
        # readiness). Dhan's call still crosses the account-wide rate limiter;
        # any broker rejection/ambiguity remains persisted and reconciled.
        if not risk_reducing:
            self.ensure_preflight()
        if (
            call_class is RateLimitCallClass.NEW_ORDER
            and not risk_reducing
            and not self._entry_ready()
        ):
            raise BrokerError(
                "account reconciliation/provenance is not trusted; new live orders are blocked"
            )

        now = self._now()
        rule = rule_for(self._rate_rules, call_class)
        if rule is None:
            raise BrokerError(
                f"no live rate-limit rule configured for {call_class.value}; zero permits"
            )
        decision = self._rate_limiter.reserve(
            account_key=self._account_key,
            call_class=call_class,
            rule=rule,
            now=now,
        )
        if not decision.allowed:
            raise BrokerError(decision.reason or f"{call_class.value} rate limit reached")


__all__ = ["GuardedLiveCall"]
