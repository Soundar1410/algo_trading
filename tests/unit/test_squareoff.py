"""Square-off: cutoff before square-off, and state that survives a restart."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from common.risk import SquareOffPolicy, SquareOffState, SquareOffTrigger

IST = ZoneInfo("Asia/Kolkata")


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 7, 29, hour, minute, tzinfo=IST)


def test_entries_are_allowed_before_the_cutoff():
    assert SquareOffPolicy().entries_allowed(_at(14, 59))


def test_entries_stop_at_the_cutoff():
    assert not SquareOffPolicy().entries_allowed(_at(15, 0))


def test_entries_never_reopen_later_in_the_day():
    policy = SquareOffPolicy()
    assert not policy.entries_allowed(_at(15, 1))
    assert not policy.entries_allowed(_at(15, 29))


def test_the_cutoff_precedes_square_off_so_entries_are_not_instantly_closed():
    policy = SquareOffPolicy()
    assert policy.entry_cutoff < policy.square_off_at


def test_a_policy_with_square_off_before_the_cutoff_is_rejected():
    with pytest.raises(ValueError, match="must not precede"):
        SquareOffPolicy(entry_cutoff=time(15, 20), square_off_at=time(15, 0))


# ------------------------------------------------------------- triggers
def test_nothing_triggers_during_normal_trading():
    trigger = SquareOffPolicy().trigger_at(_at(10, 0), state=SquareOffState.PENDING)
    assert trigger is SquareOffTrigger.NONE


def test_the_cutoff_blocks_new_entries():
    trigger = SquareOffPolicy().trigger_at(_at(15, 5), state=SquareOffState.PENDING)
    assert trigger is SquareOffTrigger.BLOCK_NEW_ENTRIES


def test_square_off_time_triggers_square_off():
    trigger = SquareOffPolicy().trigger_at(_at(15, 15), state=SquareOffState.PENDING)
    assert trigger is SquareOffTrigger.SQUARE_OFF


def test_square_off_still_triggers_after_its_time_has_passed():
    """A worker that restarts at 15:20 must still square off, not skip it."""
    trigger = SquareOffPolicy().trigger_at(_at(15, 20), state=SquareOffState.PENDING)
    assert trigger is SquareOffTrigger.SQUARE_OFF


# --------------------------------------------------- restart-safe state
def test_a_completed_square_off_does_not_trigger_again_after_a_restart():
    """The spec: a restart must not reset square-off state."""
    trigger = SquareOffPolicy().trigger_at(_at(15, 20), state=SquareOffState.COMPLETED)
    assert trigger is SquareOffTrigger.NONE


def test_an_in_progress_square_off_is_not_restarted_concurrently():
    trigger = SquareOffPolicy().trigger_at(_at(15, 20), state=SquareOffState.IN_PROGRESS)
    assert trigger is SquareOffTrigger.NONE


def test_a_failed_square_off_is_retried():
    """FAILED must be retried; silently giving up would leave a position open."""
    trigger = SquareOffPolicy().trigger_at(_at(15, 20), state=SquareOffState.FAILED)
    assert trigger is SquareOffTrigger.SQUARE_OFF


def test_the_decision_is_pure_so_a_restarted_worker_agrees():
    policy = SquareOffPolicy()
    moment = _at(15, 16)
    first = policy.trigger_at(moment, state=SquareOffState.PENDING)
    second = policy.trigger_at(moment, state=SquareOffState.PENDING)
    assert first is second


def test_times_are_configurable():
    policy = SquareOffPolicy(entry_cutoff=time(11, 0), square_off_at=time(11, 30))
    assert not policy.entries_allowed(_at(11, 5))
    trigger = policy.trigger_at(_at(11, 30), state=SquareOffState.PENDING)
    assert trigger is SquareOffTrigger.SQUARE_OFF
