"""Account-wide, cross-process live-order rate limiter (spec section 14).

Fixed-window, backed by the account-shared database's
``live_order_rate_windows`` table. Atomicity across OS processes comes from
SQLite's own ``BEGIN IMMEDIATE`` transaction — the read-then-conditionally-
write "is there still capacity, and if so reserve it" sequence happens
inside one immediate transaction, so a second process's own immediate
transaction cannot even begin reading until the first commits. See
:meth:`~common.persistence.database.Database.transaction`'s docstring for
why plain (deferred) ``BEGIN`` is not sufficient for this.

No ``filelock`` wrapper is needed around the reservation itself — that
primitive is reserved for cross-process identity/lifecycle coordination
elsewhere in this codebase (migrations, process locks); the actual
increment here is a data-consistency concern, and SQLite's own transaction
serialization is the correct tool for that, consistent with the existing
division of labour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from common.config.models import RateLimitCallClass, RateLimitRule
from common.persistence.database import Database


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.allowed


def _floor_to_window(now: datetime, window_seconds: int) -> datetime:
    epoch_seconds = now.timestamp()
    floored = (epoch_seconds // window_seconds) * window_seconds
    return datetime.fromtimestamp(floored, tz=now.tzinfo)


class LiveOrderRateLimiter:
    """Wraps ``live_order_rate_windows`` in the account-shared database."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def reserve(
        self,
        *,
        account_key: str,
        call_class: RateLimitCallClass,
        rule: RateLimitRule,
        now: datetime,
    ) -> RateLimitDecision:
        """Atomically check-and-increment one call class's current window.

        An unconfigured call class has no ``rule`` to pass in at all — the
        caller (``run_live_preflight``/the broker layer) is responsible for
        refusing before ever calling this, per
        ``LiveOrderRateLimitConfig``'s own "no rule = zero permits"
        contract; this method only enforces a *given* rule's limit.
        """
        window_start = _floor_to_window(now, rule.window_seconds)
        with self._db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT count FROM live_order_rate_windows WHERE account_key = ? "
                "AND call_class = ? AND window_start = ? AND window_seconds = ?",
                (account_key, call_class.value, window_start.isoformat(), rule.window_seconds),
            ).fetchone()
            count = row["count"] if row is not None else 0
            if count >= rule.limit:
                return RateLimitDecision(
                    False,
                    f"{call_class.value} rate limit reached: {count}/{rule.limit} in the "
                    f"current {rule.window_seconds}s window",
                )
            if row is not None:
                conn.execute(
                    "UPDATE live_order_rate_windows SET count = count + 1 WHERE account_key = ? "
                    "AND call_class = ? AND window_start = ? AND window_seconds = ?",
                    (account_key, call_class.value, window_start.isoformat(), rule.window_seconds),
                )
            else:
                conn.execute(
                    "INSERT INTO live_order_rate_windows (account_key, call_class, "
                    "window_start, window_seconds, count) VALUES (?, ?, ?, ?, 1)",
                    (account_key, call_class.value, window_start.isoformat(), rule.window_seconds),
                )
            return RateLimitDecision(True)

    def current_count(
        self,
        *,
        account_key: str,
        call_class: RateLimitCallClass,
        window_seconds: int,
        now: datetime,
    ) -> int:
        """Read-only: current count in the window containing ``now``. For
        dashboards/observability — never used to decide admission (that is
        :meth:`reserve`'s job, which reads and writes atomically together)."""
        window_start = _floor_to_window(now, window_seconds)
        row = self._db.connect().execute(
            "SELECT count FROM live_order_rate_windows WHERE account_key = ? "
            "AND call_class = ? AND window_start = ? AND window_seconds = ?",
            (account_key, call_class.value, window_start.isoformat(), window_seconds),
        ).fetchone()
        return int(row["count"]) if row is not None else 0


def rule_for(
    config_rules: tuple[RateLimitRule, ...], call_class: RateLimitCallClass
) -> RateLimitRule | None:
    """The configured rule for one call class, or ``None`` — "no rule" means
    the caller must refuse (zero permits), never fall back to unlimited."""
    for rule in config_rules:
        if rule.call_class is call_class:
            return rule
    return None


def seconds_until_next_window(now: datetime, window_seconds: int) -> float:
    """For a caller that wants to schedule a retry just after the current
    window rolls over, rather than busy-polling."""
    window_start = _floor_to_window(now, window_seconds)
    next_start = window_start + timedelta(seconds=window_seconds)
    return (next_start - now).total_seconds()


__all__ = [
    "LiveOrderRateLimiter",
    "RateLimitDecision",
    "rule_for",
    "seconds_until_next_window",
]
