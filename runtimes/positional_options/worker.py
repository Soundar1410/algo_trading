"""The ``positional_options`` runtime worker — builds and runs one
:class:`~common.engine.positional.positional_engine.PositionalMultiLegEngine`.

**One child process per strategy, one shared feed hub for the group**
(Phase 5, runtime generalization). :func:`build_engine`/:func:`run_worker`
are the single-worker body — completely unaware of whether they are being
called in-process (every existing test; the original single-strategy
posture this runtime shipped with) or inside a spawned child under
:class:`~runtimes.positional_options.supervisor.PositionalOptionsSupervisor`
(:func:`run_positional_worker_process`, this module's own multiprocessing
entry point, added in Phase 5). Neither function's signature changed for
this — a worker is, and always was, "run one engine to completion"; N
workers under a shared hub is simply N calls to the same thing, each in its
own process, exactly mirroring ``runtimes.intraday_options.worker.
run_worker_process``'s own relationship to ``run_worker``/``build_engine``
there. See ``supervisor.py``'s own module docstring for the group-level
shape (shared adapter, one ``SharedFeedHub``, one control queue per
worker).

Mirrors the intraday worker's own recovery/shutdown discipline throughout:
migration + integrity check before any trading, restart reconciliation
before any new entry, and a lock that is never held past this function's
return.
"""

from __future__ import annotations

import enum
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any

from common.authentication.bootstrap import TOKEN_CACHE_FILENAME
from common.authentication.token_cache import TokenCache
from common.broker import PaperBroker
from common.broker.quotes import QuoteBook
from common.config import load_settings
from common.config.models import ExecutionMode
from common.config.secrets import read_secret
from common.engine.feed import MarketDataFeed, SubscriptionMode
from common.engine.gateway import LifecycleGateway
from common.engine.hub_feed import HubTickFeed
from common.engine.positional.lifecycle import PositionalLifecyclePolicy
from common.engine.positional.positional_engine import PositionalMultiLegEngine
from common.engine.positional.positional_models import Cycle
from common.engine.positional.positional_state import CycleRowInconsistent
from common.engine.positional.positional_state import load_cycle as _load_open_cycle
from common.engine.positions import PositionManager
from common.engine.reporting_bindings import HeartbeatEngineReporter
from common.engine.selection import DhanOptionChainResolver
from common.engine.session import MarketSession
from common.execution import ExecutionRepository, OrderLifecycle
from common.feed.queues import FeedGapNotice, TickDropNotice
from common.greeks import GreeksService, ModelAssumptions
from common.health import HealthState, HeartbeatWriter
from common.logging import get_logger, setup_logging
from common.margin import MarginEstimate, MarginEstimator
from common.market_data.dhan_margin import build_dhan_margin_fetcher
from common.market_data.dhan_option_chain import build_dhan_chain_fetcher
from common.market_data.option_chain import ChainFetcher, OptionChainService
from common.market_data.scrip_master import ScripMaster, ScripMasterCache, resolve_index_meta
from common.market_data.scrip_master import segment_code as _segment_code
from common.notifications import (
    NotificationEvent,
    Notifier,
    NullNotifier,
    SafeNotifier,
    build_notifier,
)
from common.persistence import Database, MigrationRunner
from common.process import DuplicateProcessError, worker_lock
from common.process.signals import shutdown_signals
from common.process.square_off_requests import (
    clear_square_off_request,
    read_square_off_request,
    square_off_request_path,
)
from common.utils.timeutils import now_ist

from .config_adapter import WorkerConfig
from .positional_multi_leg_engine_worker import load_positional_strategy, recover_cycle

#: Exit code used when another process already owns this strategy's worker
#: lock — distinct from 1 so the supervisor (or a test) can tell "refused as
#: duplicate" from "crashed". Mirrors ``runtimes.intraday_options.worker.
#: EXIT_DUPLICATE`` exactly.
EXIT_DUPLICATE = 3

log = get_logger(__name__)


class _NotifierSentinel(enum.Enum):
    """Sentinel for ``notifier=`` on :func:`run_positional_worker_process`:
    build the production notifier here, in the spawned child, from a
    freshly-loaded :class:`~common.config.Settings`, rather than passing an
    already-built one across the ``spawn`` boundary. An ``Enum``, not a
    plain sentinel object, for the identical pickling reason ``runtimes.
    intraday_options.worker._NotifierSentinel``'s own docstring gives in
    full: a plain ``object()`` does not survive an unpickle with its
    identity intact, and an un-recognised value falling through to being
    used *as* a notifier fails the moment anything calls ``.send()`` on it.
    """

    FROM_SETTINGS = enum.auto()


NOTIFIER_FROM_SETTINGS = _NotifierSentinel.FROM_SETTINGS


class _NoDhanCredentials(RuntimeError):
    """No cached Dhan token was available to build the real chain/margin
    fetchers. Never raised when a factory override is set — see
    :class:`~runtimes.positional_options.config_adapter.WorkerConfig`'s own
    ``chain_fetcher_factory``/``margin_fetcher_factory`` docstring."""


def _resolve_factory(ref: str) -> Callable[[], Any]:
    """Import ``"module.submodule:function_name"`` and return the function
    itself — never a live object pickled across the process boundary, only
    this string. Mirrors ``positional_multi_leg_engine_worker.
    load_positional_strategy``'s own dotted-path resolution exactly, for
    the identical picklability reason."""
    module_name, separator, attr_name = ref.partition(":")
    if not separator or not module_name or not attr_name:
        raise ValueError(f"factory ref must be 'module:function', got {ref!r}")
    module = import_module(module_name)
    try:
        factory = getattr(module, attr_name)
    except AttributeError as exc:
        raise ValueError(f"{module_name!r} has no attribute {attr_name!r}") from exc
    if not callable(factory):
        raise ValueError(f"{ref!r} does not resolve to a callable")
    return factory  # type: ignore[no-any-return]


def _cached_dhan_token(settings: Any, cache_dir: Path, client_id: str) -> str | None:
    """The token the parent already minted before any worker spawned (Phase
    5: authentication runs once, in the parent, before any worker exists —
    see ``supervisor.py``'s own module docstring). An environment override
    is checked first, mirroring ``runtimes.intraday_options.engine_worker.
    _resolve_access_token`` exactly; otherwise the on-disk cache the
    parent's own ``AuthBootstrap.get_token()`` call just wrote. Never a
    fresh network authentication call from a worker process."""
    env_token = read_secret(settings.dhan_access_token)
    if env_token:
        return env_token
    cached = TokenCache(cache_dir / TOKEN_CACHE_FILENAME).load(expected_client_id=client_id)
    return cached.access_token if cached is not None else None


def _build_scrip_master_for(config: WorkerConfig, *, default_cache_dir: Path) -> ScripMaster:
    """The real, Dhan-instrument-master-backed resolver — built fresh in
    this process (never pickled across the spawn boundary), from exactly
    the same ``parameters`` a direct ``__main__.py`` composition already
    reads via ``resolve_index_meta``."""
    meta = resolve_index_meta(
        config.underlying_instrument,
        index_security_id=str(config.parameters.get("index_security_id") or "") or None,
        index_segment=str(config.parameters.get("index_segment") or "") or None,
        fno_segment=str(config.parameters.get("fno_segment") or "") or None,
    )
    cache_dir = (
        Path(config.scrip_master_cache_dir) if config.scrip_master_cache_dir else default_cache_dir
    )
    return ScripMaster(config.underlying_instrument, exchange=meta.exchange).load(
        cache=ScripMasterCache(cache_dir)
    )


@dataclass
class WorkerOutcome:
    exit_code: int = 0
    error: str | None = None
    #: A SIGTERM/SIGINT or an operator square-off-request file ended this
    #: run, as opposed to an unhandled exception or the feed exhausting
    #: itself. Unlike the intraday worker, this is never forced to imply
    #: exposure was closed — see ``run_worker``'s own docstring.
    stopped_by_request: bool = False
    #: True whenever ``engine.run()`` returned without raising. Deliberately
    #: **not** "no open legs remain" — for this engine, stopping with an open
    #: cycle is the normal, expected overnight case, recovered on the next
    #: restart (spec section 9.2); only an unhandled exception is "unclean".
    clean_shutdown: bool = False
    #: True only when an operator's square-off request file was present *and*
    #: the cycle it targeted is now fully closed (``CycleState.COMPLETED``) —
    #: the one case the request file is cleared.
    square_off_completed: bool = False


@dataclass
class _Built:
    engine: PositionalMultiLegEngine
    quotes: QuoteBook


def build_engine(
    config: WorkerConfig,
    *,
    repository: ExecutionRepository,
    session_id: int,
    feed: MarketDataFeed,
    chain_fetcher: ChainFetcher,
    scrip_master: ScripMaster,
    margin_estimator: MarginEstimator,
    notifier: Notifier | None = None,
    heartbeat: HeartbeatWriter | None = None,
    clock: Callable[[], datetime] = now_ist,
) -> _Built:
    """Assemble one real :class:`PositionalMultiLegEngine`.

    Test-facing seam: every dependency this needs from the outside world
    (the feed, the chain fetcher, the scrip master, the margin estimator) is
    injected — a test supplies a :class:`~common.engine.feed.SimulatedFeed`,
    a canned ``ChainFetcher``, a scrip master built from a fixture CSV, and a
    :class:`~common.margin.MarginEstimator` explicitly wired for its own
    scenario (a fake ``margin_fetcher``, or ``margin_fetcher=None`` plus an
    explicitly-injected ``fallback_model`` — see ``common.margin``'s own
    module docstring), never a real Dhan call. Production construction (real
    feed, real Dhan chain fetcher, the daily-downloaded scrip master, and a
    ``MarginEstimator`` wired to the real
    :func:`~common.market_data.dhan_margin.build_dhan_margin_fetcher` with
    no fallback) happens once, in ``__main__.py`` — required, not defaulted,
    here specifically so a production call site can never silently omit it
    and fall through to an implicit test-only behaviour.
    """
    execution_mode = ExecutionMode.PAPER  # this runtime never constructs a live broker (spec §9.7)
    quotes = QuoteBook()
    gateway_broker = PaperBroker.from_config(
        paper_execution=config.paper_execution, cost_rates=config.cost_rates, quotes=quotes
    )
    lifecycle = OrderLifecycle(
        repository=repository,
        broker=gateway_broker,
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=execution_mode,
        session_id=session_id,
        quotes=quotes,
    )
    gateway = LifecycleGateway(
        lifecycle,
        strategy_id=config.strategy_id,
        execution_mode=execution_mode,
        trading_date=config.trading_date,
        repository=repository,
        runtime_id=config.runtime_id,
    )
    position_manager = PositionManager(gateway, lots=config.lots)

    # Same injected clock as everything else here (GreeksService, the engine
    # itself) — not the default real wall clock. A chain snapshot's own
    # received_at must be comparable to context.now (which for a real run is
    # also real wall-clock-derived, so this is a no-op in production, and
    # for a deterministic test with an injected clock it is what makes a
    # decision-freshness check evaluable at all against a fixed/simulated
    # "now" rather than silently comparing against the actual wall clock.
    chain_service = OptionChainService(chain_fetcher, wall_clock=clock)
    greeks_service = GreeksService(
        chain_service,
        assumptions=ModelAssumptions(
            risk_free_rate=config.risk_free_rate, dividend_yield=config.dividend_yield
        ),
        max_age_seconds=config.quote_max_age_seconds,
        clock=clock,
    )

    strategy = load_positional_strategy(
        config.strategy_ref,
        {
            "parameters": config.parameters,
            "scrip_master": scrip_master,
            "timezone": config.timezone,
        },
    )

    resolver = DhanOptionChainResolver(scrip_master)
    lot_size = resolver.lot_size or config.lots  # only ever a fallback for a not-yet-loaded master
    feed.add_tick_observer(quotes.record)

    session = MarketSession(config.session)
    lifecycle_policy = PositionalLifecyclePolicy(timezone=config.timezone)

    def _recover() -> Cycle | None:
        return recover_cycle(config, repository)

    def _persist_cycle(cycle) -> None:  # type: ignore[no-untyped-def]
        from common.engine.positional.positional_state import persist_cycle

        persist_cycle(repository, cycle, runtime_id=config.runtime_id)

    def _persist_leg(leg) -> None:  # type: ignore[no-untyped-def]
        from common.engine.positional.positional_state import persist_cycle_leg

        persist_cycle_leg(
            repository,
            leg,
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=execution_mode,
            cycle_id=leg.basket_id,
        )

    def _record_incident(cycle_id: str, message: str) -> None:
        repository.record_error(
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=execution_mode,
            severity="CRITICAL",
            component="positional_multi_leg_engine.incident",
            message=f"cycle={cycle_id}: {message}",
        )

    def _persist_margin_snapshot(cycle_id: str, estimate: MarginEstimate) -> None:
        repository.record_cycle_margin_snapshot(
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=execution_mode,
            cycle_id=cycle_id,
            decision_type="ENTRY_CANDIDATE",
            estimated_margin=estimate.estimated_margin,
            source=estimate.source,
            estimated_at=estimate.estimated_at.isoformat(),
            allocated_capital=estimate.allocated_capital,
            utilization_percent=estimate.utilization_percent,
            correlation_id=None,
        )

    # A one-element box so the reporter's `entries_blocked` closure can read
    # the engine's own live state without the engine existing yet at the
    # point the reporter is constructed (a genuine forward reference: the
    # reporter is a constructor argument *of* the engine it reports on).
    engine_holder: list[PositionalMultiLegEngine] = []
    engine = PositionalMultiLegEngine(
        strategy=strategy,
        position_manager=position_manager,
        gateway=gateway,
        repository=repository,
        feed=feed,
        option_selector=None,  # unused by this engine — see its constructor's own note
        greeks_service=greeks_service,
        margin_estimator=margin_estimator,
        quotes=quotes,
        session=session,
        lifecycle_policy=lifecycle_policy,
        underlying_security_id=config.underlying_security_id,
        underlying_instrument=config.underlying_instrument,
        underlying_segment=config.underlying_segment,
        option_segment=config.option_segment,
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=execution_mode,
        max_adjustments_per_day=config.max_adjustments_per_day,
        max_adjustments_per_cycle=config.max_adjustments_per_cycle,
        min_minutes_between_adjustments=config.min_minutes_between_adjustments,
        evaluation_interval_seconds=config.evaluation_interval_seconds,
        max_quote_age_seconds=config.quote_max_age_seconds,
        entry_leg_timeout_seconds=config.entry_leg_timeout_seconds,
        notifier=notifier or NullNotifier(),
        reporter=(
            HeartbeatEngineReporter(
                heartbeat,
                execution_mode=execution_mode,
                entries_blocked=lambda: engine_holder[0].entries_blocked,
            )
            if heartbeat is not None
            else None
        ),
        recover_cycle=_recover,
        persist_cycle=_persist_cycle,
        persist_cycle_leg=_persist_leg,
        persist_margin_snapshot=_persist_margin_snapshot,
        record_incident=_record_incident,
        clock=clock,
    )
    engine_holder.append(engine)
    del lot_size  # resolved for validation only; the strategy resolves its own per-leg lot size
    return _Built(engine=engine, quotes=quotes)


def close_previous_session(
    config: WorkerConfig, repository: ExecutionRepository, session_id: int
) -> None:
    """Close the session a previous, crashed process left open — the
    positional runtime's own copy of ``intraday_options.worker``'s function
    of the same name (not imported: that module's ``WorkerConfig`` is a
    different, incompatible type, and this is ten lines)."""
    previous = repository.previous_incomplete_session(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        exclude_session_id=session_id,
    )
    if previous is not None:
        log.info("recovering from incomplete session id=%s", previous["id"])
        repository.close_session(int(previous["id"]), reason="recovered_after_restart")


def run_worker(
    config: WorkerConfig,
    *,
    repository: ExecutionRepository,
    session_id: int,
    feed: MarketDataFeed,
    chain_fetcher: ChainFetcher,
    scrip_master: ScripMaster,
    margin_estimator: MarginEstimator,
    runtime_root: Path,
    notifier: Notifier | None = None,
    clock: Callable[[], datetime] = now_ist,
    engine_holder: list[PositionalMultiLegEngine] | None = None,
) -> WorkerOutcome:
    """Build one engine and drive it to completion — the composition root's
    only call into this module beyond ``build_engine`` itself.

    **Stop is not square-off, so a shutdown signal only stops the feed.**
    ``PositionalMultiLegEngine.run()`` already never forces an exit on its
    own stop (see that class's own docstring); this function's SIGTERM/SIGINT
    handler is deliberately just ``feed.stop()`` — never
    ``engine.request_square_off()`` — so a routine overnight stop (deploy,
    restart, operator ``Ctrl-C``) leaves any open cycle exactly as it is, to
    be reconciled and adopted by ``recover_cycle`` on the next restart. The
    *only* thing that ever calls ``request_square_off`` here is an operator's
    square-off request file — a distinct, deliberate act
    (``scripts/square_off.py``), never an accident of process lifecycle.

    ``engine_holder`` (Phase 6A): an optional one-element list the caller
    already constructed *before* this function ran, so it could wire
    ``feed``'s own ``on_poll``/``on_tick_dropped``/``on_feed_gap``
    callbacks — which only exist at ``HubTickFeed`` construction time,
    strictly before the real engine exists — against a forward reference
    (mirrors ``build_engine``'s own internal ``engine_holder`` trick for its
    heartbeat reporter, just threaded one level further out here). Appended
    to the instant the real engine exists, before it ever runs.

    **Phase 6A correction: no separate polling thread lives here anymore.**
    Before this phase, this function ran its own background thread —
    woken every few seconds to write a liveness heartbeat and notice an
    operator's square-off request file. Both jobs were silently broken:
    the thread reused this function's own ``repository``/``database``
    sqlite3 connection from a *different* thread than the one that opened
    it, which sqlite3 refuses outright (D31) — so the very first wake
    raised, uncaught, and killed the thread for the rest of the run.
    Operator square-off polling and periodic heartbeats had therefore
    never actually worked past the first few seconds of any real session.
    Fixed by using the *existing* generic, correctly-threaded mechanism
    instead of a second one: ``_run_positional_worker_locked``'s own
    ``on_poll`` callback (running on this same thread, via
    ``HubTickFeed`` — see that module's own docstring on why that thread
    is the one thread D31 allows to touch this connection) now checks the
    square-off request file directly, and the engine's own ``_evaluate``
    calls its reporter's ``beat()`` on every evaluation, tick- or
    poll-driven. Both are therefore only as available as the caller's own
    ``feed.on_poll`` wiring — true today (the only real caller) and
    consistent with how ``on_tick_dropped``/``on_feed_gap`` already work.
    """
    safe_notifier = (
        notifier if isinstance(notifier, SafeNotifier) else SafeNotifier(notifier or NullNotifier())
    )
    heartbeat = HeartbeatWriter(
        repository,
        session_id=session_id,
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
    )
    outcome = WorkerOutcome()
    close_previous_session(config, repository, session_id)
    square_off_request_file = square_off_request_path(
        runtime_root, config.runtime_id, config.strategy_id
    )

    try:
        built = build_engine(
            config,
            repository=repository,
            session_id=session_id,
            feed=feed,
            chain_fetcher=chain_fetcher,
            scrip_master=scrip_master,
            margin_estimator=margin_estimator,
            notifier=safe_notifier,
            heartbeat=heartbeat,
            clock=clock,
        )
    except Exception as exc:
        outcome.exit_code = 1
        outcome.error = str(exc)
        repository.record_error(
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=ExecutionMode.PAPER,
            severity="CRITICAL",
            component="positional_multi_leg_engine",
            message=outcome.error,
        )
        heartbeat.beat(HealthState.FAILED, force=True)
        log.exception("positional engine worker failed to build strategy_id=%s", config.strategy_id)
        repository.close_session(session_id, reason="build_failed")
        return outcome

    engine = built.engine
    if engine_holder is not None:
        engine_holder.append(engine)
    safe_notifier.send(
        NotificationEvent(
            event_type="worker_started",
            message=f"{config.strategy_id} started in paper mode on the positional engine",
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=ExecutionMode.PAPER,
        )
    )

    signalled = threading.Event()

    def _on_shutdown_signal() -> None:
        signalled.set()
        feed.stop()

    try:
        with shutdown_signals(_on_shutdown_signal):
            engine.run()
        outcome.clean_shutdown = True
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        outcome.exit_code = 1
        outcome.error = str(exc)
        repository.record_error(
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=ExecutionMode.PAPER,
            severity="CRITICAL",
            component="positional_multi_leg_engine",
            message=outcome.error,
        )
        heartbeat.beat(HealthState.FAILED, force=True)
        log.exception("positional engine worker failed strategy_id=%s", config.strategy_id)
        repository.close_session(session_id, reason="crashed")
        return outcome

    outcome.stopped_by_request = signalled.is_set() or engine.square_off_requested

    operator_request = read_square_off_request(square_off_request_file)
    if operator_request is not None:
        cycle_still_open = _has_open_cycle(repository, config)
        outcome.square_off_completed = not cycle_still_open
        if outcome.square_off_completed:
            repository.record_audit_event(
                runtime_id=config.runtime_id,
                action="square_off_completed",
                actor=operator_request.requested_by,
                strategy_id=config.strategy_id,
                execution_mode=ExecutionMode.PAPER,
                detail=f"requested at {operator_request.requested_at}: {operator_request.reason}",
            )
            clear_square_off_request(square_off_request_file)
        else:
            log.warning(
                "operator square-off request for strategy_id=%s is still pending "
                "(cycle not yet fully closed) — request file left in place",
                config.strategy_id,
            )

    stop_kind = "requested" if outcome.stopped_by_request else "unattended"
    safe_notifier.send(
        NotificationEvent(
            event_type="worker_stopped",
            message=f"{config.strategy_id} stopped ({stop_kind})",
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=ExecutionMode.PAPER,
        )
    )
    repository.close_session(
        session_id, reason="signal" if outcome.stopped_by_request else "clean_shutdown"
    )
    heartbeat.beat(HealthState.STOPPED, force=True)
    return outcome


def _has_open_cycle(repository: ExecutionRepository, config: WorkerConfig) -> bool:
    """Read-only re-check of durable state after ``engine.run()`` returns —
    never the in-memory engine, which may already be torn down. A row this
    cannot safely interpret is treated as "still open": the fail-closed
    direction for deciding whether it is safe to clear an operator's
    square-off request."""
    try:
        cycle = _load_open_cycle(
            repository,
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=ExecutionMode.PAPER,
        )
    except CycleRowInconsistent:
        return True
    return cycle is not None


# ============================================================ Phase 5: spawn
def run_positional_worker_process(
    config: WorkerConfig,
    tick_queue: Any,
    control_queue: Any,
    notifier: Notifier | _NotifierSentinel | None = None,
    clock: Callable[[], datetime] = now_ist,
) -> None:
    """The real ``multiprocessing.Process(target=...)`` entry point for one
    strategy under :class:`~runtimes.positional_options.supervisor.
    PositionalOptionsSupervisor`'s shared feed hub.

    Mirrors ``runtimes.intraday_options.worker.run_worker_process``'s own
    shape and reasoning exactly: ``multiprocessing`` discards a spawned
    target's return value, so only a ``sys.exit()``-translating wrapper
    like this one makes the supervisor's own ``worker_exit_codes`` reflect
    what actually happened. Every direct caller keeps using
    :func:`run_worker`, unchanged — this function exists only to be the
    thing ``multiprocessing.Process`` calls.

    Builds every network-facing dependency itself, in this process, from
    its own environment and the token the parent already cached — never a
    live client/credential pickled across the spawn boundary (see
    :func:`_cached_dhan_token`) — mirroring ``engine_worker.run_engine``'s
    own per-child construction pattern. ``config.chain_fetcher_factory``/
    ``scrip_master_factory``/``margin_fetcher_factory``, when set, are used
    instead — see their own docstring on
    :class:`~runtimes.positional_options.config_adapter.WorkerConfig`.

    ``clock`` defaults to the real wall clock — a production spawn never
    passes anything else. It exists so a test driving a real spawned
    worker against a *scripted*, non-real-time tick stream (Phase 6A wires
    ``HubTickFeed``'s ``on_poll`` to ``engine.poll()``, which evaluates
    against this clock independent of the last tick's own timestamp) can
    supply a picklable, spawn-safe clock instead — see
    ``tests.integration._weekly_delta_neutral_fixtures.OffsetClock``.
    Threaded straight through to :func:`run_worker`'s own ``clock``
    parameter, unchanged.
    """
    assert config.database_path is not None, "the supervisor must set database_path"
    assert config.lock_dir is not None, "the supervisor must set lock_dir"
    assert config.pid_dir is not None, "the supervisor must set pid_dir"
    assert config.log_dir is not None, "the supervisor must set log_dir"
    assert config.runtime_root is not None, "the supervisor must set runtime_root"

    settings = load_settings()
    # settings= registers this worker's own secrets with its own redaction
    # filter — spawn starts a fresh interpreter, so the parent's own
    # registration never carries over. Mirrors run_worker_process's own
    # setup_logging call in runtimes.intraday_options.worker exactly.
    setup_logging(
        level=settings.algo_log_level,
        log_dir=config.log_dir,
        log_file_name=f"{config.strategy_id}.log",
        settings=settings,
    )

    resolved_notifier: Notifier | None = (
        build_notifier(settings) if notifier is NOTIFIER_FROM_SETTINGS else notifier
    )

    if tick_queue is None:
        # Refused, not degraded to some other path — every positional
        # worker under this supervisor is tick-driven; one spawned with no
        # tick queue at all is a wiring defect, not a valid configuration.
        log.error(
            "refusing to start strategy_id=%s: no tick queue was given; the supervisor "
            "must register every worker with a tick channel",
            config.strategy_id,
        )
        sys.exit(1)

    lock = worker_lock(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        lock_dir=config.lock_dir,
        pid_dir=config.pid_dir,
    )
    try:
        lock.acquire()
    except DuplicateProcessError as exc:
        # Refusal, not a crash. The rest of the group keeps running
        # untouched — see PositionalOptionsSupervisor._report_duplicate_
        # worker, which is what makes this visible to an operator.
        log.error("%s", exc)
        sys.exit(EXIT_DUPLICATE)

    try:
        outcome = _run_positional_worker_locked(
            config, tick_queue, control_queue, resolved_notifier, settings, clock
        )
    finally:
        lock.release()
    sys.exit(outcome.exit_code)


def _run_positional_worker_locked(
    config: WorkerConfig,
    tick_queue: Any,
    control_queue: Any,
    notifier: Notifier | None,
    settings: Any,
    clock: Callable[[], datetime] = now_ist,
) -> WorkerOutcome:
    assert config.database_path is not None
    assert config.runtime_root is not None
    database = Database(config.database_path)
    MigrationRunner(database).run_pending()

    problems = database.integrity_check()
    if problems:
        # A corrupt database must stop this worker, not be traded through.
        outcome = WorkerOutcome(exit_code=1, error=f"integrity check failed: {problems}")
        log.error("refusing to start strategy_id=%s: %s", config.strategy_id, outcome.error)
        database.close()
        return outcome

    repository = ExecutionRepository(database)
    cache_dir = config.cache_dir or Path(".")

    def _credentials() -> tuple[str, str]:
        client_id = read_secret(settings.dhan_client_id)
        if not client_id:
            raise _NoDhanCredentials("DHAN_CLIENT_ID is not set")
        token = _cached_dhan_token(settings, cache_dir, client_id)
        if not token:
            raise _NoDhanCredentials(
                "no cached Dhan access token — the parent must authenticate "
                "(AuthBootstrap.get_token()) before any worker is spawned"
            )
        return client_id, token

    try:
        if config.chain_fetcher_factory is not None:
            chain_fetcher: ChainFetcher = _resolve_factory(config.chain_fetcher_factory)()
        else:
            client_id, token = _credentials()
            chain_fetcher = build_dhan_chain_fetcher(client_id=client_id, access_token=token)

        if config.margin_fetcher_factory is not None:
            margin_fetcher = _resolve_factory(config.margin_fetcher_factory)()
        else:
            client_id, token = _credentials()
            margin_fetcher = build_dhan_margin_fetcher(client_id=client_id, access_token=token)
        margin_estimator = MarginEstimator(margin_fetcher=margin_fetcher)

        if config.scrip_master_factory is not None:
            scrip_master = _resolve_factory(config.scrip_master_factory)()
        else:
            scrip_master = _build_scrip_master_for(config, default_cache_dir=cache_dir)
    except _NoDhanCredentials as exc:
        error_message = str(exc)
        outcome = WorkerOutcome(exit_code=1, error=error_message)
        log.error("refusing to start strategy_id=%s: %s", config.strategy_id, error_message)
        repository.record_error(
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=ExecutionMode.PAPER,
            severity="CRITICAL",
            component="positional_multi_leg_engine",
            message=error_message,
        )
        database.close()
        return outcome

    option_segment_code = _segment_code(config.option_segment)

    def _request_subscription(security_id: str) -> None:
        """Forward one runtime subscription upstream to the hub, never
        blocking. Mirrors ``engine_worker._subscription_sender``'s own
        segment/mode split exactly: the underlying keeps the hub's
        defaults; every other id this engine ever subscribes to is an
        option contract, so it goes out on the real ``NSE_FNO``-equivalent
        segment in Full (21) mode — the only mode carrying a bid/ask book,
        which the paper fill model needs."""
        if control_queue is None:
            log.warning(
                "no control queue wired for strategy_id=%s; a runtime subscription for "
                "%s will never reach the hub",
                config.strategy_id,
                security_id,
            )
            return
        is_underlying = security_id == config.underlying_security_id
        segment = None if is_underlying else option_segment_code
        mode = None if is_underlying else int(SubscriptionMode.FULL)
        try:
            control_queue.put_nowait((security_id, segment, mode))
        except Exception:
            log.exception(
                "could not forward a subscription request for %s from %s; ticks for it "
                "will not arrive",
                security_id,
                config.strategy_id,
            )

    # Phase 6A: HubTickFeed's own on_poll/on_tick_dropped/on_feed_gap
    # callbacks only exist at construction time, strictly before
    # run_worker (and the build_engine call inside it) ever returns the
    # real engine — so this holder is the forward reference every callback
    # below closes over, populated by run_worker itself the instant the
    # real engine exists (mirrors build_engine's own internal
    # engine_holder trick for its heartbeat reporter).
    engine_holder: list[PositionalMultiLegEngine] = []

    # An operator's square-off request file — read here, on this thread,
    # every on_poll wake (see _on_poll below), never from a separate
    # thread: run_worker used to run its own polling thread for exactly
    # this, and it silently never worked past its first wake (see
    # run_worker's own updated docstring for the full D31 explanation).
    assert config.runtime_root is not None
    square_off_request_file = square_off_request_path(
        config.runtime_root, config.runtime_id, config.strategy_id
    )

    def _on_poll() -> None:
        # The only place this worker polls on a timer: without it, a feed
        # gone quiet (staleness, a stuck subscription) would never
        # re-trigger the engine's own hard-expiry-deadline check, which is
        # otherwise only ever reached from an underlying tick. Checked
        # before poll() so a request landing on this exact wake is acted
        # on by this same evaluation, not the next one — mirrors the wall-
        # clock net's own ordering (hub_feed.py's module docstring).
        if not engine_holder:
            return
        request = read_square_off_request(square_off_request_file)
        if request is not None and not engine_holder[0].square_off_requested:
            log.warning(
                "operator square-off request for strategy_id=%s: %s",
                config.strategy_id,
                request.reason,
            )
            engine_holder[0].request_square_off(request.reason)
        engine_holder[0].poll()

    def _on_tick_dropped(notice: TickDropNotice) -> None:
        # Mirrors multi_leg_engine_worker.py's own _on_tick_dropped shape:
        # a lost tick means this worker's own candle/quote picture may be
        # quietly wrong, so new risk is refused for the rest of the day.
        if engine_holder:
            engine_holder[0].block_entries(
                f"tick queue overflow (dropped={notice.dropped}); refusing new risk "
                "for the rest of the day"
            )

    def _on_feed_gap(notice: FeedGapNotice) -> None:
        if engine_holder:
            engine_holder[0].on_feed_gap_notice(notice)

    feed = HubTickFeed(
        tick_queue,
        request_subscription=_request_subscription,
        on_poll=_on_poll,
        on_tick_dropped=_on_tick_dropped,
        on_feed_gap=_on_feed_gap,
        # A live production session never idle-times-out: a quiet option
        # market between adjustment checks is ordinary, not a finished
        # tape. Square-off is never driven by ending this feed early —
        # see run_worker's own docstring ("stop is not square-off") — only
        # the supervisor's shutdown sentinel and this worker's own SIGTERM
        # handler (feed.stop(), already wired inside run_worker, unchanged)
        # ever end it.
        idle_timeout_seconds=None,
    )

    session = repository.open_session(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=os.getpid(),
    )

    outcome = run_worker(
        config,
        repository=repository,
        session_id=session.id,
        feed=feed,
        chain_fetcher=chain_fetcher,
        scrip_master=scrip_master,
        margin_estimator=margin_estimator,
        runtime_root=config.runtime_root,
        notifier=notifier,
        engine_holder=engine_holder,
        clock=clock,
    )
    database.close()
    return outcome
