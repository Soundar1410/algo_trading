"""LiveOrderRateLimiter: fixed-window check-and-reserve, call-class
independence — single-process logic. Real cross-process contention is
tests/integration/test_live_rate_limiter_cross_process.py."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from common.broker.live_rate_limiter import (
    LiveOrderRateLimiter,
    rule_for,
    seconds_until_next_window,
)
from common.config.models import RateLimitCallClass, RateLimitRule
from common.persistence import migrate_account_shared_database, open_account_shared_database

NOW = datetime(2026, 8, 13, 9, 15, 0, tzinfo=UTC)
NEW_ORDER_RULE = RateLimitRule(call_class=RateLimitCallClass.NEW_ORDER, limit=3, window_seconds=60)
READ_RULE = RateLimitRule(call_class=RateLimitCallClass.READ, limit=100, window_seconds=1)


def _limiter(tmp_path: Path) -> LiveOrderRateLimiter:
    database = open_account_shared_database(tmp_path / "dhan_account_shared.db")
    migrate_account_shared_database(database)
    return LiveOrderRateLimiter(database)


def test_reservations_succeed_up_to_the_limit(tmp_path: Path):
    limiter = _limiter(tmp_path)
    for _ in range(3):
        decision = limiter.reserve(
            account_key="acct1",
            call_class=RateLimitCallClass.NEW_ORDER,
            rule=NEW_ORDER_RULE,
            now=NOW,
        )
        assert decision.allowed


def test_the_reservation_past_the_limit_is_refused(tmp_path: Path):
    limiter = _limiter(tmp_path)
    for _ in range(3):
        limiter.reserve(
            account_key="acct1",
            call_class=RateLimitCallClass.NEW_ORDER,
            rule=NEW_ORDER_RULE,
            now=NOW,
        )
    decision = limiter.reserve(
        account_key="acct1", call_class=RateLimitCallClass.NEW_ORDER, rule=NEW_ORDER_RULE, now=NOW
    )
    assert not decision.allowed
    assert "rate limit reached" in decision.reason


def test_call_classes_have_independent_limits(tmp_path: Path):
    limiter = _limiter(tmp_path)
    for _ in range(3):
        limiter.reserve(
            account_key="acct1",
            call_class=RateLimitCallClass.NEW_ORDER,
            rule=NEW_ORDER_RULE,
            now=NOW,
        )
    # new_order is exhausted; read has its own independent window/limit.
    decision = limiter.reserve(
        account_key="acct1", call_class=RateLimitCallClass.READ, rule=READ_RULE, now=NOW
    )
    assert decision.allowed


def test_accounts_have_independent_limits(tmp_path: Path):
    limiter = _limiter(tmp_path)
    for _ in range(3):
        limiter.reserve(
            account_key="acct1",
            call_class=RateLimitCallClass.NEW_ORDER,
            rule=NEW_ORDER_RULE,
            now=NOW,
        )
    decision = limiter.reserve(
        account_key="acct2", call_class=RateLimitCallClass.NEW_ORDER, rule=NEW_ORDER_RULE, now=NOW
    )
    assert decision.allowed


def test_a_new_window_resets_capacity(tmp_path: Path):
    limiter = _limiter(tmp_path)
    for _ in range(3):
        limiter.reserve(
            account_key="acct1",
            call_class=RateLimitCallClass.NEW_ORDER,
            rule=NEW_ORDER_RULE,
            now=NOW,
        )
    from datetime import timedelta

    later = NOW + timedelta(seconds=61)
    decision = limiter.reserve(
        account_key="acct1", call_class=RateLimitCallClass.NEW_ORDER, rule=NEW_ORDER_RULE, now=later
    )
    assert decision.allowed


def test_current_count_reflects_reservations_without_consuming_one(tmp_path: Path):
    limiter = _limiter(tmp_path)
    limiter.reserve(
        account_key="acct1", call_class=RateLimitCallClass.NEW_ORDER, rule=NEW_ORDER_RULE, now=NOW
    )
    count = limiter.current_count(
        account_key="acct1",
        call_class=RateLimitCallClass.NEW_ORDER,
        window_seconds=60,
        now=NOW,
    )
    assert count == 1
    # Reading again does not itself reserve.
    count_again = limiter.current_count(
        account_key="acct1",
        call_class=RateLimitCallClass.NEW_ORDER,
        window_seconds=60,
        now=NOW,
    )
    assert count_again == 1


def test_rule_for_finds_the_configured_rule():
    rules = (NEW_ORDER_RULE, READ_RULE)
    assert rule_for(rules, RateLimitCallClass.NEW_ORDER) == NEW_ORDER_RULE
    assert rule_for(rules, RateLimitCallClass.READ) == READ_RULE


def test_rule_for_returns_none_for_an_unconfigured_call_class():
    """No rule = zero permits is the *caller's* responsibility to enforce;
    this just proves the lookup honestly reports "nothing configured"."""
    rules = (NEW_ORDER_RULE,)
    assert rule_for(rules, RateLimitCallClass.CANCEL) is None


def test_seconds_until_next_window_is_positive_and_bounded():
    remaining = seconds_until_next_window(NOW, 60)
    assert 0 < remaining <= 60
