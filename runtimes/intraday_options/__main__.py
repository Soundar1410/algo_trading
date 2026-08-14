"""Entrypoint for the ``intraday_options`` supervisor.

    .venv/bin/python -m runtimes.intraday_options [--config-root config]

This is the missing link Phase 5 adds: :func:`~common.config.discover_enabled_
strategies` and :func:`~common.config.load_resolved_config` have existed since
Phase 0, but before this module nothing outside a test ever turned a strategy's
resolved YAML into a running worker. This module discovers every enabled
strategy under one runtime group, evaluates the live gate for each, and hands
the admitted ones to :class:`~runtimes.intraday_options.supervisor.
IntradayOptionsSupervisor`. A live-designated strategy that the gate blocks is
refused individually (spec's mixed-mode gate) — it never stops a paper
strategy elsewhere in the same group from starting, and it is never rerouted
to paper.

Requires real Dhan credentials in ``.env`` (see ``.env.example``). Every
committed strategy remains paper. The Phase 10 live route exists, but admission
requires all static gates plus parent and worker-local preflight and therefore
remains unreachable from committed configuration.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from common.authentication import AuthBootstrap, AuthCredentials, AuthError
from common.broker.base import Broker
from common.config import (
    ProjectPaths,
    ResolvedConfig,
    RuntimeConfig,
    Settings,
    discover_enabled_strategies,
    discover_strategies,
    effective_live_gate,
    load_paths,
    load_runtime_config,
    load_settings,
)
from common.config.secrets import read_secret
from common.execution import ExecutionRepository, check_mode_transition_safety
from common.logging import get_logger, setup_logging
from common.market_data.adapter import MarketFeedAdapter
from common.notifications import build_notifier
from common.persistence import Database, MigrationRunner
from common.process import legacy_system_status
from common.reconciliation import ReconciliationRunner
from common.retention import backup_database, run_retention, verify_backup_restorable
from common.utils.timeutils import local_date_in, now_ist

from .config_adapter import build_worker_config
from .supervisor import IntradayOptionsSupervisor, SupervisorConfig

_log = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_CREDENTIALS = 2
EXIT_RUNTIME_DISABLED = 3
EXIT_STRATEGY_NOT_FOUND = 4
#: Phase 8's "old-system exclusion" gate: the legacy Trading_Automation system
#: (LaunchAgent label or process) was detected active. See
#: common.process.legacy_guard for what is checked and why.
EXIT_LEGACY_SYSTEM_ACTIVE = 5
#: A shutdown signal ended this run (operator stop, or a future emergency
#: path) rather than the feed finishing on its own. The supervised launcher
#: (orchestration/process_control/supervised_launch.py) treats this as
#: terminal — spec section 12: "no restart loop after a deliberate safety
#: shutdown." Scope: the operator-stop path, and — since the Phase 10 feed
#: fix — the supervisor's own stuck-subscription shutdown
#: (SupervisorResult.stopped_by_stuck_subscription): a feed connected but
#: delivering nothing is not a crash to hot-loop-retry either, and an
#: immediate restart would most likely reproduce the same stuck state. The
#: daily-loss halt and kill switch (common.engine.daily_guard) latch per
#: worker inside the engine and do not end this supervisor run — see the
#: Phase 8 runbook entry for why that is a deliberately separate, later
#: decision.
EXIT_SAFETY_SHUTDOWN = 6


def _database_path(paths: ProjectPaths, runtime_cfg: RuntimeConfig, runtime_id: str) -> Path:
    """Where this runtime group's operational database lives.

    ``runtime_cfg.database`` (relative to the project root) when the runtime
    YAML sets one, else the conventional ``data/operational/<runtime_id>.db``
    — the same choice :func:`main` and :func:`build_supervisor` both need, so
    it lives here once rather than as two copies that could drift.
    """
    if runtime_cfg.database:
        return paths.project_root / runtime_cfg.database
    return paths.database_path(runtime_id)


def build_supervisor(
    *,
    runtime_id: str,
    config_root: Path,
    paths: ProjectPaths,
    adapter: MarketFeedAdapter,
    settings: Settings | None = None,
    trading_date: str | None = None,
    strategy_ids: frozenset[str] | None = None,
    live_broker_factory: Callable[[ResolvedConfig], Broker | None] | None = None,
    live_preflight_passed_for: Callable[[ResolvedConfig], bool] | None = None,
) -> IntradayOptionsSupervisor:
    """Discover this runtime's enabled strategies, admit them, build the group.

    Deliberately separable from :func:`main`'s CLI and credential handling: a
    caller that already has an adapter — a test, or a future second runtime
    reusing this same wiring — drives this directly instead of reimplementing
    discovery and admission. Assumes the caller has already checked that the
    runtime itself is enabled; :func:`main` does, before this is ever called.

    ``strategy_ids``, when given, admits only strategies whose id is in the
    set — the rest are discovered and skipped, never refused as blocked or
    reported as an error. This is ``scripts/start_strategy.py``'s only
    mechanism (Phase 7 Part 4): a per-strategy start still goes through a
    supervisor, exactly as an unfiltered start does — the spec is explicit
    that a bare worker is never spawned outside one.

    Phase 10 mode-transition safety (spec 366-373) runs here, before
    :meth:`~runtimes.intraday_options.supervisor.IntradayOptionsSupervisor
    .add_worker` — a strategy whose transition is unsafe is skipped
    entirely this session (no worker registered for it), logged loudly.
    ``live_broker_factory``/``live_preflight_passed_for`` are ``None`` by
    default for direct test/library callers: with no factory, a
    strategy with live history simply cannot prove a live -> paper/disabled
    transition safe and is refused, fail-closed — see
    ``common.execution.mode_transition``'s own docstring for why "cannot
    prove" and "proven unsafe" are treated identically.

    Discovery deliberately includes disabled configurations so a
    ``live -> disabled`` transition cannot bypass the exposure check. Disabled
    strategies are discarded only after that check and never spawn a worker.
    """
    settings = settings if settings is not None else load_settings()
    trading_date = trading_date or local_date_in(now_ist()).isoformat()
    runtime_cfg = load_runtime_config(config_root, runtime_id)
    database_path = _database_path(paths, runtime_cfg, runtime_id)

    supervisor = IntradayOptionsSupervisor(
        SupervisorConfig(
            runtime_id=runtime_id,
            database_path=database_path,
            lock_dir=paths.lock_root,
            pid_dir=paths.pid_root,
            log_dir=paths.log_root,
            heartbeat_interval_seconds=runtime_cfg.health.heartbeat_interval_seconds,
        ),
        adapter,
        # Real Telegram when settings carry credentials, else NullNotifier —
        # settings is already resolved above, so there is exactly one place
        # in this function that decides it. Group-level events (this
        # supervisor's own) are sent from here; each spawned worker below
        # decides independently, from its own environment, because spawn
        # cannot hand it this object — see NOTIFIER_FROM_SETTINGS's docstring.
        notifier=build_notifier(settings),
    )

    transition_database = Database(database_path)
    try:
        MigrationRunner(transition_database).run_pending()
        repository = ExecutionRepository(transition_database)
        reconciliation_runner = ReconciliationRunner(transition_database)

        for cfg in discover_strategies(config_root, runtime_id, settings=settings):
            if strategy_ids is not None and cfg.strategy.strategy_id not in strategy_ids:
                continue

            broker_for_check = (
                live_broker_factory(cfg) if live_broker_factory is not None else None
            )
            try:
                transition_decision = check_mode_transition_safety(
                    repository,
                    strategy_id=cfg.strategy.strategy_id,
                    runtime_id=runtime_id,
                    new_mode=cfg.strategy.mode if cfg.strategy.enabled else None,
                    broker=broker_for_check,
                    reconciliation_runner=(
                        reconciliation_runner if broker_for_check is not None else None
                    ),
                )
            finally:
                close_broker = getattr(broker_for_check, "close", None)
                if callable(close_broker):
                    close_broker()
            if not transition_decision.allowed:
                _log.error(
                    "refusing to admit strategy_id=%s this session: mode transition to "
                    "%s is unsafe: %s",
                    cfg.strategy.strategy_id,
                    cfg.strategy.mode.value,
                    transition_decision.reason,
                )
                continue

            if not cfg.strategy.enabled:
                # Disabled strategies start no worker, but disabling must not
                # bypass the live-to-disabled exposure check above.
                continue

            # Do not spend authentication/IP/order-book reads for paper or for
            # a live strategy already blocked by a static permission flag.  If
            # every non-preflight gate is open, however, the production
            # callback is mandatory and its result is the final admission fact.
            needs_live_preflight = (
                cfg.strategy.mode.value == "live"
                and effective_live_gate(cfg, preflight_passed=True).allowed
            )
            live_preflight_passed = (
                live_preflight_passed_for(cfg)
                if needs_live_preflight and live_preflight_passed_for is not None
                else False
            )
            worker_config = build_worker_config(
                cfg,
                database_path=database_path,
                lock_dir=paths.lock_root,
                pid_dir=paths.pid_root,
                log_dir=paths.log_root,
                trading_date=trading_date,
                live_preflight_passed=live_preflight_passed,
                account_shared_database_path=paths.account_shared_database_path,
                token_cache_dir=paths.cache_root,
            )
            # Admission and the child receive the same preflight fact.  Omitting
            # it here used to make a successful preflight irrelevant because
            # ``effective_live_gate`` silently re-applied its fail-closed default.
            supervisor.add_worker(
                worker_config,
                tick_channel=worker_config.engine is not None,
                live_gate=effective_live_gate(
                    cfg, preflight_passed=live_preflight_passed
                ),
            )
    finally:
        transition_database.close()

    return supervisor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-root", type=Path, default=Path("config"), help="Root of the config/ tree."
    )
    parser.add_argument(
        "--runtime-id", default="intraday_options", help="Which runtime group to start."
    )
    parser.add_argument(
        "--strategy-id",
        default=None,
        help=(
            "Start only this one strategy — still through this same supervisor "
            "(scripts/start_strategy.py's only mechanism; a bare worker is never "
            "spawned outside one). Omit to start every enabled strategy, as before."
        ),
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    paths = load_paths(settings=settings)
    paths.ensure_writable_dirs()
    redactor = setup_logging(
        level=settings.algo_log_level, log_dir=paths.log_root, settings=settings
    )

    runtime_cfg = load_runtime_config(args.config_root, args.runtime_id)
    if not runtime_cfg.enabled:
        print(
            f"runtimes/{args.runtime_id}.yaml has enabled: false — nothing to start. "
            "Set it to true when this group is ready to run."
        )
        return EXIT_RUNTIME_DISABLED

    # Checked before anything that touches disk or the network: spec section
    # 12/16's "do not start legacy and new trading systems together," and
    # Phase 8's own "old-system exclusion" gate test. Fail-closed — a legacy
    # system that cannot be determined either way is not "not detected".
    legacy_status = legacy_system_status()
    if legacy_status.active:
        if legacy_status.undetermined:
            print(
                "Refusing to start: the legacy Trading_Automation system's state could "
                f"not be determined ({legacy_status.describe()}). A check that cannot be "
                "verified is treated as active, not as absent. Resolve why launchctl "
                "could not be queried, then retry."
            )
        else:
            print(
                "Refusing to start: the legacy Trading_Automation system appears to be "
                f"active ({legacy_status.describe()}). The old and new systems must never "
                f"run together. Unload the legacy LaunchAgent first:\n"
                "  launchctl bootout gui/$(id -u)/com.soundarraj.tradingautomation.starttrading"
            )
        return EXIT_LEGACY_SYSTEM_ACTIVE

    if args.strategy_id is not None:
        # Checked before authenticating: a typo'd strategy id should not cost
        # a Dhan auth request against the ~1-per-2-minute limit.
        enabled = discover_enabled_strategies(args.config_root, args.runtime_id, settings=settings)
        enabled_ids = {cfg.strategy.strategy_id for cfg in enabled}
        if args.strategy_id not in enabled_ids:
            print(
                f"{args.strategy_id!r} is not an enabled strategy under "
                f"runtimes/{args.runtime_id}.yaml (enabled: {sorted(enabled_ids) or ['none']})."
            )
            return EXIT_STRATEGY_NOT_FOUND

    # Backup, migrate, retain — in that order, once, here. This is the
    # controlled-startup entry point (spec section 12): never a cron, never a
    # thread on the trading path, and strictly before authentication or any
    # worker exists, so storage housekeeping cannot race a trading write.
    # build_supervisor() below re-opens this same database and re-runs
    # migration (a no-op replay — see MigrationRunner._apply_one) because it
    # is also called directly by tests without going through main(); doing
    # the backup and the retention sweep only here, rather than there too,
    # is what keeps them at exactly one call site.
    database_path = _database_path(paths, runtime_cfg, args.runtime_id)
    database = Database(database_path)
    backup_path = backup_database(
        database.path,
        paths.backup_root,
        retain_count=runtime_cfg.retention.backup_retain_count,
    )
    if backup_path is not None:
        verify_backup_restorable(backup_path)
    MigrationRunner(database).run_pending(
        require_fresh_backup_for_destructive=backup_path is not None
    )
    run_retention(
        database=database,
        log_dir=paths.log_root,
        cache_dir=paths.cache_root,
        log_max_age_days=runtime_cfg.retention.log_max_age_days,
        log_compress_after_days=runtime_cfg.retention.log_compress_after_days,
        db_row_max_age_days=runtime_cfg.retention.db_row_max_age_days,
        db_delete_batch_limit=runtime_cfg.retention.db_delete_batch_limit,
        scrip_cache_retain_count=runtime_cfg.retention.scrip_cache_retain_count,
        # Phase 8: the same directory orchestration/launchd/generate_plists.py
        # points every plist's StandardOutPath/StandardErrorPath at. launchd
        # never rotates these itself; rotate_launchd_logs (inside
        # run_retention) is what makes them eligible for the sweep above.
        launchd_log_dir=paths.log_root / "launchd",
    )
    database.close()

    client_id = read_secret(settings.dhan_client_id)
    if not client_id:
        print("DHAN_CLIENT_ID is not set. Fill it in .env (see .env.example).")
        return EXIT_NO_CREDENTIALS

    bootstrap = AuthBootstrap(
        AuthCredentials(
            client_id=client_id,
            pin=read_secret(settings.dhan_pin),
            totp_secret=read_secret(settings.dhan_totp_secret),
            access_token=read_secret(settings.dhan_access_token),
        ),
        cache_dir=paths.cache_root,
        on_token_minted=lambda token: redactor.add_secrets([token]),
    )
    try:
        token, outcome = bootstrap.get_token()
    except AuthError as exc:
        print(f"Cannot authenticate ({type(exc).__name__}): {exc}")
        return EXIT_FAILED
    _log.info("authenticated source=%s", outcome.source)

    # Imported here, not at module level, so this module can be read and
    # linted without the Dhan SDK present — the same discipline
    # scripts/capture_live_tape.py uses for the same reason.
    from common.market_data.dhan import DhanMarketFeedAdapter, build_dhan_order_client

    from .live_runtime import (
        account_shared_strategy_has_live_history,
        build_transition_reconciliation_broker,
        parent_live_preflight_passed,
    )

    adapter = DhanMarketFeedAdapter(client_id=client_id, access_token=token)
    order_client = build_dhan_order_client(client_id=client_id, access_token=token)

    account_identity_pepper = read_secret(settings.account_identity_pepper)
    transition_broker_factory = (
        (
            lambda cfg: (
                build_transition_reconciliation_broker(
                    cfg,
                    settings=settings,
                    client_id=client_id,
                    account_database_path=paths.account_shared_database_path,
                    order_client=order_client,
                )
                if account_shared_strategy_has_live_history(
                    account_database_path=paths.account_shared_database_path,
                    client_id=client_id,
                    account_identity_pepper=account_identity_pepper,
                    strategy_id=cfg.strategy.strategy_id,
                )
                else None
            )
        )
        if account_identity_pepper
        else None
    )

    supervisor = build_supervisor(
        runtime_id=args.runtime_id,
        config_root=args.config_root,
        paths=paths,
        adapter=adapter,
        settings=settings,
        strategy_ids=frozenset({args.strategy_id}) if args.strategy_id else None,
        live_preflight_passed_for=lambda cfg: parent_live_preflight_passed(
            cfg,
            settings=settings,
            client_id=client_id,
            access_token=token,
            account_database_path=paths.account_shared_database_path,
            order_client=order_client,
        ),
        live_broker_factory=transition_broker_factory,
    )
    # Recorded here rather than inside AuthBootstrap: get_token() ran before
    # this runtime's database (and therefore auth_events) existed — see
    # IntradayOptionsSupervisor.set_startup_auth_outcome's docstring. A
    # pre-database auth *failure* is not persisted; it stays print()+log only.
    supervisor.set_startup_auth_outcome(
        source=outcome.source,
        token_expiry=outcome.expiry_time,
        requests_made=outcome.requests_made,
    )
    result = supervisor.run()
    _log.info(
        "supervisor run finished workers_started=%d candles_published=%d",
        result.workers_started,
        result.candles_published,
    )
    # A signal (operator stop) ended this run deliberately, as opposed to the
    # feed finishing on its own — the supervised launcher must not treat this
    # as a crash to retry. See EXIT_SAFETY_SHUTDOWN's own docstring for scope.
    #
    # A stuck subscription (the feed connected but delivered nothing long
    # enough that a worker's request could never be applied) is the same kind
    # of deliberate, non-crash shutdown: the supervisor chose to end the run
    # rather than keep presenting an ambiguously healthy RUNNING_PAPER. See
    # SupervisorResult.stopped_by_stuck_subscription.
    if result.stopped_by_signal or result.stopped_by_stuck_subscription:
        return EXIT_SAFETY_SHUTDOWN
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
