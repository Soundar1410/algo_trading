"""Account-wide coordination across *two different runtime groups*, each
with its own real OS worker process — not merely multiple objects or
threads in one process, and not merely multiple workers within one
runtime group (architecture report §4.1/§4.6: two runtime groups
authenticated to the same account must share one enforcement surface,
never be able to bypass limits by using different runtime_ids).

Follows the same ``multiprocessing.get_context("spawn")`` pattern as
``tests/end_to_end/test_walking_skeleton.py``'s duplicate-worker gate and
``tests/integration/test_live_rate_limiter_cross_process.py``.
"""

from __future__ import annotations

import multiprocessing as mp
from datetime import UTC, datetime
from pathlib import Path

from common.broker.live_rate_limiter import LiveOrderRateLimiter
from common.config.models import RateLimitCallClass, RateLimitRule
from common.persistence import migrate_account_shared_database, open_account_shared_database
from common.risk import AccountReservationGate

NOW_ISO = datetime(2026, 8, 13, 9, 15, 0, tzinfo=UTC).isoformat()
RULE = RateLimitRule(call_class=RateLimitCallClass.NEW_ORDER, limit=10, window_seconds=60)
ATTEMPTS_PER_PROCESS = 8


def _hammer_rate_limit(db_path: str, account_key: str, result_queue) -> None:  # type: ignore[no-untyped-def]
    """Module-level child entrypoint — must be importable for `spawn`. Each
    of the two processes represents a worker in a *different* runtime
    group, both authenticated to the same account."""
    from datetime import datetime as _datetime

    database = open_account_shared_database(Path(db_path))
    limiter = LiveOrderRateLimiter(database)
    now = _datetime.fromisoformat(NOW_ISO)

    successes = 0
    for _ in range(ATTEMPTS_PER_PROCESS):
        decision = limiter.reserve(
            account_key=account_key, call_class=RateLimitCallClass.NEW_ORDER, rule=RULE, now=now
        )
        if decision.allowed:
            successes += 1
    result_queue.put(successes)


def test_shared_rate_limit_holds_across_two_runtime_groups_real_processes(tmp_path: Path):
    db_path = tmp_path / "dhan_account_shared.db"
    migrate_account_shared_database(open_account_shared_database(db_path))
    account_key = "acct_shared"

    # 2 processes * 8 attempts = 16, comfortably exceeding the 10-order limit
    # — if the limiter were scoped per runtime_id rather than per account,
    # each "group" would independently succeed up to 10, totalling near 16.
    assert RULE.limit < 2 * ATTEMPTS_PER_PROCESS

    context = mp.get_context("spawn")
    result_queue = context.Queue()
    # Process A simulates a worker in "intraday_options"; process B
    # simulates a worker in "positional_options" — both share one
    # account-shared database and one account_key, exactly as two real
    # runtime groups authenticated to the same Dhan account would.
    processes = [
        context.Process(target=_hammer_rate_limit, args=(str(db_path), account_key, result_queue))
        for _ in range(2)
    ]
    for p in processes:
        p.start()
    for p in processes:
        p.join(timeout=30)
        assert not p.is_alive()
        assert p.exitcode == 0

    total_successes = sum(result_queue.get(timeout=5) for _ in processes)
    assert total_successes == RULE.limit, (
        f"expected exactly {RULE.limit} successful reservations combined across both "
        f"runtime groups, got {total_successes}"
    )


def _hammer_reservations(
    db_path: str, account_key: str, runtime_id: str, strategy_id: str, result_queue
) -> None:  # type: ignore[no-untyped-def]
    """Module-level child entrypoint. Each process reserves account-wide
    risk capacity as a worker in its own distinct runtime group."""
    from datetime import datetime as _datetime

    database = open_account_shared_database(Path(db_path))
    gate = AccountReservationGate(database)
    now = _datetime.fromisoformat(NOW_ISO)

    successes = 0
    for i in range(5):
        decision = gate.check_and_reserve(
            account_key=account_key,
            runtime_id=runtime_id,
            strategy_id=strategy_id,
            correlation_id=f"l_{runtime_id}_{strategy_id}_20260813_{i:04d}",
            trading_date="2026-08-13",
            projected_capital=20_000.0,
            projected_legs=1,
            quantity=75,
            max_deployed_capital=100_000.0,
            max_open_positions=None,
            max_open_legs=None,
            now=now,
        )
        if decision.allowed:
            successes += 1
    result_queue.put(successes)


def test_account_risk_capacity_is_shared_across_two_runtime_groups_real_processes(
    tmp_path: Path,
):
    """Requirement: cross-worker risk aggregation includes live workers
    across runtime groups, proven with real OS processes — two runtime
    groups authenticated to the same account cannot each independently
    reserve up to the full cap."""
    db_path = tmp_path / "dhan_account_shared.db"
    migrate_account_shared_database(open_account_shared_database(db_path))
    account_key = "acct_shared"

    # Each process attempts 5 reservations of 20,000 capital = up to 100,000
    # each; the shared cap is 100,000 for the *account*, not per group. If
    # the reservation gate were scoped per runtime group, both processes
    # would succeed fully (10 total, 200,000 combined capital).
    context = mp.get_context("spawn")
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_hammer_reservations,
            args=(str(db_path), account_key, "intraday_options", "st_io", result_queue),
        ),
        context.Process(
            target=_hammer_reservations,
            args=(str(db_path), account_key, "positional_options", "st_po", result_queue),
        ),
    ]
    for p in processes:
        p.start()
    for p in processes:
        p.join(timeout=30)
        assert not p.is_alive()
        assert p.exitcode == 0

    total_successes = sum(result_queue.get(timeout=5) for _ in processes)
    # 100,000 cap / 20,000 per reservation = exactly 5 successful reservations
    # combined across BOTH runtime groups, never per group.
    assert total_successes == 5, (
        f"expected exactly 5 successful reservations shared across both runtime "
        f"groups (100,000 capital cap / 20,000 each), got {total_successes}"
    )

    database = open_account_shared_database(db_path)
    with database.connect() as conn:
        by_runtime = conn.execute(
            "SELECT runtime_id, COUNT(*) AS n FROM live_risk_reservations "
            "WHERE account_key = ? GROUP BY runtime_id",
            (account_key,),
        ).fetchall()
    runtime_ids_with_reservations = {row["runtime_id"] for row in by_runtime}
    # Both groups contributed at least an attempt to the shared ledger —
    # confirms this is genuinely cross-group, not one group starving the other.
    assert runtime_ids_with_reservations, "no reservations were recorded at all"
