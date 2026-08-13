"""Live-only worker bootstrap, imported lazily after the worker lock is held.

This is the production call site for static-IP preflight, TTL revalidation,
account-wide rate/risk coordination, and startup reconciliation.  Paper workers
never import it.  The committed configuration cannot reach it because every
live gate remains disabled; deterministic tests inject fakes at the surrounding
seams and never contact Dhan.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from common.authentication import AuthBootstrap, AuthCredentials
from common.broker import (
    Broker,
    GuardedLiveCall,
    LiveBrokerDependencies,
    LiveExecutionBlocked,
    LiveOrderRateLimiter,
    build_broker,
)
from common.broker.dhan_preflight import run_configured_live_preflight
from common.broker.live_preflight import (
    LivePreflightOutcome,
    derive_account_key,
    load_egress_ip_provider,
)
from common.broker.live_preflight_gate import LivePreflightGate
from common.broker.live_rate_limiter import rule_for
from common.broker.order_update_consumer import DhanOrderUpdateStream, OrderUpdateInbox
from common.broker.paper import InstrumentRulesLookup
from common.config import ResolvedConfig, Settings, fingerprint
from common.config.models import ExecutionMode, RateLimitCallClass
from common.config.secrets import read_secret
from common.execution import ExecutionRepository, LiveAccountRiskLimits
from common.market_data.dhan import build_dhan_order_client
from common.persistence import (
    Database,
    migrate_account_shared_database,
    open_account_shared_database,
)
from common.reconciliation import (
    LocalOrderState,
    LocalPositionState,
    ProvenanceStatus,
    ReconciliationRunner,
    RuntimeGroupSnapshot,
    get_provenance,
    rebuild_account_shared_state,
)
from common.risk import AccountReservationGate

from .worker import WorkerConfig, resolved_config_from_worker


@dataclass(slots=True)
class LiveRuntimeContext:
    broker: Broker
    reservation_gate: AccountReservationGate
    account_key: str
    risk_limits: LiveAccountRiskLimits
    account_database: Database
    account_live_lock: FileLock
    order_update_stream: DhanOrderUpdateStream

    def close(self) -> None:
        try:
            self.order_update_stream.stop()
        finally:
            try:
                self.account_database.close()
            finally:
                self.account_live_lock.release()


def _acquire_account_live_lock(lock_dir: Path, account_key: str) -> FileLock:
    """Hold the controlled-rollout account lease for the worker lifetime.

    Phase 10 deliberately rolls out exactly one minimum-quantity live strategy
    while another remains paper.  The shared risk/limiter tables are capable of
    coordinating multiple processes, but startup reconstruction cannot safely
    claim it has enumerated every runtime database until multi-live rollout is
    separately designed.  This lease makes that limitation executable and
    fail-closed instead of relying on operator discipline.
    """
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_dir / f"dhan_account_{account_key}.live.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout as exc:
        raise LiveExecutionBlocked(
            "another live worker already holds this Dhan account's controlled-rollout "
            "lease; Phase 10 permits exactly one live strategy at a time"
        ) from exc
    return lock


def _local_snapshot(
    repository: ExecutionRepository, *, strategy_id: str
) -> tuple[list[LocalOrderState], list[LocalPositionState]]:
    from common.models import OrderStatus

    orders = [
        LocalOrderState(
            correlation_id=str(row["correlation_id"]),
            status=OrderStatus(str(row["status"])),
        )
        for row in repository.all_orders(
            strategy_id=strategy_id, execution_mode=ExecutionMode.LIVE
        )
    ]
    pending = repository.database.connect().execute(
        "SELECT oi.correlation_id FROM order_intents oi "
        "LEFT JOIN orders o ON o.intent_id = oi.id "
        "WHERE oi.strategy_id = ? AND oi.execution_mode = 'live' AND o.id IS NULL",
        (strategy_id,),
    )
    orders.extend(
        LocalOrderState(
            correlation_id=str(row["correlation_id"]), status=OrderStatus.UNKNOWN
        )
        for row in pending
    )
    positions = [
        LocalPositionState(
            security_id=position.security_id,
            quantity=position.quantity,
            average_price=position.average_price,
            product_type="INTRADAY",
            status="OPEN",
        )
        for position in repository.open_positions_all_dates(
            strategy_id=strategy_id, execution_mode=ExecutionMode.LIVE
        )
    ]
    return orders, positions


def prepare_live_runtime(
    config: WorkerConfig,
    *,
    repository: ExecutionRepository,
    settings: Settings,
    exchange_segment: str,
    instrument_rules: InstrumentRulesLookup,
    now: datetime | None = None,
) -> LiveRuntimeContext:
    """Build a live broker only after worker-local preflight and reconciliation.

    The worker calls this after acquiring its exclusive process lock and opening
    its local database.  ``LivePreflightGate.ensure_fresh(force=True)`` ensures
    a fresh parent-process row is never accepted as the child's own validation;
    subsequent broker calls re-run it at the configured TTL.
    """
    if config.execution_mode is not ExecutionMode.LIVE:
        raise ValueError("prepare_live_runtime is live-only")
    if config.account_shared_database_path is None or config.token_cache_dir is None:
        raise LiveExecutionBlocked("live worker has no account database/token-cache paths")
    if not config.config_fingerprint:
        raise LiveExecutionBlocked("live worker has no config fingerprint")

    client_id = read_secret(settings.dhan_client_id)
    pepper = read_secret(settings.account_identity_pepper)
    if not client_id or not pepper:
        raise LiveExecutionBlocked(
            "live worker requires DHAN_CLIENT_ID and ACCOUNT_IDENTITY_PEPPER"
        )
    bootstrap = AuthBootstrap(
        AuthCredentials(
            client_id=client_id,
            pin=read_secret(settings.dhan_pin),
            totp_secret=read_secret(settings.dhan_totp_secret),
            access_token=read_secret(settings.dhan_access_token),
        ),
        cache_dir=config.token_cache_dir,
    )
    token, _ = bootstrap.get_token()
    order_client = build_dhan_order_client(client_id=client_id, access_token=token)

    account_database = open_account_shared_database(config.account_shared_database_path)
    migrate_account_shared_database(account_database)
    resolved = resolved_config_from_worker(config)
    preflight_config = resolved.runtime.live_preflight
    ttl = preflight_config.max_preflight_age_seconds
    if ttl is None:
        account_database.close()
        raise LiveExecutionBlocked("live preflight TTL is missing")
    account_key = derive_account_key(client_id, pepper)
    try:
        account_live_lock = _acquire_account_live_lock(Path(config.lock_dir), account_key)
    except Exception:
        account_database.close()
        raise
    rules = preflight_config.rate_limits.rules
    limiter = LiveOrderRateLimiter(account_database)
    gate = LivePreflightGate(account_database)
    provider = load_egress_ip_provider(preflight_config.egress_ip_provider)
    if now is not None:
        fixed_now = now

        def clock() -> datetime:
            return fixed_now

    else:

        def clock() -> datetime:
            return datetime.now(UTC)

    def _before_preflight_read() -> None:
        read_rule = rule_for(rules, RateLimitCallClass.READ)
        if read_rule is None:
            raise LiveExecutionBlocked("no READ rate-limit rule configured")
        decision = limiter.reserve(
            account_key=account_key,
            call_class=RateLimitCallClass.READ,
            rule=read_rule,
            now=clock(),
        )
        if not decision.allowed:
            raise LiveExecutionBlocked(decision.reason or "READ rate limit reached")

    def _run_preflight() -> LivePreflightOutcome:
        return run_configured_live_preflight(
            account_database=account_database,
            client_id=client_id,
            access_token=token,
            account_identity_pepper=pepper,
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            config_fingerprint=config.config_fingerprint or "",
            expected_static_ip=preflight_config.expected_static_ip,
            egress_provider=provider,
            order_client=order_client,
            before_read=_before_preflight_read,
            now=clock(),
        )

    call_guard = GuardedLiveCall(
        preflight_gate=gate,
        rate_limiter=limiter,
        account_key=account_key,
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        config_fingerprint=config.config_fingerprint,
        preflight_ttl_seconds=float(ttl),
        rate_rules=rules,
        run_preflight=_run_preflight,
        entry_ready=lambda: (
            get_provenance(account_database, account_key=account_key)
            == ProvenanceStatus.RECONCILED
            and order_update_stream.is_connected
        ),
        now=clock,
    )
    update_inbox = OrderUpdateInbox()
    order_update_stream = DhanOrderUpdateStream(
        client_id=client_id,
        access_token=token,
        inbox=update_inbox,
    )
    stream_started = False
    try:
        call_guard.ensure_preflight(force=True)
        order_update_stream.start()
        stream_started = True
        if not order_update_stream.wait_until_connected(10.0):
            raise LiveExecutionBlocked(
                "Dhan order-update stream did not connect; live entries remain blocked"
            )
        broker = build_broker(
            resolved,
            preflight_passed=True,
            instrument_rules=instrument_rules,
            live_dependencies=LiveBrokerDependencies(
                client=order_client,
                exchange_segment=exchange_segment,
                product_type="INTRADAY",
                call_guard=call_guard,
                order_updates=update_inbox,
            ),
        )
        local_orders, local_positions = _local_snapshot(
            repository, strategy_id=config.strategy_id
        )
        rebuild = rebuild_account_shared_state(
            account_database,
            account_key=account_key,
            runtime_groups=(
                RuntimeGroupSnapshot(
                    runtime_id=config.runtime_id,
                    strategy_id=config.strategy_id,
                    broker=broker,
                    local_orders=local_orders,
                    local_positions=local_positions,
                    reconciliation_runner=ReconciliationRunner(repository.database),
                ),
            ),
            lock_path=Path(config.lock_dir) / "dhan_account_rebuild.lock",
        )
        if rebuild.status != "reconciled":
            raise LiveExecutionBlocked(
                f"startup account reconciliation failed: {rebuild.error_message}"
            )
    except Exception:
        if stream_started:
            order_update_stream.stop()
        account_database.close()
        account_live_lock.release()
        raise

    risk = preflight_config.account_risk
    return LiveRuntimeContext(
        broker=broker,
        reservation_gate=AccountReservationGate(account_database),
        account_key=account_key,
        risk_limits=LiveAccountRiskLimits(
            max_deployed_capital=risk.max_deployed_capital,
            max_open_positions=risk.max_open_positions,
            max_open_legs=risk.max_open_legs,
            max_daily_loss=risk.max_daily_loss,
            max_mtm_age_seconds=risk.max_mtm_age_seconds,
        ),
        account_database=account_database,
        account_live_lock=account_live_lock,
        order_update_stream=order_update_stream,
    )


def parent_live_preflight_passed(
    cfg: ResolvedConfig,
    *,
    settings: Settings,
    client_id: str,
    access_token: str,
    account_database_path: Path,
    order_client: Any,
    now: datetime | None = None,
) -> bool:
    """Run and persist the supervisor-side admission preflight.

    The spawned worker still forces its own independent check.  This parent
    result exists only to prevent spawning a live-designated worker whose
    static/token/account prerequisites are already known to fail; it is never
    trusted by the child as authorization for an order.
    """
    pepper = read_secret(settings.account_identity_pepper)
    if not pepper:
        return False
    database = open_account_shared_database(account_database_path)
    try:
        migrate_account_shared_database(database)
        account_key = derive_account_key(client_id, pepper)
        preflight = cfg.runtime.live_preflight
        rules = preflight.rate_limits.rules
        limiter = LiveOrderRateLimiter(database)
        current_time = now or datetime.now(UTC)

        def _before_read() -> None:
            read_rule = rule_for(rules, RateLimitCallClass.READ)
            if read_rule is None:
                raise LiveExecutionBlocked("no READ rate-limit rule configured")
            decision = limiter.reserve(
                account_key=account_key,
                call_class=RateLimitCallClass.READ,
                rule=read_rule,
                now=current_time,
            )
            if not decision.allowed:
                raise LiveExecutionBlocked(decision.reason or "READ rate limit reached")

        outcome = run_configured_live_preflight(
            account_database=database,
            client_id=client_id,
            access_token=access_token,
            account_identity_pepper=pepper,
            runtime_id=cfg.runtime.runtime_id,
            strategy_id=cfg.strategy.strategy_id,
            config_fingerprint=fingerprint(cfg),
            expected_static_ip=preflight.expected_static_ip,
            egress_provider=load_egress_ip_provider(preflight.egress_ip_provider),
            order_client=order_client,
            before_read=_before_read,
            now=current_time,
        )
        LivePreflightGate(database).record(outcome)
        return outcome.passed
    except Exception:
        return False
    finally:
        database.close()


__all__ = [
    "LiveRuntimeContext",
    "_acquire_account_live_lock",
    "parent_live_preflight_passed",
    "prepare_live_runtime",
]
