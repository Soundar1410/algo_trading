"""Entrypoint for the ``positional_options`` runtime.

    .venv/bin/python -m runtimes.positional_options [--config-root config]

Mirrors ``runtimes.intraday_options.__main__``'s startup order exactly —
legacy-system guard, backup/migrate/retain, auth bootstrap, real feed — with
one structural difference: no child process is spawned. This module
discovers the one enabled strategy, builds the real Dhan feed adapter, chain
fetcher and scrip master for its underlying, then hands everything to
:func:`runtimes.positional_options.supervisor.run_supervisor`, which runs
that strategy's engine on this same process.

Every committed strategy under this runtime ships ``mode: paper``, and
``config/runtimes/positional_options.yaml`` ships ``enabled: false`` — see
CLAUDE.md. This entrypoint refuses to start while either gate is closed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common.authentication import AuthBootstrap, AuthCredentials, AuthError
from common.config import (
    ConfigError,
    ProjectPaths,
    ResolvedConfig,
    RuntimeConfig,
    load_paths,
    load_runtime_config,
    load_settings,
)
from common.config.secrets import read_secret
from common.logging import get_logger, setup_logging
from common.market_data.dhan_option_chain import build_dhan_chain_fetcher
from common.market_data.scrip_master import ScripMaster, ScripMasterCache, resolve_index_meta
from common.notifications import build_notifier
from common.persistence import Database, MigrationRunner
from common.process import legacy_system_status
from common.retention import backup_database, run_retention, verify_backup_restorable
from common.utils.timeutils import local_date_in, now_ist

from .supervisor import (
    EXIT_ALREADY_RUNNING,
    EXIT_FAILED,
    EXIT_MODE_TRANSITION_UNSAFE,
    EXIT_OK,
    SupervisorOutcome,
    run_supervisor,
    select_one_enabled_strategy,
)

_log = get_logger(__name__)

EXIT_RUNTIME_DISABLED = 10
EXIT_NO_CREDENTIALS = 11
#: Phase 8's "old-system exclusion" gate — see
#: runtimes.intraday_options.__main__.EXIT_LEGACY_SYSTEM_ACTIVE for the
#: fail-closed reasoning this mirrors exactly.
EXIT_LEGACY_SYSTEM_ACTIVE = 12
EXIT_TOO_MANY_STRATEGIES = 13


def _database_path(paths: ProjectPaths, runtime_cfg: RuntimeConfig, runtime_id: str) -> Path:
    if runtime_cfg.database:
        return paths.project_root / runtime_cfg.database
    return paths.database_path(runtime_id)


def _resolve_underlying_meta(cfg: ResolvedConfig):  # type: ignore[no-untyped-def]
    parameters = cfg.strategy.parameters
    underlying = str(parameters["underlying"])
    meta = resolve_index_meta(
        underlying,
        index_security_id=str(parameters.get("index_security_id") or "") or None,
        index_segment=str(parameters.get("index_segment") or "") or None,
        fno_segment=str(parameters.get("fno_segment") or "") or None,
    )
    return underlying, meta


def _build_scrip_master(cfg: ResolvedConfig, *, default_cache_dir: Path) -> ScripMaster:
    underlying, meta = _resolve_underlying_meta(cfg)
    cache_dir_raw = cfg.strategy.parameters.get("scrip_master_cache_dir")
    cache_dir = Path(cache_dir_raw) if cache_dir_raw else default_cache_dir
    return ScripMaster(underlying, exchange=meta.exchange).load(cache=ScripMasterCache(cache_dir))


def _build_feed(cfg: ResolvedConfig, adapter):  # type: ignore[no-untyped-def]
    from common.engine.adapter_feed import AdapterFeed
    from common.engine.feed import SubscriptionMode
    from common.market_data.scrip_master import segment_code

    _underlying, meta = _resolve_underlying_meta(cfg)
    underlying_id = str(meta.security_id)
    underlying_segment = segment_code(meta.segment)
    option_segment = segment_code(meta.fno_segment)

    def _segment_for(security_id: str) -> int | None:
        return underlying_segment if security_id == underlying_id else option_segment

    def _mode_for(security_id: str) -> int | None:
        return None if security_id == underlying_id else int(SubscriptionMode.FULL)

    return AdapterFeed(adapter, segment_for=_segment_for, mode_for=_mode_for)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-root", type=Path, default=Path("config"), help="Root of the config/ tree."
    )
    parser.add_argument(
        "--runtime-id", default="positional_options", help="Which runtime group to start."
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
            "Set it to true only after the paper evaluation and approval CLAUDE.md "
            "requires are both complete."
        )
        return EXIT_RUNTIME_DISABLED

    # Checked before anything that touches disk or the network — identical
    # gate and reasoning to runtimes.intraday_options.__main__.main.
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

    # Which strategy (if any) is admitted has to be known before the feed or
    # scrip master can be built for its real underlying — checked before
    # authentication too, so a misconfigured/absent strategy costs no Dhan
    # auth request.
    try:
        cfg = select_one_enabled_strategy(args.config_root, args.runtime_id, settings=settings)
    except ConfigError as exc:
        print(str(exc))
        return EXIT_TOO_MANY_STRATEGIES
    if cfg is None:
        print(f"No enabled strategy under runtimes/{args.runtime_id}.yaml; nothing to start.")
        return EXIT_OK

    # Backup, migrate, retain — once, here, strictly before authentication or
    # any worker exists — identical discipline to the intraday entrypoint.
    # supervisor.run_supervisor() re-opens this same database and re-runs
    # migration (a no-op replay) because it is also called directly by tests.
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
    # runtimes.intraday_options.__main__ uses for the same reason.
    from common.market_data.dhan import DhanMarketFeedAdapter

    adapter = DhanMarketFeedAdapter(client_id=client_id, access_token=token)
    chain_fetcher = build_dhan_chain_fetcher(client_id=client_id, access_token=token)
    scrip_master = _build_scrip_master(cfg, default_cache_dir=paths.cache_root)
    feed = _build_feed(cfg, adapter)

    supervisor_outcome = run_supervisor(
        cfg=cfg,
        runtime_id=args.runtime_id,
        config_root=args.config_root,
        paths=paths,
        feed=feed,
        chain_fetcher=chain_fetcher,
        scrip_master=scrip_master,
        notifier=build_notifier(settings),
        trading_date=local_date_in(now_ist()).isoformat(),
    )
    return _exit_code_for(supervisor_outcome)


def _exit_code_for(outcome: SupervisorOutcome) -> int:
    if outcome.exit_code == EXIT_OK:
        return EXIT_OK
    if outcome.exit_code == EXIT_MODE_TRANSITION_UNSAFE:
        print(f"Refusing to start {outcome.strategy_id}: {outcome.error}")
        return EXIT_FAILED
    if outcome.exit_code == EXIT_ALREADY_RUNNING:
        print(outcome.error)
        return EXIT_FAILED
    print(outcome.error or "positional_options runtime failed")
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
