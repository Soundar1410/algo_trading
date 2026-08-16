"""Entrypoint for the ``positional_options`` runtime.

    .venv/bin/python -m runtimes.positional_options [--config-root config] [--strategy-id ID]

Mirrors ``runtimes.intraday_options.__main__``'s startup order exactly —
legacy-system guard, backup/migrate/retain, auth bootstrap, real feed —
and, since Phase 5 (runtime generalization), its child-process fan-out too:
this module builds the **one shared** Dhan feed adapter, discovers every
enabled strategy under this runtime, and hands the whole set to
:func:`runtimes.positional_options.supervisor.build_positional_supervisor`,
which spawns one child process per strategy under one shared
:class:`~common.feed.hub.SharedFeedHub` — never one Dhan WebSocket per
strategy. A single enabled strategy (the only case this runtime has ever
actually run) is simply the N=1 case of that same machinery, not a
different code path.

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
    discover_enabled_strategies,
    load_paths,
    load_runtime_config,
    load_settings,
)
from common.config.models import RuntimeConfig
from common.config.paths import ProjectPaths
from common.config.secrets import read_secret
from common.logging import get_logger, setup_logging
from common.notifications import build_notifier
from common.persistence import Database, MigrationRunner
from common.process import legacy_system_status
from common.retention import backup_database, run_retention, verify_backup_restorable

from .supervisor import EXIT_OK, build_positional_supervisor

_log = get_logger(__name__)

EXIT_FAILED = 1
EXIT_RUNTIME_DISABLED = 10
EXIT_NO_CREDENTIALS = 11
#: Phase 8's "old-system exclusion" gate — see
#: runtimes.intraday_options.__main__.EXIT_LEGACY_SYSTEM_ACTIVE for the
#: fail-closed reasoning this mirrors exactly.
EXIT_LEGACY_SYSTEM_ACTIVE = 12
EXIT_STRATEGY_NOT_FOUND = 13


def _database_path(paths: ProjectPaths, runtime_cfg: RuntimeConfig, runtime_id: str) -> Path:
    if runtime_cfg.database:
        return paths.project_root / runtime_cfg.database
    return paths.database_path(runtime_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-root", type=Path, default=Path("config"), help="Root of the config/ tree."
    )
    parser.add_argument(
        "--runtime-id", default="positional_options", help="Which runtime group to start."
    )
    parser.add_argument(
        "--strategy-id",
        default=None,
        help=(
            "Start only this one strategy — still through this same supervisor "
            "(scripts/start_strategy.py's only mechanism; a bare worker is never "
            "spawned outside one). Omit to start every enabled strategy."
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

    if args.strategy_id is not None:
        # Checked before authenticating: a typo'd strategy id should not
        # cost a Dhan auth request against the ~1-per-2-minute limit.
        enabled = discover_enabled_strategies(args.config_root, args.runtime_id, settings=settings)
        enabled_ids = {cfg.strategy.strategy_id for cfg in enabled}
        if args.strategy_id not in enabled_ids:
            print(
                f"{args.strategy_id!r} is not an enabled strategy under "
                f"runtimes/{args.runtime_id}.yaml (enabled: {sorted(enabled_ids) or ['none']})."
            )
            return EXIT_STRATEGY_NOT_FOUND

    # Backup, migrate, retain — once, here, strictly before authentication
    # or any worker exists. build_positional_supervisor() re-opens this
    # same database and re-runs migration (a no-op replay) because it is
    # also called directly by tests without going through main().
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

    # Authenticated exactly once, here, in the parent — every spawned child
    # reads this same cached token from disk (worker._cached_dhan_token),
    # never re-authenticating over the network itself.
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
    redactor.add_secrets([token])
    _log.info("authenticated source=%s", outcome.source)

    # Imported here, not at module level, so this module can be read and
    # linted without the Dhan SDK present — the same discipline
    # runtimes.intraday_options.__main__ uses for the same reason.
    from common.market_data.dhan import DhanMarketFeedAdapter

    # One shared adapter for the whole group — never one per strategy. Each
    # spawned child builds its own chain fetcher/margin fetcher/scrip
    # master independently (worker.run_positional_worker_process's own
    # docstring); only this one feed connection is shared.
    adapter = DhanMarketFeedAdapter(client_id=client_id, access_token=token)

    try:
        supervisor = build_positional_supervisor(
            runtime_id=args.runtime_id,
            config_root=args.config_root,
            paths=paths,
            adapter=adapter,
            settings=settings,
            strategy_ids=frozenset({args.strategy_id}) if args.strategy_id else None,
            notifier=build_notifier(settings),
        )
    except ConfigError as exc:
        print(str(exc))
        return EXIT_FAILED

    result = supervisor.run()
    _log.info(
        "supervisor run finished workers_started=%d ticks_received=%d",
        result.workers_started,
        result.ticks_received,
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
