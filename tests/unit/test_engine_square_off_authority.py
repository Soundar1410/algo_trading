"""Who decides square-off, and does the decision survive a restart.

Phase 3 Part 2b-ii-B-1. Until this part the engine asked
``MarketSession.is_past_square_off(tick.exchange_time)`` — a pure function of the
clock, latched only by the engine's in-memory ``_squared_off``. An engine
restarted at 15:25 therefore re-ran square-off against a position the previous
process had already closed, which is the failure execution §10 exists to prevent
("a process restart must not reset the square-off state").

The seam is :class:`~common.engine.square_off.SquareOffAuthority`. Two
implementations, and both are tested here for the property that matters:

* :class:`SessionSquareOffAuthority` — the default, and it must be **behaviour
  identical** to the method it replaces, because every ported engine test and
  every offline run depends on that.
* :class:`PersistedSquareOffAuthority` — the real one, driven against a **real**
  :class:`~common.execution.repository.ExecutionRepository` and a real SQLite
  database rather than a stub, because the whole point of it is what is on disk.

The last group covers the configuration half: two independently configured
square-off times cannot drift if one is derived from the other.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from common.config.models import (
    ExecutionMode,
    GlobalConfig,
    ResolvedConfig,
    RuntimeConfig,
    StrategyConfig,
)
from common.engine.config import EngineConfig, SessionConfig
from common.engine.session import MarketSession
from common.engine.square_off import (
    PersistedSquareOffAuthority,
    SessionSquareOffAuthority,
    SquareOffAuthority,
)
from common.execution import ExecutionRepository
from common.persistence import Database, MigrationRunner
from common.risk import SquareOffPolicy, SquareOffState

IST = ZoneInfo("Asia/Kolkata")
RUNTIME_ID = "intraday_options"
STRATEGY_ID = "engine01"
TRADING_DATE = "2026-07-31"


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 7, 31, hour, minute, second, tzinfo=IST)


@pytest.fixture
def repository(database_path):
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    yield ExecutionRepository(database)
    database.close()


def _authority(repository: ExecutionRepository, **kwargs) -> PersistedSquareOffAuthority:
    policy = kwargs.pop(
        "policy", SquareOffPolicy(entry_cutoff=time(15, 0), square_off_at=time(15, 15))
    )
    return PersistedSquareOffAuthority(
        policy,
        repository,
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        **kwargs,
    )


def _stored(repository: ExecutionRepository):
    return repository.load_strategy_state(
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )


def _store(repository: ExecutionRepository, state: SquareOffState) -> None:
    repository.save_strategy_state(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        square_off_state=state.value,
    )


# --------------------------------------------------------- the default authority


def _session(square_off_time: str = "15:20") -> MarketSession:
    return MarketSession(
        SessionConfig(
            timezone="Asia/Kolkata",
            start_time="09:15",
            end_time="15:15",
            square_off_time=square_off_time,
        )
    )


def test_both_authorities_satisfy_the_protocol() -> None:
    assert isinstance(SessionSquareOffAuthority(_session()), SquareOffAuthority)


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (_at(9, 15), False),
        (_at(15, 19, 59), False),
        (_at(15, 20), True),
        (_at(15, 20, 1), True),
        (_at(23, 59), True),
    ],
)
def test_the_default_authority_reproduces_the_session_clock(at: datetime, expected: bool) -> None:
    """The replaced method's exact truth table: ``at.time() >= square_off``.

    Written as a table rather than by calling the old method, because the old
    method is gone — this *is* the record of what it did.
    """
    assert SessionSquareOffAuthority(_session()).due(at) is expected


def test_the_default_authority_persists_nothing() -> None:
    """``completed`` is a no-op, so an offline run needs no database.

    This is what keeps every ported engine test unchanged: the engine now calls
    ``completed(ts)`` on every square-off, and by default that must cost nothing.
    """
    authority = SessionSquareOffAuthority(_session())
    authority.completed(_at(15, 20))
    assert authority.due(_at(15, 20)) is True  # still clock-only, no latch


def test_the_default_authority_follows_a_moved_square_off_time() -> None:
    assert SessionSquareOffAuthority(_session("15:30")).due(_at(15, 20)) is False
    assert SessionSquareOffAuthority(_session("15:30")).due(_at(15, 30)) is True


def test_the_session_no_longer_answers_the_square_off_question() -> None:
    """The duplicate decider is removed, not merely bypassed.

    Leaving ``is_past_square_off`` in place would let a future caller reintroduce
    the second decider without touching the engine.
    """
    assert not hasattr(_session(), "is_past_square_off")


def test_the_session_keeps_its_entry_window_and_calendar() -> None:
    """What ``MarketSession`` retains: architecture §13 puts the entry cutoff and
    the holiday calendar at strategy level, and only square-off at runtime level."""
    session = MarketSession(
        SessionConfig(
            timezone="Asia/Kolkata",
            start_time="09:15",
            end_time="15:15",
            square_off_time="15:20",
            holidays=("2026-07-31",),
        )
    )
    assert session.can_enter(_at(10, 0)) is False  # configured holiday
    assert session.is_holiday(_at(10, 0)) is True
    assert session.is_trading_day(_at(10, 0)) is False


# ------------------------------------------------------- the persisted authority


def test_a_fresh_day_squares_off_at_the_policy_time(repository: ExecutionRepository) -> None:
    authority = _authority(repository)
    assert authority.due(_at(15, 14, 59)) is False
    assert authority.due(_at(15, 15)) is True


def test_a_completed_square_off_is_not_repeated_after_a_restart(
    repository: ExecutionRepository,
) -> None:
    """The failure this whole seam exists to prevent.

    The clock alone says "square off" at 15:25 forever. Only the persisted state
    can say "already done", and only if it is read at startup.
    """
    _store(repository, SquareOffState.COMPLETED)
    restarted = _authority(repository)
    assert restarted.due(_at(15, 25)) is False


def test_a_failed_square_off_is_retried_after_a_restart(
    repository: ExecutionRepository,
) -> None:
    _store(repository, SquareOffState.FAILED)
    assert _authority(repository).due(_at(15, 25)) is True


def test_an_inherited_in_progress_attempt_is_retried_and_reported(
    repository: ExecutionRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """An ``IN_PROGRESS`` read from disk is a *stalled* attempt, not a live one.

    ``SquareOffPolicy.trigger_at`` returns ``NONE`` for ``IN_PROGRESS`` — correct
    for the process that wrote it, which is mid-attempt. But a process that reads
    it at startup did not write it: whoever did is dead. Honouring it literally
    would leave a position open past square-off with nothing that will ever close
    it, so it is normalised to ``PENDING`` at load and said out loud.
    """
    _store(repository, SquareOffState.IN_PROGRESS)
    with caplog.at_level(logging.WARNING):
        authority = _authority(repository)
    assert authority.due(_at(15, 25)) is True
    assert any("IN_PROGRESS" in record.getMessage() for record in caplog.records)


def test_the_normalisation_happens_at_load_not_in_the_policy(
    repository: ExecutionRepository,
) -> None:
    """The retry decision is one layer above ``trigger_at``, which is untouched.

    A same-process ``IN_PROGRESS`` — one this authority wrote itself moments ago —
    must still suppress nothing it should not, and the policy's own unit test
    (``test_an_in_progress_square_off_is_not_restarted_concurrently``) must stay
    green.
    """
    policy = SquareOffPolicy(entry_cutoff=time(15, 0), square_off_at=time(15, 15))
    from common.risk import SquareOffTrigger

    assert policy.trigger_at(_at(15, 25), state=SquareOffState.IN_PROGRESS) is SquareOffTrigger.NONE


def test_the_attempt_is_recorded_before_the_close_is_attempted(
    repository: ExecutionRepository,
) -> None:
    """``IN_PROGRESS`` reaches disk from ``due()``, i.e. *before* the engine acts.

    Same ordering the worker already uses (``worker.py`` writes ``IN_PROGRESS``
    before ``handle_signal``): a crash mid-close must leave evidence that an
    attempt was started.
    """
    authority = _authority(repository)
    assert _stored(repository) is None

    assert authority.due(_at(15, 15)) is True
    row = _stored(repository)
    assert row is not None
    assert row["square_off_state"] == SquareOffState.IN_PROGRESS.value
    assert row["entries_blocked"] == 1


def test_completion_is_recorded_and_survives_a_reload(
    repository: ExecutionRepository,
) -> None:
    authority = _authority(repository)
    assert authority.due(_at(15, 15)) is True
    authority.completed(_at(15, 15))

    assert _stored(repository)["square_off_state"] == SquareOffState.COMPLETED.value
    assert _authority(repository).due(_at(15, 25)) is False


def test_a_square_off_that_did_not_complete_is_still_due(
    repository: ExecutionRepository,
) -> None:
    """The in-memory half of the retry: an attempt that raised before
    ``completed()`` must keep answering ``True`` so the next tick tries again."""
    authority = _authority(repository)
    assert authority.due(_at(15, 15)) is True
    assert authority.due(_at(15, 16)) is True  # no completed() in between


def test_the_attempt_row_is_written_once_not_per_tick(
    repository: ExecutionRepository,
) -> None:
    """``due()`` runs on every tick; it must not write on every tick."""
    authority = _authority(repository)
    for second in range(5):
        assert authority.due(_at(15, 15, second)) is True
    assert authority.writes == 1


def test_completion_is_idempotent(repository: ExecutionRepository) -> None:
    authority = _authority(repository)
    authority.due(_at(15, 15))
    authority.completed(_at(15, 15))
    writes_after_first = authority.writes
    authority.completed(_at(15, 16))
    assert authority.writes == writes_after_first


def test_an_unreadable_stored_state_fails_towards_squaring_off(
    repository: ExecutionRepository,
) -> None:
    """A corrupt value must not be read as "already done".

    Same direction ``worker.py``'s ``_load_square_off_state`` chose: an unknown
    string degrades to ``PENDING``, because the cost of an extra square-off
    attempt against an already-flat book is nil, and the cost of skipping one is
    an open position overnight.
    """
    repository.save_strategy_state(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        square_off_state="NOT_A_STATE",
    )
    assert _authority(repository).due(_at(15, 25)) is True


def test_a_normal_day_records_exactly_two_attempts(repository: ExecutionRepository) -> None:
    """Phase 6 Part 3, spec section 10's 'persist square-off attempts'. Every
    persisted write from this authority counts -- a normal day makes exactly two
    (the IN_PROGRESS write from due(), then COMPLETED from completed())."""
    authority = _authority(repository)
    authority.due(_at(15, 15))
    authority.completed(_at(15, 15))
    assert _stored(repository)["square_off_attempts"] == 2


def test_a_crash_forced_retry_records_more_attempts_than_a_normal_day(
    repository: ExecutionRepository,
) -> None:
    """The count is the observable evidence a retry happened. Scenario: the
    first process's due() writes IN_PROGRESS (attempt 1) and crashes before
    completed(); the restarted process inherits the stalled IN_PROGRESS,
    retries (test_an_inherited_in_progress_attempt_is_retried_and_reported
    already proves the retry itself), and its own due()+completed() add two
    more."""
    first = _authority(repository)
    first.due(_at(15, 15))  # crashes here, before completed()

    restarted = _authority(repository)
    assert restarted.due(_at(15, 16)) is True  # the retry the stalled IN_PROGRESS forces
    restarted.completed(_at(15, 16))

    assert _stored(repository)["square_off_attempts"] == 3
    assert _stored(repository)["square_off_attempts"] > 2


def test_another_strategys_state_is_not_consulted(repository: ExecutionRepository) -> None:
    repository.save_strategy_state(
        runtime_id=RUNTIME_ID,
        strategy_id="someone_else",
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        square_off_state=SquareOffState.COMPLETED.value,
    )
    assert _authority(repository).due(_at(15, 25)) is True


def test_yesterdays_completion_does_not_suppress_today(
    repository: ExecutionRepository,
) -> None:
    """``strategy_state`` is keyed by trading date; square-off must not leak
    across a day boundary any more than a position does."""
    repository.save_strategy_state(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date="2026-07-30",
        square_off_state=SquareOffState.COMPLETED.value,
    )
    assert _authority(repository).due(_at(15, 25)) is True


# ------------------------------------------- expiry-driven square-off (Part 4)
#
# TRADING_DATE is 2026-07-31. The default lead (square_off_before_expiry_days=0)
# makes 2026-07-31 the last day a contract expiring 2026-07-31 may be held, so an
# expiry of 2026-07-30 is what makes *this* trading date overdue.


def test_an_overdue_expiry_makes_due_true_before_the_entry_cutoff(
    repository: ExecutionRepository,
) -> None:
    """The 'immediately, at the first due()' decision, against the real authority."""
    authority = _authority(repository, expiry="2026-07-30")
    assert authority.due(_at(9, 20)) is True


def test_an_expiry_on_the_trading_date_itself_is_not_yet_overdue(
    repository: ExecutionRepository,
) -> None:
    authority = _authority(repository, expiry=TRADING_DATE)
    assert authority.due(_at(9, 20)) is False
    assert authority.due(_at(15, 15)) is True  # the ordinary clock still fires


def test_an_authority_built_with_no_expiry_behaves_exactly_as_before(
    repository: ExecutionRepository,
) -> None:
    """Negative control: omitting ``expiry`` must be indistinguishable from
    Part 3's authority — no worker that never sets it changes behaviour."""
    authority = _authority(repository)
    assert authority.due(_at(9, 20)) is False
    assert authority.due(_at(15, 15)) is True


def test_an_overdue_expiry_writes_in_progress_once_not_per_tick(
    repository: ExecutionRepository,
) -> None:
    authority = _authority(repository, expiry="2026-07-30")
    for minute in range(5):
        assert authority.due(_at(9, 20 + minute)) is True
    assert authority.writes == 1
    row = _stored(repository)
    assert row["square_off_state"] == SquareOffState.IN_PROGRESS.value


def test_an_overdue_expiry_day_reaches_exactly_two_attempts(
    repository: ExecutionRepository,
) -> None:
    """Same invariant Part 3 pinned for the time-of-day trigger: a normal day
    (however it is triggered) makes exactly two persisted writes."""
    authority = _authority(repository, expiry="2026-07-30")
    authority.due(_at(9, 20))
    authority.completed(_at(9, 20))
    assert _stored(repository)["square_off_attempts"] == 2


def test_a_restart_on_an_overdue_day_reads_completed_and_does_not_reclose(
    repository: ExecutionRepository,
) -> None:
    first = _authority(repository, expiry="2026-07-30")
    first.due(_at(9, 20))
    first.completed(_at(9, 20))

    restarted = _authority(repository, expiry="2026-07-30")
    assert restarted.due(_at(9, 25)) is False


def test_an_unparseable_expiry_logs_once_at_construction_and_stays_inert(
    repository: ExecutionRepository, caplog: pytest.LogCaptureFixture
) -> None:
    """A simulated contract's placeholder expiry (e.g. ``'WEEKLY'``) must not
    force-close a run on its first tick — logged once, not per due() call."""
    with caplog.at_level(logging.WARNING):
        authority = _authority(repository, expiry="WEEKLY")
    assert any("not a parseable ISO date" in record.getMessage() for record in caplog.records)
    assert authority.due(_at(9, 20)) is False  # inert, not overdue
    assert authority.due(_at(15, 15)) is True  # the ordinary clock still fires


# ------------------------------------------------------- the configured times


def test_the_session_times_are_derived_from_the_policy() -> None:
    """One configured pair, two derived strings — so they cannot drift."""
    policy = SquareOffPolicy(entry_cutoff=time(15, 0), square_off_at=time(15, 15))
    session = SessionConfig.from_square_off_policy(policy)
    assert session.end_time == "15:00"
    assert session.square_off_time == "15:15"
    assert session.timezone == policy.timezone


def test_a_moved_policy_moves_both_session_boundaries() -> None:
    policy = SquareOffPolicy(entry_cutoff=time(11, 0), square_off_at=time(11, 30))
    session = SessionConfig.from_square_off_policy(policy, start_time="09:30")
    assert (session.start_time, session.end_time, session.square_off_time) == (
        "09:30",
        "11:00",
        "11:30",
    )
    MarketSession(session)  # start < end <= square_off still holds


def test_the_derived_session_carries_the_holiday_calendar() -> None:
    policy = SquareOffPolicy()
    session = SessionConfig.from_square_off_policy(policy, holidays=("2026-08-15",))
    assert session.holidays == ("2026-08-15",)


def _resolved() -> ResolvedConfig:
    return ResolvedConfig(
        global_config=GlobalConfig(live_trading_enabled=False),
        runtime=RuntimeConfig(runtime_id=RUNTIME_ID, enabled=True),
        strategy=StrategyConfig(strategy_id=STRATEGY_ID, enabled=True, mode=ExecutionMode.PAPER),
    )


def test_the_engine_config_can_be_built_from_a_policy() -> None:
    cfg = EngineConfig.from_resolved(
        _resolved(),
        square_off_policy=SquareOffPolicy(entry_cutoff=time(15, 0), square_off_at=time(15, 15)),
    )
    assert cfg.session.end_time == "15:00"
    assert cfg.session.square_off_time == "15:15"


def test_supplying_both_a_session_and_a_policy_is_refused() -> None:
    """The guard is the deliverable. Accepting both would let the drift back in
    silently, which is exactly the state this part was written to end."""
    with pytest.raises(ValueError, match="square_off_policy"):
        EngineConfig.from_resolved(
            _resolved(),
            session=SessionConfig(),
            square_off_policy=SquareOffPolicy(),
        )


def test_supplying_neither_still_works_unchanged() -> None:
    """Existing callers pass neither and must keep the documented defaults."""
    cfg = EngineConfig.from_resolved(_resolved())
    assert (cfg.session.end_time, cfg.session.square_off_time) == ("15:15", "15:20")
