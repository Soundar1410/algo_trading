"""orchestration.auto_start.day_claim: exactly one notification per trading date.

The property under test is the one a "check the file, then write it" marker
cannot give you: two processes arriving together must produce one message, not
two — and a *failed* send must leave the day unclaimed rather than recording a
delivery that never happened.
"""

from __future__ import annotations

import contextlib
import threading
from datetime import date
from pathlib import Path

from orchestration.auto_start.day_claim import (
    KIND_AUTH_SUCCESS,
    KIND_GIVE_UP,
    DailyNotificationClaim,
)

DAY = date(2026, 8, 20)
NEXT_DAY = date(2026, 8, 21)


def _claim(tmp_path: Path) -> DailyNotificationClaim:
    return DailyNotificationClaim(tmp_path / "notifications.json")


def test_the_first_send_goes_through_and_is_recorded(tmp_path: Path):
    claim = _claim(tmp_path)
    sends = []

    def deliver() -> bool:
        sends.append(1)
        return True

    result = claim.send_once(day=DAY, kind=KIND_AUTH_SUCCESS, deliver=deliver)
    assert result.delivered and result.sent_by_us
    assert claim.already_delivered(day=DAY, kind=KIND_AUTH_SUCCESS)
    assert len(sends) == 1


def test_a_second_trigger_the_same_day_does_not_send_again(tmp_path: Path):
    """RunAtLoad and the 09:00 calendar trigger both firing."""
    claim = _claim(tmp_path)
    calls = []

    def deliver() -> bool:
        calls.append(1)
        return True

    first = claim.send_once(day=DAY, kind=KIND_AUTH_SUCCESS, deliver=deliver)
    second = claim.send_once(day=DAY, kind=KIND_AUTH_SUCCESS, deliver=deliver)

    assert first.sent_by_us and not second.sent_by_us
    assert second.delivered  # already delivered counts as delivered
    assert len(calls) == 1


def test_concurrent_processes_produce_exactly_one_message(tmp_path: Path):
    """Two independent claim objects over one file, racing on real threads."""
    path = tmp_path / "notifications.json"
    calls: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def deliver() -> bool:
        with lock:
            calls.append(1)
        return True

    def worker() -> None:
        barrier.wait()
        DailyNotificationClaim(path).send_once(
            day=DAY, kind=KIND_AUTH_SUCCESS, deliver=deliver
        )

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(calls) == 1


def test_a_failed_delivery_is_not_recorded_as_delivered(tmp_path: Path):
    """The difference between 'we told the operator' and 'we tried and lost it'."""
    claim = _claim(tmp_path)
    result = claim.send_once(day=DAY, kind=KIND_AUTH_SUCCESS, deliver=lambda: False)
    assert not result.delivered
    assert result.sent_by_us  # we attempted it
    assert not claim.already_delivered(day=DAY, kind=KIND_AUTH_SUCCESS)


def test_a_later_trigger_may_retry_after_a_failed_delivery(tmp_path: Path):
    claim = _claim(tmp_path)
    claim.send_once(day=DAY, kind=KIND_AUTH_SUCCESS, deliver=lambda: False)
    second = claim.send_once(day=DAY, kind=KIND_AUTH_SUCCESS, deliver=lambda: True)
    assert second.delivered and second.sent_by_us
    assert claim.already_delivered(day=DAY, kind=KIND_AUTH_SUCCESS)


def test_a_new_trading_date_is_claimed_independently(tmp_path: Path):
    claim = _claim(tmp_path)
    claim.send_once(day=DAY, kind=KIND_AUTH_SUCCESS, deliver=lambda: True)
    assert not claim.already_delivered(day=NEXT_DAY, kind=KIND_AUTH_SUCCESS)
    result = claim.send_once(day=NEXT_DAY, kind=KIND_AUTH_SUCCESS, deliver=lambda: True)
    assert result.sent_by_us


def test_the_two_kinds_do_not_share_a_claim(tmp_path: Path):
    claim = _claim(tmp_path)
    claim.send_once(day=DAY, kind=KIND_AUTH_SUCCESS, deliver=lambda: True)
    assert not claim.already_delivered(day=DAY, kind=KIND_GIVE_UP)
    result = claim.send_once(day=DAY, kind=KIND_GIVE_UP, deliver=lambda: True)
    assert result.sent_by_us


def test_a_corrupt_record_is_treated_as_nothing_delivered(tmp_path: Path):
    """One duplicate message beats silence on the day something went wrong."""
    path = tmp_path / "notifications.json"
    path.write_text("{not json at all", encoding="utf-8")
    result = DailyNotificationClaim(path).send_once(
        day=DAY, kind=KIND_AUTH_SUCCESS, deliver=lambda: True
    )
    assert result.delivered and result.sent_by_us


def test_a_raising_delivery_does_not_leave_the_lock_held(tmp_path: Path):
    claim = _claim(tmp_path)

    def boom() -> bool:
        raise RuntimeError("telegram exploded")

    with contextlib.suppress(RuntimeError):
        claim.send_once(day=DAY, kind=KIND_AUTH_SUCCESS, deliver=boom)

    # The lock must be free for the next attempt.
    result = claim.send_once(day=DAY, kind=KIND_AUTH_SUCCESS, deliver=lambda: True)
    assert result.sent_by_us
