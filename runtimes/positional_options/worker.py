"""The ``positional_options`` runtime worker — builds and runs one
:class:`~common.engine.positional.positional_engine.PositionalMultiLegEngine`
in-process.

**Single-process, deliberately, for this first positional runtime.**
``runtimes/intraday_options`` spawns one child process per strategy because
it must isolate *several concurrent* strategies from each other. CLAUDE.md
and the spec both restrict this runtime to exactly one approved strategy
(``weekly_delta_neutral``) — the multi-process fan-out that isolation exists
for has no strategy to isolate from yet. This module still gets everything
that isolation would have bought incidentally right (its own process lock,
its own database, its own log file when launched via ``__main__``), and
nothing about the on-disk config/database contract here would need to
change if a second positional strategy is ever approved and this becomes a
real supervisor/child-process split — see ``supervisor.py``'s own docstring.

Mirrors the intraday worker's own recovery/shutdown discipline throughout:
migration + integrity check before any trading, restart reconciliation
before any new entry, and a lock that is never held past this function's
return.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from common.broker import PaperBroker
from common.broker.quotes import QuoteBook
from common.config.models import ExecutionMode
from common.engine.feed import MarketDataFeed
from common.engine.gateway import LifecycleGateway
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
from common.greeks import GreeksService, ModelAssumptions
from common.health import HealthState, HeartbeatWriter
from common.logging import get_logger
from common.market_data.option_chain import ChainFetcher, OptionChainService
from common.market_data.scrip_master import ScripMaster
from common.notifications import NotificationEvent, Notifier, NullNotifier, SafeNotifier
from common.process.signals import shutdown_signals
from common.process.square_off_requests import (
    clear_square_off_request,
    read_square_off_request,
    square_off_request_path,
)
from common.utils.timeutils import now_ist

from .config_adapter import WorkerConfig
from .positional_multi_leg_engine_worker import load_positional_strategy, recover_cycle

#: How often the square-off-request poll thread wakes — independent of, and
#: much coarser than, the engine's own ``evaluation_interval_seconds``: this
#: thread only ever notices an operator's file write and rate-limits the
#: heartbeat, neither of which needs tick-speed polling.
_POLL_INTERVAL_SECONDS = 5.0

log = get_logger(__name__)


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
    notifier: Notifier | None = None,
    heartbeat: HeartbeatWriter | None = None,
    clock: Callable[[], datetime] = now_ist,
) -> _Built:
    """Assemble one real :class:`PositionalMultiLegEngine`.

    Test-facing seam: every dependency this needs from the outside world
    (the feed, the chain fetcher, the scrip master) is injected — a test
    supplies a :class:`~common.engine.feed.SimulatedFeed`, a canned
    ``ChainFetcher``, and a scrip master built from a fixture CSV, never a
    real Dhan call. Production construction (real feed, real Dhan chain
    fetcher, the daily-downloaded scrip master) happens once, in
    ``__main__.py``.
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
        quotes=quotes,
        session=session,
        lifecycle_policy=lifecycle_policy,
        underlying_security_id=config.underlying_security_id,
        underlying_instrument=config.underlying_instrument,
        underlying_segment=config.underlying_segment,
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=execution_mode,
        max_adjustments_per_day=config.max_adjustments_per_day,
        max_adjustments_per_cycle=config.max_adjustments_per_cycle,
        min_minutes_between_adjustments=config.min_minutes_between_adjustments,
        evaluation_interval_seconds=config.evaluation_interval_seconds,
        max_quote_age_seconds=config.quote_max_age_seconds,
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
    runtime_root: Path,
    notifier: Notifier | None = None,
    clock: Callable[[], datetime] = now_ist,
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
    square-off request file, noticed by the poll thread below — a distinct,
    deliberate act (``scripts/square_off.py``), never an accident of process
    lifecycle.
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
    safe_notifier.send(
        NotificationEvent(
            event_type="worker_started",
            message=f"{config.strategy_id} started in paper mode on the positional engine",
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=ExecutionMode.PAPER,
        )
    )

    stop_polling = threading.Event()
    signalled = threading.Event()

    def _poll_square_off() -> None:
        while not stop_polling.wait(_POLL_INTERVAL_SECONDS):
            heartbeat.beat(HealthState.running_for(ExecutionMode.PAPER))
            request = read_square_off_request(square_off_request_file)
            if request is not None and not engine.square_off_requested:
                log.warning(
                    "operator square-off request for strategy_id=%s: %s",
                    config.strategy_id,
                    request.reason,
                )
                engine.request_square_off(request.reason)

    poll_thread = threading.Thread(
        target=_poll_square_off, name=f"{config.strategy_id}:square_off_poll", daemon=True
    )

    def _on_shutdown_signal() -> None:
        signalled.set()
        feed.stop()

    try:
        with shutdown_signals(_on_shutdown_signal):
            poll_thread.start()
            try:
                engine.run()
            finally:
                stop_polling.set()
                poll_thread.join(timeout=_POLL_INTERVAL_SECONDS * 2)
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
