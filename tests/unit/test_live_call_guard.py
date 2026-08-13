from __future__ import annotations

from datetime import UTC, datetime

import pytest

from common.broker.live_call_guard import GuardedLiveCall
from common.broker.live_rate_limiter import RateLimitDecision
from common.config.models import RateLimitCallClass, RateLimitRule


class _BlockingPreflight:
    def __init__(self) -> None:
        self.calls = 0

    def ensure_fresh(self, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise AssertionError("entry preflight is intentionally unavailable")


class _AllowingLimiter:
    def __init__(self) -> None:
        self.calls: list[RateLimitCallClass] = []

    def reserve(self, *, call_class, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(call_class)
        return RateLimitDecision(True)


def _guard(preflight, limiter) -> GuardedLiveCall:  # type: ignore[no-untyped-def]
    now = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)
    return GuardedLiveCall(
        preflight_gate=preflight,
        rate_limiter=limiter,
        account_key="acct",
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp",
        preflight_ttl_seconds=300,
        rate_rules=(
            RateLimitRule(
                call_class=RateLimitCallClass.NEW_ORDER,
                limit=5,
                window_seconds=1,
            ),
        ),
        run_preflight=lambda: None,  # type: ignore[arg-type]
        entry_ready=lambda: False,
        now=lambda: now,
    )


def test_non_reducing_order_still_requires_entry_preflight() -> None:
    preflight = _BlockingPreflight()
    limiter = _AllowingLimiter()
    with pytest.raises(AssertionError, match="preflight"):
        _guard(preflight, limiter).before_call(RateLimitCallClass.NEW_ORDER)
    assert limiter.calls == []


def test_risk_reducing_exit_bypasses_entry_only_trust_but_keeps_rate_limit() -> None:
    preflight = _BlockingPreflight()
    limiter = _AllowingLimiter()

    _guard(preflight, limiter).before_call(RateLimitCallClass.NEW_ORDER, risk_reducing=True)

    assert preflight.calls == 0
    assert limiter.calls == [RateLimitCallClass.NEW_ORDER]
