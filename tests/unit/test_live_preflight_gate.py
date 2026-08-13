"""LivePreflightGate: TTL freshness, config-fingerprint invalidation, and
worker-local revalidation — never a value merely trusted from elsewhere."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from common.broker.live_preflight import (
    LivePreflightOutcome,
    PreflightCheckResult,
    run_live_preflight,
)
from common.broker.live_preflight_gate import LivePreflightGate
from common.persistence import migrate_account_shared_database, open_account_shared_database

PASS = PreflightCheckResult(True)
FAIL = PreflightCheckResult(False, "boom")


def _database(tmp_path: Path):
    database = open_account_shared_database(tmp_path / "dhan_account_shared.db")
    migrate_account_shared_database(database)
    return database


def _outcome(*, checked_at: datetime, config_fingerprint: str = "fp1", passed: bool = True):
    result = PASS if passed else FAIL
    return run_live_preflight(
        account_key="acct1",
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint=config_fingerprint,
        now=checked_at,
        static_ip=result,
        account_identity=PASS,
        shared_db_health=PASS,
        token=PASS,
        connectivity=PASS,
        confirmation=PASS,
    )


def test_current_is_none_before_any_preflight_has_run(tmp_path: Path):
    gate = LivePreflightGate(_database(tmp_path))
    stored = gate.current(account_key="acct1", runtime_id="intraday_options", strategy_id="st01")
    assert stored is None


def test_record_then_current_round_trips(tmp_path: Path):
    gate = LivePreflightGate(_database(tmp_path))
    now = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)
    gate.record(_outcome(checked_at=now))

    stored = gate.current(account_key="acct1", runtime_id="intraday_options", strategy_id="st01")
    assert stored is not None
    assert stored.passed is True
    assert stored.config_fingerprint == "fp1"
    assert stored.checked_at == now


def test_a_failed_outcome_is_recorded_as_failed_not_fabricated_as_passed(tmp_path: Path):
    gate = LivePreflightGate(_database(tmp_path))
    now = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)
    gate.record(_outcome(checked_at=now, passed=False))

    stored = gate.current(account_key="acct1", runtime_id="intraday_options", strategy_id="st01")
    assert stored is not None
    assert stored.passed is False
    assert "static_ip" in stored.detail


def test_is_fresh_within_ttl(tmp_path: Path):
    gate = LivePreflightGate(_database(tmp_path))
    checked_at = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)
    gate.record(_outcome(checked_at=checked_at))
    stored = gate.current(account_key="acct1", runtime_id="intraday_options", strategy_id="st01")

    assert gate.is_fresh(
        stored, now=checked_at + timedelta(seconds=100), ttl_seconds=300, config_fingerprint="fp1"
    )


def test_is_fresh_false_once_ttl_elapses(tmp_path: Path):
    gate = LivePreflightGate(_database(tmp_path))
    checked_at = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)
    gate.record(_outcome(checked_at=checked_at))
    stored = gate.current(account_key="acct1", runtime_id="intraday_options", strategy_id="st01")

    assert not gate.is_fresh(
        stored, now=checked_at + timedelta(seconds=301), ttl_seconds=300, config_fingerprint="fp1"
    )


def test_is_fresh_false_on_config_fingerprint_change_even_within_ttl(tmp_path: Path):
    """A config change invalidates freshness immediately — TTL or not."""
    gate = LivePreflightGate(_database(tmp_path))
    checked_at = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)
    gate.record(_outcome(checked_at=checked_at, config_fingerprint="fp_old"))
    stored = gate.current(account_key="acct1", runtime_id="intraday_options", strategy_id="st01")

    assert not gate.is_fresh(
        stored, now=checked_at + timedelta(seconds=1), ttl_seconds=300, config_fingerprint="fp_new"
    )


def test_ensure_fresh_reuses_a_fresh_matching_result_without_rerunning(tmp_path: Path):
    gate = LivePreflightGate(_database(tmp_path))
    checked_at = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)
    gate.record(_outcome(checked_at=checked_at))

    calls = []

    def run_check():
        calls.append(1)
        raise AssertionError("must not re-run a still-fresh, matching-config preflight")

    result = gate.ensure_fresh(
        account_key="acct1",
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp1",
        now=checked_at + timedelta(seconds=10),
        ttl_seconds=300,
        run_check=run_check,
    )
    assert calls == []
    assert result.passed is True


def test_force_reruns_even_a_fresh_parent_result_at_the_worker_boundary(tmp_path: Path):
    gate = LivePreflightGate(_database(tmp_path))
    checked_at = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)
    gate.record(_outcome(checked_at=checked_at))
    rerun_at = checked_at + timedelta(seconds=1)
    calls: list[int] = []

    def run_check():  # type: ignore[no-untyped-def]
        calls.append(1)
        return _outcome(checked_at=rerun_at)

    result = gate.ensure_fresh(
        account_key="acct1",
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp1",
        now=rerun_at,
        ttl_seconds=300,
        run_check=run_check,
        force=True,
    )

    assert calls == [1]
    assert result.checked_at == rerun_at


def test_ensure_fresh_reruns_when_stale(tmp_path: Path):
    gate = LivePreflightGate(_database(tmp_path))
    checked_at = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)
    gate.record(_outcome(checked_at=checked_at))

    fresh_time = checked_at + timedelta(seconds=1000)
    calls = []

    def run_check():
        calls.append(1)
        return _outcome(checked_at=fresh_time)

    result = gate.ensure_fresh(
        account_key="acct1",
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp1",
        now=fresh_time,
        ttl_seconds=300,
        run_check=run_check,
    )
    assert calls == [1]
    assert result.checked_at == fresh_time


def test_ensure_fresh_reruns_when_no_prior_result_exists(tmp_path: Path):
    gate = LivePreflightGate(_database(tmp_path))
    now = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)
    calls = []

    def run_check():
        calls.append(1)
        return _outcome(checked_at=now)

    result = gate.ensure_fresh(
        account_key="acct1",
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp1",
        now=now,
        ttl_seconds=300,
        run_check=run_check,
    )
    assert calls == [1]
    assert result.passed is True


def test_ensure_fresh_never_fabricates_a_pass_from_a_failed_rerun(tmp_path: Path):
    gate = LivePreflightGate(_database(tmp_path))
    now = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)

    def run_check():
        return _outcome(checked_at=now, passed=False)

    result = gate.ensure_fresh(
        account_key="acct1",
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp1",
        now=now,
        ttl_seconds=300,
        run_check=run_check,
    )
    assert result.passed is False


def test_outcome_type_is_the_orchestrators_own(tmp_path: Path):
    """Sanity: LivePreflightOutcome is what run_live_preflight actually
    returns, and record() accepts it directly."""
    outcome = _outcome(checked_at=datetime(2026, 8, 13, 9, 15, tzinfo=UTC))
    assert isinstance(outcome, LivePreflightOutcome)
