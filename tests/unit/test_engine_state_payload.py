"""``strategy_state.payload``, against a real database rather than a double.

Phase 3 Part 2b-ii-B-2. The module under test exists because
:meth:`~common.execution.repository.ExecutionRepository.save_strategy_state` has two
behaviours that are silent when you get them wrong, and both would corrupt restart
recovery rather than raise:

* ``payload = COALESCE(excluded.payload, payload)`` means ``payload=None``
  **preserves** the column. Writing ``None`` to clear the open-position record would
  leave a restarted engine adopting a position that had already been closed.
* a write replaces the whole column, so two writers with different keys clobber each
  other — and there are exactly two, ``LifecycleGateway`` and
  ``RepositoryReportWriter``, writing at different moments in the same run.

Both are asserted here against real SQL, because both are properties of the SQL and
a mock repository would happily agree with a wrong implementation.
"""

from __future__ import annotations

import pytest

from common.config.models import ExecutionMode
from common.engine.state_payload import (
    DAY_SUMMARY_KEY,
    OPEN_POSITION_KEY,
    UnsupportedStateVersion,
    merge_payload,
    read_payload,
)
from common.execution import ExecutionRepository
from common.models import CURRENT_STATE_VERSION
from common.persistence import Database, MigrationRunner

RUNTIME_ID = "intraday_options"
STRATEGY_ID = "engine01"
TRADING_DATE = "2026-07-16"
MODE = ExecutionMode.PAPER

_CONTRACT = {
    "symbol": "NIFTY 24000 CE",
    "security_id": "SIM:NIFTY:WEEKLY:24000:CE",
    "strike": 24000.0,
    "option_type": "CE",
    "expiry": "2026-07-23",
    "lot_size": 65,
    "side": "BUY",
    "lots": 1,
}


@pytest.fixture
def repository(database_path):
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    yield ExecutionRepository(database)
    database.close()


def _merge(repository: ExecutionRepository, updates: dict) -> dict:
    return merge_payload(
        repository,
        updates,
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=MODE,
        trading_date=TRADING_DATE,
    )


def _read(repository: ExecutionRepository) -> dict:
    return read_payload(
        repository, strategy_id=STRATEGY_ID, execution_mode=MODE, trading_date=TRADING_DATE
    )


def _raw_payload(repository: ExecutionRepository):
    row = repository.load_strategy_state(
        strategy_id=STRATEGY_ID, execution_mode=MODE, trading_date=TRADING_DATE
    )
    return None if row is None else row["payload"]


# ------------------------------------------------------------------ basics
def test_a_strategy_day_with_no_row_reads_as_empty(repository):
    assert _read(repository) == {}


def test_a_written_record_reads_back_unchanged(repository):
    _merge(repository, {OPEN_POSITION_KEY: _CONTRACT})
    assert _read(repository)[OPEN_POSITION_KEY] == _CONTRACT


def test_the_write_creates_the_row_when_there_is_none(repository):
    _merge(repository, {OPEN_POSITION_KEY: _CONTRACT})
    row = repository.load_strategy_state(
        strategy_id=STRATEGY_ID, execution_mode=MODE, trading_date=TRADING_DATE
    )
    assert row is not None
    # An insert has to leave the other columns at their documented defaults rather
    # than at whatever the payload write happened to pass through.
    assert row["square_off_state"] == "PENDING"
    assert row["entries_blocked"] == 0


# ------------------------------------------------- trap 1: clearing the column
def test_clearing_a_key_actually_removes_it(repository):
    _merge(repository, {OPEN_POSITION_KEY: _CONTRACT})
    _merge(repository, {OPEN_POSITION_KEY: None})
    assert OPEN_POSITION_KEY not in _read(repository)


def test_an_emptied_payload_is_written_as_an_object_not_null(repository):
    """The COALESCE trap, asserted on the column itself.

    ``payload = COALESCE(excluded.payload, payload)``: had the clear been expressed
    as ``payload=None``, this column would still hold the old contract and a restart
    would adopt a position that has already been closed. The distinction is invisible
    through :func:`read_payload` alone, so it is asserted on the raw value.
    """
    _merge(repository, {OPEN_POSITION_KEY: _CONTRACT})
    _merge(repository, {OPEN_POSITION_KEY: None})

    raw = _raw_payload(repository)
    assert raw is not None, "the column went NULL, so COALESCE will resurrect the old value"
    assert raw == "{}"


def test_passing_none_to_the_repository_directly_does_not_clear_it(repository):
    """The behaviour the helper exists to work around, pinned so it cannot surprise.

    If this ever starts failing, ``save_strategy_state`` has changed and
    :func:`merge_payload` should be revisited — not the other way round.
    """
    _merge(repository, {OPEN_POSITION_KEY: _CONTRACT})
    repository.save_strategy_state(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=MODE,
        trading_date=TRADING_DATE,
        payload=None,
    )
    assert _read(repository)[OPEN_POSITION_KEY] == _CONTRACT


# ------------------------------------------------ trap 2: two writers, one column
def test_two_keys_written_at_different_moments_both_survive(repository):
    _merge(repository, {OPEN_POSITION_KEY: _CONTRACT})
    _merge(repository, {DAY_SUMMARY_KEY: {"trade_count": 1, "net_pnl": 12.5}})

    payload = _read(repository)
    assert payload[OPEN_POSITION_KEY] == _CONTRACT
    assert payload[DAY_SUMMARY_KEY]["net_pnl"] == 12.5


def test_clearing_one_key_leaves_the_other_alone(repository):
    _merge(repository, {OPEN_POSITION_KEY: _CONTRACT})
    _merge(repository, {DAY_SUMMARY_KEY: {"trade_count": 1}})
    _merge(repository, {OPEN_POSITION_KEY: None})

    payload = _read(repository)
    assert OPEN_POSITION_KEY not in payload
    assert payload[DAY_SUMMARY_KEY] == {"trade_count": 1}


def test_a_payload_write_does_not_disturb_the_square_off_state(repository):
    """The two share a row, and the square-off state is the one that must not move."""
    repository.save_strategy_state(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=MODE,
        trading_date=TRADING_DATE,
        square_off_state="COMPLETED",
        entries_blocked=True,
    )
    _merge(repository, {OPEN_POSITION_KEY: _CONTRACT})

    row = repository.load_strategy_state(
        strategy_id=STRATEGY_ID, execution_mode=MODE, trading_date=TRADING_DATE
    )
    assert row["square_off_state"] == "COMPLETED"
    assert row["entries_blocked"] == 1


# ------------------------------------------------------------- bad data
@pytest.mark.parametrize("stored", ["not json at all", "[1, 2, 3]", '"a string"', "null"])
def test_unusable_stored_data_reads_as_empty_rather_than_raising(repository, stored):
    """A state file this build did not write must not stop the worker starting.

    The caller's absence handling — no recovered position, no prior summary — is
    already correct, and it is a far better outcome than refusing to trade.
    """
    repository.save_strategy_state(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=MODE,
        trading_date=TRADING_DATE,
    )
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE strategy_state SET payload = ? WHERE strategy_id = ?",
            (stored, STRATEGY_ID),
        )
    assert _read(repository) == {}


def test_the_payload_does_not_leak_across_trading_dates(repository):
    _merge(repository, {OPEN_POSITION_KEY: _CONTRACT})
    other = read_payload(
        repository, strategy_id=STRATEGY_ID, execution_mode=MODE, trading_date="2026-07-17"
    )
    assert other == {}, "intraday state must never cross a day boundary (spec section 12)"


def test_the_payload_does_not_leak_across_strategies(repository):
    _merge(repository, {OPEN_POSITION_KEY: _CONTRACT})
    other = read_payload(
        repository, strategy_id="someone_else", execution_mode=MODE, trading_date=TRADING_DATE
    )
    assert other == {}


# ------------------------------------------------ Phase 6 Part 3: state version
def test_a_fresh_row_carries_the_current_version_and_reads_fine(repository):
    """The write side stamps it explicitly (see repository tests); this pins
    that the read side then agrees with what was just written."""
    _merge(repository, {OPEN_POSITION_KEY: _CONTRACT})
    row = repository.load_strategy_state(
        strategy_id=STRATEGY_ID, execution_mode=MODE, trading_date=TRADING_DATE
    )
    assert row is not None
    assert row["state_version"] == CURRENT_STATE_VERSION
    assert read_payload(
        repository, strategy_id=STRATEGY_ID, execution_mode=MODE, trading_date=TRADING_DATE
    )[OPEN_POSITION_KEY] == _CONTRACT


def test_an_unrecognised_state_version_raises_rather_than_being_read(repository):
    """The one deliberate narrowing of 'never raises on bad data' (module
    docstring): a version mismatch means this build has no guarantee it can
    read the payload's shape correctly, unlike a payload that merely fails to
    decode -- see test_unusable_stored_data_reads_as_empty_rather_than_raising
    for the contrast this is deliberately different from."""
    _merge(repository, {OPEN_POSITION_KEY: _CONTRACT})
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE strategy_state SET state_version = ? WHERE strategy_id = ?",
            (CURRENT_STATE_VERSION + 1, STRATEGY_ID),
        )

    with pytest.raises(UnsupportedStateVersion, match="state_version"):
        read_payload(
            repository, strategy_id=STRATEGY_ID, execution_mode=MODE, trading_date=TRADING_DATE
        )


def test_an_older_state_version_also_raises_not_just_a_newer_one(repository):
    """'A version it does not understand' (spec wording) -- every version but
    the current one, not only ones ahead of it."""
    _merge(repository, {OPEN_POSITION_KEY: _CONTRACT})
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE strategy_state SET state_version = 0 WHERE strategy_id = ?",
            (STRATEGY_ID,),
        )

    with pytest.raises(UnsupportedStateVersion):
        read_payload(
            repository, strategy_id=STRATEGY_ID, execution_mode=MODE, trading_date=TRADING_DATE
        )


def test_merge_payload_also_propagates_the_version_raise(repository):
    """merge_payload reads before it writes -- the raise must surface through it too."""
    _merge(repository, {OPEN_POSITION_KEY: _CONTRACT})
    with repository.database.transaction() as conn:
        conn.execute(
            "UPDATE strategy_state SET state_version = ? WHERE strategy_id = ?",
            (CURRENT_STATE_VERSION + 1, STRATEGY_ID),
        )

    with pytest.raises(UnsupportedStateVersion):
        _merge(repository, {DAY_SUMMARY_KEY: {"trade_count": 1}})
