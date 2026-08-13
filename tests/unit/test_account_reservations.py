"""AccountReservationGate: atomic check-and-reserve, and the state machine
that governs every reservation afterward — most importantly, that UNKNOWN
has no legal exit except RECONCILED."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from common.persistence import migrate_account_shared_database, open_account_shared_database
from common.risk import (
    AccountReservationGate,
    IllegalReservationTransition,
)

NOW = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)


def _gate(tmp_path: Path) -> AccountReservationGate:
    database = open_account_shared_database(tmp_path / "dhan_account_shared.db")
    migrate_account_shared_database(database)
    with database.transaction() as conn:
        conn.executemany(
            "INSERT INTO live_account_state_provenance "
            "(account_key, reconciliation_status, last_reconciled_at, established_at) "
            "VALUES (?, 'reconciled', ?, ?)",
            ((account_key, NOW.isoformat(), NOW.isoformat()) for account_key in ("acct1", "acct2")),
        )
    return AccountReservationGate(database)


def _move(gate, new_state, *, correlation_id="l_io_st01_20260813_0001", account_key="acct1"):
    gate.transition(
        account_key=account_key, correlation_id=correlation_id, new_state=new_state, now=NOW
    )


def _reserve(
    gate, *, correlation_id="l_io_st01_20260813_0001", projected_capital=10_000.0, **overrides
):
    kwargs = dict(
        account_key="acct1",
        runtime_id="intraday_options",
        strategy_id="st01",
        correlation_id=correlation_id,
        trading_date="2026-08-13",
        projected_capital=projected_capital,
        projected_legs=1,
        quantity=75,
        max_deployed_capital=None,
        max_open_positions=None,
        max_open_legs=None,
        now=NOW,
    )
    kwargs.update(overrides)
    return gate.check_and_reserve(**kwargs)


# ------------------------------------------------------------ check-and-reserve
def test_a_reservation_within_limits_succeeds(tmp_path: Path):
    gate = _gate(tmp_path)
    decision = _reserve(gate, max_deployed_capital=50_000.0)
    assert decision.allowed
    state = gate.current_state(account_key="acct1", correlation_id="l_io_st01_20260813_0001")
    assert state == "RESERVED"


def test_a_reservation_that_would_exceed_max_deployed_capital_is_refused(tmp_path: Path):
    gate = _gate(tmp_path)
    decision = _reserve(gate, projected_capital=60_000.0, max_deployed_capital=50_000.0)
    assert not decision.allowed
    assert "max_deployed_capital" in decision.reason
    assert gate.current_state(account_key="acct1", correlation_id="l_io_st01_20260813_0001") is None


def test_a_reservation_that_would_exceed_max_open_positions_is_refused(tmp_path: Path):
    gate = _gate(tmp_path)
    _reserve(gate, correlation_id="l_io_st01_20260813_0001", max_open_positions=1)
    decision = _reserve(gate, correlation_id="l_io_st01_20260813_0002", max_open_positions=1)
    assert not decision.allowed
    assert "max_open_positions" in decision.reason


def test_a_reservation_that_would_exceed_max_open_legs_is_refused(tmp_path: Path):
    gate = _gate(tmp_path)
    _reserve(gate, correlation_id="l_io_st01_20260813_0001", projected_legs=3, max_open_legs=3)
    decision = _reserve(
        gate, correlation_id="l_io_st01_20260813_0002", projected_legs=1, max_open_legs=3
    )
    assert not decision.allowed
    assert "max_open_legs" in decision.reason


def test_two_reservations_that_individually_pass_but_collectively_exceed_the_limit(tmp_path: Path):
    """Single-process proof of the same property
    test_two_concurrent_workers_...  proves across real OS processes."""
    gate = _gate(tmp_path)
    first = _reserve(
        gate,
        correlation_id="l_io_st01_20260813_0001",
        projected_capital=30_000.0,
        max_deployed_capital=50_000.0,
    )
    second = _reserve(
        gate,
        correlation_id="l_io_st01_20260813_0002",
        projected_capital=30_000.0,
        max_deployed_capital=50_000.0,
    )
    assert first.allowed
    assert not second.allowed


def test_reserving_the_same_correlation_id_twice_is_refused_not_double_reserved(tmp_path: Path):
    gate = _gate(tmp_path)
    first = _reserve(gate, max_deployed_capital=50_000.0)
    second = _reserve(gate, max_deployed_capital=50_000.0)
    assert first.allowed
    assert not second.allowed
    assert "already exists" in second.reason


def test_accounts_are_independent(tmp_path: Path):
    gate = _gate(tmp_path)
    _reserve(gate, account_key="acct1", projected_capital=40_000.0, max_deployed_capital=50_000.0)
    decision = _reserve(
        gate,
        account_key="acct2",
        correlation_id="l_io_st01_20260813_0002",
        projected_capital=40_000.0,
        max_deployed_capital=50_000.0,
    )
    assert decision.allowed


def test_open_positions_capital_counts_toward_the_limit(tmp_path: Path):
    gate = _gate(tmp_path)
    database = open_account_shared_database(tmp_path / "dhan_account_shared.db")
    with database.transaction() as conn:
        conn.execute(
            "INSERT INTO live_open_positions (account_key, runtime_id, strategy_id, security_id, "
            "quantity, average_price, deployed_capital, opened_at, updated_at) VALUES "
            "('acct1', 'intraday_options', 'st01', 'sec1', 75, 190.0, 40000.0, ?, ?)",
            (NOW.isoformat(), NOW.isoformat()),
        )
    decision = _reserve(gate, projected_capital=20_000.0, max_deployed_capital=50_000.0)
    assert not decision.allowed


def test_no_configured_limit_never_blocks(tmp_path: Path):
    gate = _gate(tmp_path)
    decision = _reserve(gate, projected_capital=10_000_000.0)
    assert decision.allowed


def test_risk_reducing_exit_is_reserved_even_when_entry_state_is_untrusted(tmp_path: Path):
    gate = _gate(tmp_path)
    database = open_account_shared_database(tmp_path / "dhan_account_shared.db")
    with database.transaction() as conn:
        conn.execute(
            "UPDATE live_account_state_provenance SET reconciliation_status = 'failed' "
            "WHERE account_key = 'acct1'"
        )

    decision = _reserve(
        gate,
        projected_capital=0.0,
        projected_legs=0,
        max_deployed_capital=1.0,
        max_open_positions=1,
        max_open_legs=1,
        max_daily_loss=1.0,
        max_mtm_age_seconds=1.0,
        risk_reducing=True,
    )

    assert decision.allowed
    assert (
        gate.current_state(account_key="acct1", correlation_id="l_io_st01_20260813_0001")
        == "RESERVED"
    )


# ------------------------------------------------------------- state transitions
def test_pending_reservation_consumes_capacity_until_released(tmp_path: Path):
    """Requirement: pending/UNKNOWN submissions consume account risk
    capacity, not just filled positions."""
    gate = _gate(tmp_path)
    _reserve(
        gate,
        correlation_id="l_io_st01_20260813_0001",
        projected_capital=40_000.0,
        max_deployed_capital=50_000.0,
    )
    # Still RESERVED (not yet filled, not yet released) — the next
    # reservation must still see this one's capital.
    decision = _reserve(
        gate,
        correlation_id="l_io_st01_20260813_0002",
        projected_capital=20_000.0,
        max_deployed_capital=50_000.0,
    )
    assert not decision.allowed


def test_a_released_reservation_no_longer_consumes_capacity(tmp_path: Path):
    gate = _gate(tmp_path)
    _reserve(
        gate,
        correlation_id="l_io_st01_20260813_0001",
        projected_capital=40_000.0,
        max_deployed_capital=50_000.0,
    )
    gate.transition(
        account_key="acct1", correlation_id="l_io_st01_20260813_0001", new_state="REJECTED", now=NOW
    )
    gate.transition(
        account_key="acct1", correlation_id="l_io_st01_20260813_0001", new_state="RELEASED", now=NOW
    )
    decision = _reserve(
        gate,
        correlation_id="l_io_st01_20260813_0002",
        projected_capital=40_000.0,
        max_deployed_capital=50_000.0,
    )
    assert decision.allowed


@pytest.mark.parametrize(
    "path",
    [
        ["SUBMITTED", "ACKNOWLEDGED", "FILLED", "RELEASED"],
        ["SUBMITTED", "PARTIALLY_FILLED", "FILLED", "RELEASED"],
        ["SUBMITTED", "REJECTED", "RELEASED"],
        ["SUBMITTED", "CANCELLED", "RELEASED"],
        ["SUBMITTED", "EXPIRED", "RELEASED"],
        ["SUBMITTED", "UNKNOWN", "RECONCILED", "RELEASED"],
        ["REJECTED", "RELEASED"],
    ],
)
def test_every_legal_path_succeeds(tmp_path: Path, path: list[str]):
    gate = _gate(tmp_path)
    _reserve(gate)
    for new_state in path:
        _move(gate, new_state)
    assert (
        gate.current_state(account_key="acct1", correlation_id="l_io_st01_20260813_0001")
        == path[-1]
    )


def test_unknown_never_transitions_directly_to_released():
    """The one rule that matters most, checked directly against the graph."""
    from common.risk.account_reservations import _LEGAL_TRANSITIONS

    assert _LEGAL_TRANSITIONS["UNKNOWN"] == frozenset({"RECONCILED"})


def test_unknown_never_transitions_directly_to_rejected_or_cancelled():
    from common.risk.account_reservations import _LEGAL_TRANSITIONS

    assert "REJECTED" not in _LEGAL_TRANSITIONS["UNKNOWN"]
    assert "CANCELLED" not in _LEGAL_TRANSITIONS["UNKNOWN"]


def test_attempting_an_illegal_transition_raises(tmp_path: Path):
    gate = _gate(tmp_path)
    _reserve(gate)
    with pytest.raises(IllegalReservationTransition, match="not a legal transition"):
        gate.transition(
            account_key="acct1",
            correlation_id="l_io_st01_20260813_0001",
            new_state="FILLED",  # RESERVED cannot jump straight to FILLED
            now=NOW,
        )


def test_transitioning_an_unreserved_correlation_id_raises(tmp_path: Path):
    gate = _gate(tmp_path)
    with pytest.raises(IllegalReservationTransition, match="no reservation found"):
        _move(gate, "SUBMITTED", correlation_id="l_io_st01_99999999_0001")


def test_released_is_a_true_terminal_state(tmp_path: Path):
    gate = _gate(tmp_path)
    _reserve(gate)
    _move(gate, "REJECTED")
    _move(gate, "RELEASED")
    with pytest.raises(IllegalReservationTransition):
        _move(gate, "SUBMITTED")


def test_partial_fill_updates_residual_quantity(tmp_path: Path):
    """Requirement: partial fills retain correct residual reservation."""
    gate = _gate(tmp_path)
    _reserve(gate, quantity=3)
    _move(gate, "SUBMITTED")
    gate.transition(
        account_key="acct1",
        correlation_id="l_io_st01_20260813_0001",
        new_state="PARTIALLY_FILLED",
        now=NOW,
        residual_quantity=2,
    )
    database = open_account_shared_database(tmp_path / "dhan_account_shared.db")
    row = (
        database.connect()
        .execute(
            "SELECT residual_quantity FROM live_risk_reservations WHERE correlation_id = ?",
            ("l_io_st01_20260813_0001",),
        )
        .fetchone()
    )
    assert row["residual_quantity"] == 2
