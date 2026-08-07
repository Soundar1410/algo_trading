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

Requires real Dhan credentials in ``.env`` (see ``.env.example``). Paper mode
only: see :mod:`common.broker.factory` for why a live-mode strategy can never
obtain a broker yet, live order placement is Phase 10.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common.authentication import AuthBootstrap, AuthCredentials, AuthError
from common.config import (
    ProjectPaths,
    Settings,
    discover_enabled_strategies,
    effective_live_gate,
    load_paths,
    load_runtime_config,
    load_settings,
)
from common.config.secrets import read_secret
from common.logging import get_logger, setup_logging
from common.market_data.adapter import MarketFeedAdapter
from common.notifications import build_notifier
from common.utils.timeutils import local_date_in, now_ist

from .config_adapter import build_worker_config
from .supervisor import IntradayOptionsSupervisor, SupervisorConfig

_log = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_CREDENTIALS = 2
EXIT_RUNTIME_DISABLED = 3
EXIT_STRATEGY_NOT_FOUND = 4


def build_supervisor(
    *,
    runtime_id: str,
    config_root: Path,
    paths: ProjectPaths,
    adapter: MarketFeedAdapter,
    settings: Settings | None = None,
    trading_date: str | None = None,
    strategy_ids: frozenset[str] | None = None,
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
    """
    settings = settings if settings is not None else load_settings()
    trading_date = trading_date or local_date_in(now_ist()).isoformat()
    runtime_cfg = load_runtime_config(config_root, runtime_id)
    database_path = (
        paths.project_root / runtime_cfg.database
        if runtime_cfg.database
        else paths.database_path(runtime_id)
    )

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

    for cfg in discover_enabled_strategies(config_root, runtime_id, settings=settings):
        if strategy_ids is not None and cfg.strategy.strategy_id not in strategy_ids:
            continue
        worker_config = build_worker_config(
            cfg,
            database_path=database_path,
            lock_dir=paths.lock_root,
            pid_dir=paths.pid_root,
            log_dir=paths.log_root,
            trading_date=trading_date,
        )
        # Cheap and harmless to compute for a paper strategy too — add_worker
        # only ever consults it for a live-mode worker.
        supervisor.add_worker(worker_config, live_gate=effective_live_gate(cfg))

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
    redactor = setup_logging(log_dir=paths.log_root, settings=settings)

    runtime_cfg = load_runtime_config(args.config_root, args.runtime_id)
    if not runtime_cfg.enabled:
        print(
            f"runtimes/{args.runtime_id}.yaml has enabled: false — nothing to start. "
            "Set it to true when this group is ready to run."
        )
        return EXIT_RUNTIME_DISABLED

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
    from common.market_data.dhan import DhanMarketFeedAdapter

    adapter = DhanMarketFeedAdapter(client_id=client_id, access_token=token)

    supervisor = build_supervisor(
        runtime_id=args.runtime_id,
        config_root=args.config_root,
        paths=paths,
        adapter=adapter,
        settings=settings,
        strategy_ids=frozenset({args.strategy_id}) if args.strategy_id else None,
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
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
