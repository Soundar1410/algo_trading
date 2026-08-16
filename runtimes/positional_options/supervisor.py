"""Composition root for the ``positional_options`` runtime.

**Single-process, single-strategy, by design** — see ``worker.py``'s own
module docstring for the full reasoning. This module is the positional
counterpart of ``runtimes.intraday_options.__main__.build_supervisor``,
without the child-process fan-out: it discovers this runtime's enabled
strategy, refuses anything unsafe exactly as the intraday build does, and
then builds and runs one worker directly, in this process, under one
exclusive lock.

Production network construction (the Dhan feed adapter, the chain fetcher,
the scrip master download) happens in ``__main__.py``, never here — every
network-facing object this module needs is a parameter, so a test drives it
with a canned adapter/fetcher/master and never touches the network,
mirroring ``worker.build_engine``'s own test-facing seam.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from common.config import (
    ConfigError,
    ProjectPaths,
    ResolvedConfig,
    RuntimeConfig,
    Settings,
    discover_enabled_strategies,
    load_runtime_config,
)
from common.config.models import ExecutionMode
from common.engine.feed import MarketDataFeed
from common.execution import ExecutionRepository, check_mode_transition_safety
from common.logging import get_logger
from common.margin import MarginEstimator
from common.market_data.option_chain import ChainFetcher
from common.market_data.scrip_master import ScripMaster
from common.notifications import Notifier
from common.persistence import Database, MigrationRunner
from common.process.locks import DuplicateProcessError, worker_lock
from common.utils.timeutils import local_date_in, now_ist

from .config_adapter import build_worker_config
from .worker import WorkerOutcome, run_worker

log = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_STRATEGY = 2
EXIT_TOO_MANY_STRATEGIES = 3
EXIT_MODE_TRANSITION_UNSAFE = 4
EXIT_ALREADY_RUNNING = 5


@dataclass
class SupervisorOutcome:
    exit_code: int
    strategy_id: str | None = None
    worker: WorkerOutcome | None = None
    error: str | None = None


def _database_path(paths: ProjectPaths, runtime_cfg: RuntimeConfig, runtime_id: str) -> Path:
    if runtime_cfg.database:
        return paths.project_root / runtime_cfg.database
    return paths.database_path(runtime_id)


def select_one_enabled_strategy(
    config_root: Path, runtime_id: str, *, settings: Settings
) -> ResolvedConfig | None:
    """Exactly one, or a fail-closed refusal — never "run the first one".

    This runtime drives one ``PositionalMultiLegEngine`` on the process's own
    main thread; nothing here fans a second strategy out to its own thread or
    process. CLAUDE.md and the spec restrict this runtime to exactly one
    approved strategy today (``weekly_delta_neutral``), so a second *enabled*
    strategy config is not "start both" or "start the first" — it is a
    configuration this runtime cannot safely admit without the real
    multi-process split ``worker.py``'s own docstring already names as the
    future path, so it is refused outright rather than silently picking one.
    """
    enabled = discover_enabled_strategies(config_root, runtime_id, settings=settings)
    if not enabled:
        return None
    if len(enabled) > 1:
        raise ConfigError(
            f"{len(enabled)} strategies are enabled under runtimes/{runtime_id}.yaml "
            f"({', '.join(sorted(c.strategy.strategy_id for c in enabled))}), but this "
            "runtime drives exactly one strategy per process today — see "
            "runtimes/positional_options/worker.py's own module docstring. Disable all "
            "but one before starting."
        )
    return enabled[0]


def run_supervisor(
    *,
    cfg: ResolvedConfig,
    runtime_id: str,
    config_root: Path,
    paths: ProjectPaths,
    feed: MarketDataFeed,
    chain_fetcher: ChainFetcher,
    scrip_master: ScripMaster,
    margin_estimator: MarginEstimator,
    notifier: Notifier | None = None,
    trading_date: str | None = None,
) -> SupervisorOutcome:
    """Admit ``cfg`` — already the one enabled strategy a caller resolved via
    :func:`select_one_enabled_strategy` — and run its worker.

    ``cfg`` is a parameter, not discovered here, because building ``feed``
    and ``scrip_master`` for the *real* underlying/segment already requires
    knowing which strategy was admitted; asking this function to discover
    again internally would mean discovering twice (or handing it network
    construction callables, which is worse). :func:`select_one_enabled_strategy`
    stays the single source of truth for "which strategy, or none, or too
    many" — this function assumes that question is already answered.
    """
    trading_date = trading_date or local_date_in(now_ist()).isoformat()
    runtime_cfg = load_runtime_config(config_root, runtime_id)
    database_path = _database_path(paths, runtime_cfg, runtime_id)

    strategy_id = cfg.strategy.strategy_id
    database = Database(database_path)
    # A no-op replay when __main__.main() already migrated this database —
    # mirrors intraday's own build_supervisor, which re-opens and re-runs
    # migration for exactly the same reason: a direct/test caller of this
    # function never went through main() at all.
    MigrationRunner(database).run_pending()
    repository = ExecutionRepository(database)
    try:
        # Defense in depth, exactly as intraday's build_supervisor runs this
        # before admitting a worker: live is structurally unreachable for
        # this runtime (build_worker_config raises on mode: live before any
        # worker exists, and no live broker class is ever constructed here),
        # so there is no broker to check broker-confirmed exposure against —
        # only a hand-edited database could ever make this fail.
        decision = check_mode_transition_safety(
            repository,
            strategy_id=strategy_id,
            runtime_id=runtime_id,
            new_mode=cfg.strategy.mode,
            broker=None,
            reconciliation_runner=None,
        )
        if not decision.allowed:
            log.error(
                "refusing to admit strategy_id=%s this session: mode transition unsafe: %s",
                strategy_id,
                decision.reason,
            )
            return SupervisorOutcome(
                exit_code=EXIT_MODE_TRANSITION_UNSAFE,
                strategy_id=strategy_id,
                error=decision.reason,
            )

        worker_config = build_worker_config(cfg, trading_date=trading_date)

        lock = worker_lock(
            runtime_id=runtime_id,
            strategy_id=strategy_id,
            lock_dir=paths.lock_root,
            pid_dir=paths.pid_root,
        )
        try:
            lock.acquire()
        except DuplicateProcessError as exc:
            log.error("%s", exc)
            return SupervisorOutcome(
                exit_code=EXIT_ALREADY_RUNNING, strategy_id=strategy_id, error=str(exc)
            )
        try:
            session = repository.open_session(
                runtime_id=runtime_id,
                strategy_id=strategy_id,
                execution_mode=ExecutionMode.PAPER,
                process_role="worker",
                pid=os.getpid(),
            )
            outcome = run_worker(
                worker_config,
                repository=repository,
                session_id=session.id,
                feed=feed,
                chain_fetcher=chain_fetcher,
                scrip_master=scrip_master,
                margin_estimator=margin_estimator,
                runtime_root=paths.runtime_root,
                notifier=notifier,
            )
            return SupervisorOutcome(
                exit_code=EXIT_OK if outcome.exit_code == 0 else EXIT_FAILED,
                strategy_id=strategy_id,
                worker=outcome,
                error=outcome.error,
            )
        finally:
            lock.release()
    finally:
        database.close()
