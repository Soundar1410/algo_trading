"""The one place a Python-level lock genuinely cannot substitute for proof:
two real OS processes sharing no Python object, both hammering
``LiveOrderRateLimiter.reserve`` against the same SQLite file. Follows the
same ``multiprocessing.get_context("spawn")`` pattern as
``tests/end_to_end/test_walking_skeleton.py``'s duplicate-worker gate.

Requirement (spec section 14 / architecture report §22): shared rate
limiting must hold across multiple simulated *worker processes*, not merely
multiple objects or threads in one process.
"""

from __future__ import annotations

import multiprocessing as mp
from datetime import UTC, datetime
from pathlib import Path

from common.broker.live_rate_limiter import LiveOrderRateLimiter
from common.config.models import RateLimitCallClass, RateLimitRule
from common.persistence import migrate_account_shared_database, open_account_shared_database

NOW_ISO = datetime(2026, 8, 13, 9, 15, 0, tzinfo=UTC).isoformat()
RULE = RateLimitRule(call_class=RateLimitCallClass.NEW_ORDER, limit=20, window_seconds=60)
ATTEMPTS_PER_PROCESS = 15
WORKER_COUNT = 4


def _hammer_reserve(db_path: str, result_queue) -> None:  # type: ignore[no-untyped-def]
    """Module-level child entrypoint — must be importable for `spawn`."""
    from datetime import datetime as _datetime

    database = open_account_shared_database(Path(db_path))
    limiter = LiveOrderRateLimiter(database)
    now = _datetime.fromisoformat(NOW_ISO)

    successes = 0
    for _ in range(ATTEMPTS_PER_PROCESS):
        decision = limiter.reserve(
            account_key="acct1", call_class=RateLimitCallClass.NEW_ORDER, rule=RULE, now=now
        )
        if decision.allowed:
            successes += 1
    result_queue.put(successes)


def test_the_limit_holds_across_real_os_processes(tmp_path: Path):
    db_path = tmp_path / "dhan_account_shared.db"
    migrate_account_shared_database(open_account_shared_database(db_path))

    # WORKER_COUNT * ATTEMPTS_PER_PROCESS (60) comfortably exceeds RULE.limit
    # (20) — if the limiter were merely process-local, every process would
    # independently succeed up to 20, for a combined total near 80.
    assert RULE.limit < WORKER_COUNT * ATTEMPTS_PER_PROCESS

    context = mp.get_context("spawn")
    result_queue = context.Queue()
    processes = [
        context.Process(target=_hammer_reserve, args=(str(db_path), result_queue))
        for _ in range(WORKER_COUNT)
    ]
    for p in processes:
        p.start()
    for p in processes:
        p.join(timeout=30)
        assert not p.is_alive(), "a worker process hung"
        assert p.exitcode == 0, f"a worker process crashed (exitcode={p.exitcode})"

    total_successes = sum(result_queue.get(timeout=5) for _ in processes)

    assert total_successes == RULE.limit, (
        f"expected exactly {RULE.limit} successful reservations across all processes "
        f"combined, got {total_successes} — the limit did not hold across process "
        "boundaries"
    )

    database = open_account_shared_database(db_path)
    limiter = LiveOrderRateLimiter(database)
    stored_count = limiter.current_count(
        account_key="acct1",
        call_class=RateLimitCallClass.NEW_ORDER,
        window_seconds=RULE.window_seconds,
        now=datetime.fromisoformat(NOW_ISO),
    )
    assert stored_count == RULE.limit
