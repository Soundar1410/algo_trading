from __future__ import annotations

from datetime import UTC, datetime

import pytest

from common.broker.base import BrokerPosition
from common.broker.factory import LiveExecutionBlocked
from common.broker.live_preflight import (
    LivePreflightOutcome,
    PreflightCheckResult,
    derive_account_key,
)
from common.config.models import ExecutionMode
from common.config.settings import Settings
from common.execution import ExecutionRepository, LiveAccountRiskLimits
from common.models import Order, OrderStatus
from common.persistence import (
    Database,
    MigrationRunner,
    migrate_account_shared_database,
    open_account_shared_database,
)
from common.reconciliation import AccountRebuildResult
from common.risk import AccountReservationGate
from runtimes.intraday_options.live_runtime import (
    LiveRuntimeContext,
    _acquire_account_live_lock,
    account_shared_strategy_has_live_history,
    prepare_live_runtime,
)
from runtimes.intraday_options.worker import WorkerConfig


class _Broker:
    name = "fake-live"

    def __init__(self, *, orders=(), positions=()) -> None:
        self.orders = orders
        self.positions = positions
        self.reads = 0

    def fetch_order_book(self):  # type: ignore[no-untyped-def]
        self.reads += 1
        return self.orders

    def fetch_trades(self):  # type: ignore[no-untyped-def]
        self.reads += 1
        return ()

    def fetch_positions(self):  # type: ignore[no-untyped-def]
        self.reads += 1
        return self.positions

    def is_healthy(self):  # type: ignore[no-untyped-def]
        return True

    def submit(self, intent, quote):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def order_by_correlation_id(self, correlation_id):  # type: ignore[no-untyped-def]
        return None

    def modify(self, correlation_id, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def cancel(self, correlation_id):  # type: ignore[no-untyped-def]
        raise NotImplementedError


class _Stream:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _Lease:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


def _context(tmp_path, broker):  # type: ignore[no-untyped-def]
    operational = Database(tmp_path / "operational.db")
    MigrationRunner(operational).run_pending()
    repository = ExecutionRepository(operational)
    account = open_account_shared_database(tmp_path / "account.db")
    migrate_account_shared_database(account)
    with account.transaction() as conn:
        conn.execute(
            "INSERT INTO live_account_state_provenance "
            "(account_key, reconciliation_status, last_reconciled_at, established_at) "
            "VALUES ('acct', 'reconciled', ?, ?)",
            (datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
        )
    stream = _Stream()
    lease = _Lease()
    context = LiveRuntimeContext(
        broker=broker,
        reservation_gate=AccountReservationGate(account),
        account_key="acct",
        risk_limits=LiveAccountRiskLimits(),
        account_database=account,
        account_live_lock=lease,  # type: ignore[arg-type]
        order_update_stream=stream,  # type: ignore[arg-type]
        repository=repository,
        runtime_id="intraday_options",
        strategy_id="st01",
    )
    return context, stream, lease, account


def test_controlled_rollout_allows_only_one_live_worker_per_account(tmp_path) -> None:
    first = _acquire_account_live_lock(tmp_path, "account-key")
    try:
        try:
            _acquire_account_live_lock(tmp_path, "account-key")
        except LiveExecutionBlocked as exc:
            assert "exactly one live strategy" in str(exc)
        else:  # pragma: no cover - assertion aid
            raise AssertionError("a second account-wide live lease was granted")
    finally:
        first.release()


def test_controlled_rollout_lease_is_recoverable_after_clean_shutdown(tmp_path) -> None:
    first = _acquire_account_live_lock(tmp_path, "account-key")
    first.release()

    second = _acquire_account_live_lock(tmp_path, "account-key")
    second.release()


def test_live_close_reconciles_broker_flat_before_releasing_resources(tmp_path) -> None:
    broker = _Broker()
    context, stream, lease, _account = _context(tmp_path, broker)

    context.close()

    assert broker.reads == 3
    assert stream.stopped
    assert lease.released


def test_live_close_refuses_open_broker_exposure_and_marks_provenance_failed(tmp_path) -> None:
    broker = _Broker(
        positions=(
            BrokerPosition("sec1", quantity=75, average_price=190.0, product_type="INTRADAY"),
        )
    )
    context, stream, lease, account = _context(tmp_path, broker)

    with pytest.raises(LiveExecutionBlocked, match="critical mismatch"):
        context.close()

    # Re-open after close to verify the fail-closed provenance persisted.
    verify = open_account_shared_database(account.path)
    row = verify.connect().execute(
        "SELECT reconciliation_status FROM live_account_state_provenance "
        "WHERE account_key = 'acct'"
    ).fetchone()
    assert row["reconciliation_status"] == "failed"
    assert stream.stopped
    assert lease.released


def test_live_close_refuses_a_non_terminal_broker_order_even_when_identity_matches_nothing(
    tmp_path,
) -> None:
    broker = _Broker(
        orders=(
            Order(
                correlation_id="broker-only",
                strategy_id="st01",
                execution_mode=ExecutionMode.LIVE,
                status=OrderStatus.UNKNOWN,
                updated_at=datetime.now(UTC),
                broker_order_id="b1",
            ),
        )
    )
    context, _stream, _lease, _account = _context(tmp_path, broker)

    with pytest.raises(LiveExecutionBlocked):
        context.close()


def test_prepare_live_runtime_assembles_the_real_production_boundaries(
    tmp_path, monkeypatch
) -> None:
    """Exercise the production assembler, not only its individual components."""
    operational = Database(tmp_path / "operational.db")
    MigrationRunner(operational).run_pending()
    repository = ExecutionRepository(operational)
    config = WorkerConfig(
        runtime_id="intraday_options",
        strategy_id="st01",
        security_id="13",
        instrument="NIFTY",
        database_path=operational.path,
        lock_dir=tmp_path / "locks",
        pid_dir=tmp_path / "pid",
        log_dir=tmp_path / "logs",
        trading_date="2026-08-13",
        execution_mode=ExecutionMode.LIVE,
        config_fingerprint="fp1",
        global_live_trading_enabled=True,
        runtime_enabled=True,
        runtime_live_execution_allowed=True,
        strategy_enabled=True,
        strategy_live_approved=True,
        live_preflight_passed=True,
        live_quantity_lots=1,
        live_expected_static_ip="203.0.113.10",
        live_egress_ip_provider="tests.fake:provider",
        live_max_preflight_age_seconds=60,
        live_rate_limit_rules=(
            ("new_order", 5, 1),
            ("modify", 5, 1),
            ("cancel", 5, 1),
            ("read", 50, 1),
        ),
        live_max_daily_loss=5_000.0,
        live_max_open_positions=1,
        live_max_open_legs=1,
        live_max_deployed_capital=100_000.0,
        live_max_mtm_age_seconds=60,
        account_shared_database_path=tmp_path / "account.db",
        token_cache_dir=tmp_path / "tokens",
    )
    settings = Settings(
        dhan_client_id="client",
        dhan_access_token="token",
        account_identity_pepper="pepper",
    )
    now = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)
    account_key = derive_account_key("client", "pepper")
    passed = PreflightCheckResult(True)
    outcome = LivePreflightOutcome(
        account_key=account_key,
        runtime_id="intraday_options",
        strategy_id="st01",
        config_fingerprint="fp1",
        checked_at=now,
        static_ip=passed,
        account_identity=passed,
        shared_db_health=passed,
        token=passed,
        connectivity=passed,
        confirmation=passed,
    )
    broker = _Broker()
    events: list[str] = []
    registered_secrets: list[str] = []

    class _Redactor:
        def add_secrets(self, secrets):  # type: ignore[no-untyped-def]
            registered_secrets.extend(secrets)

    class _Bootstrap:
        def __init__(self, *_args, on_token_minted=None, **_kwargs):  # type: ignore[no-untyped-def]
            assert on_token_minted is not None
            on_token_minted("fresh-worker-token")

        def get_token(self):  # type: ignore[no-untyped-def]
            return "token", object()

    class _ConnectedStream(_Stream):
        is_connected = True

        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            super().__init__()

        def start(self) -> None:
            events.append("stream_started")

        def wait_until_connected(self, _timeout: float) -> bool:
            return True

    monkeypatch.setattr("runtimes.intraday_options.live_runtime.active_redactor", _Redactor)
    monkeypatch.setattr("runtimes.intraday_options.live_runtime.AuthBootstrap", _Bootstrap)
    monkeypatch.setattr(
        "runtimes.intraday_options.live_runtime.build_dhan_order_client",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "runtimes.intraday_options.live_runtime.load_egress_ip_provider",
        lambda _reference: object(),
    )
    monkeypatch.setattr(
        "runtimes.intraday_options.live_runtime.run_configured_live_preflight",
        lambda **_kwargs: outcome,
    )
    monkeypatch.setattr(
        "runtimes.intraday_options.live_runtime.DhanOrderUpdateStream", _ConnectedStream
    )
    monkeypatch.setattr(
        "runtimes.intraday_options.live_runtime.build_broker",
        lambda *_args, **_kwargs: broker,
    )

    def _rebuilt(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        events.append("startup_reconciled")
        return AccountRebuildResult(status="reconciled", critical_mismatch_total=0)

    monkeypatch.setattr(
        "runtimes.intraday_options.live_runtime.rebuild_account_shared_state", _rebuilt
    )

    context = prepare_live_runtime(
        config,
        repository=repository,
        settings=settings,
        exchange_segment="NSE_FNO",
        instrument_rules=lambda _security_id: None,
        now=now,
    )

    assert context.broker is broker
    assert events == ["stream_started", "startup_reconciled"]
    assert registered_secrets == ["fresh-worker-token"]
    context.close()
    assert broker.reads == 3


def test_transition_history_uses_account_ledger_when_runtime_database_is_missing(
    tmp_path,
) -> None:
    account_path = tmp_path / "account.db"
    account = open_account_shared_database(account_path)
    migrate_account_shared_database(account)
    account_key = derive_account_key("client", "pepper")
    with account.transaction() as conn:
        conn.execute(
            "INSERT INTO live_realised_pnl_events "
            "(account_key, runtime_id, strategy_id, trading_date, idempotency_key, "
            "realised_pnl_delta, recorded_at) VALUES "
            "(?, 'intraday_options', 'formerly_live', '2026-08-13', 'fill1', 10, ?)",
            (account_key, datetime.now(UTC).isoformat()),
        )
    account.close()

    assert account_shared_strategy_has_live_history(
        account_database_path=account_path,
        client_id="client",
        account_identity_pepper="pepper",
        strategy_id="formerly_live",
    )
    assert not account_shared_strategy_has_live_history(
        account_database_path=account_path,
        client_id="client",
        account_identity_pepper="pepper",
        strategy_id="brand_new_paper_strategy",
    )
