"""The ported engine, running inside one worker process.

Phase 3 Part 2b-ii-B-2 — the wiring. Everything Parts 2a through 2b-ii-B-1 built
offline is assembled here for the first time: the Part 2a exit policies (through the
strategy), the ported :class:`~common.engine.engine.TradingEngine`, the hub's tick
channel via :class:`~common.engine.hub_feed.HubTickFeed`, the persisted execution
path via :class:`~common.engine.gateway.LifecycleGateway`, and
:class:`~common.engine.square_off.PersistedSquareOffAuthority`, which until now had
no caller outside its own tests.

**This module is imported lazily and must stay that way.** It is reached from
exactly one deferred ``import`` inside ``worker.py``'s engine branch, because
importing ``common.engine`` costs a spawned child roughly +0.2 s and that is what
lost ``test_duplicate_worker_startup_is_refused`` its 0.5 s window in Part 2b-i.
Nothing may import this module at another module's top level;
``tests/unit/test_worker_import_boundary.py`` fails if anything does.

The engine runs on the main thread, and why that is not negotiable
-----------------------------------------------------------------
The plan for this part had the engine on its own thread, mirroring the supervisor's
feed thread, so that a coordinating thread could bound its wait on a silent feed.
**That does not work here, and the reason is worth recording.**
:meth:`common.persistence.database.Database.connect` opens SQLite without
``check_same_thread=False``, so the connection belongs to the thread that created it
— the worker's main thread. An engine on a second thread raises
``ProgrammingError: SQLite objects created in a thread can only be used in that same
thread`` on its **first fill**, because every write from ``LifecycleGateway`` down
goes through that one connection.

Loosening ``check_same_thread`` for every user of ``Database`` to serve one caller
would be trading a real safety property for a workaround, so the engine stays on the
main thread and the problem it was meant to solve is fixed at its cause instead.

**The silent-feed residual is fixed, not alarmed about.** ``HubTickFeed`` now asks
``should_stop`` on every poll wake, so a square-off requested while the stream is
quiet is honoured within one poll interval rather than waiting for a tick that may
never come. Returning from that loop reaches ``TradingEngine.run()``'s ``finally``,
which is the second of the two boundaries deviation D18 names — so nothing crosses a
thread and the ownership rule holds untouched.

Two threads remain, and neither touches the database:

* the **main** thread runs the engine, and therefore owns every SQLite write;
* a small daemon thread drains the candle stream this worker does not consume
  (deviation D23) and calls ``request_square_off`` if it sees the supervisor's
  sentinel there. ``request_square_off`` is documented safe from any thread — it sets
  a flag and returns — which is exactly why this is the only cross-thread call.

**The alarm still exists, and now checks the outcome rather than a proxy.** After the
run returns, if a square-off was asked for and a position is *still open in the
database*, :func:`_raise_silent_engine_alarm` raises it through the same three
channels ``supervisor._raise_silent_feed_alarm`` uses — a ``DEGRADED`` heartbeat left
as the last word, a ``CRITICAL`` row in ``errors``, and a notification. That is a
sharper condition than "a thread did not finish": it asserts the thing an operator
actually needs to know.
"""

from __future__ import annotations

import queue as queue_module
import threading
from collections.abc import Callable
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any

from common.broker import build_broker
from common.broker.quotes import QuoteBook
from common.config.paths import load_paths
from common.engine.config import EngineConfig, SessionConfig
from common.engine.daily_guard import DailyRiskRecovery
from common.engine.engine import TradingEngine
from common.engine.feed import SubscriptionMode
from common.engine.gateway import LifecycleGateway
from common.engine.hub_feed import HubTickFeed
from common.engine.models import AdoptedPosition, OptionContract, OptionType, OrderSide
from common.engine.positions import PositionManager
from common.engine.reporting_bindings import HeartbeatEngineReporter, RepositoryReportWriter
from common.engine.selection import (
    DhanOptionChainResolver,
    OptionSelector,
    SimulatedOptionChainResolver,
)
from common.engine.square_off import PersistedSquareOffAuthority, SquareOffAuthority
from common.engine.state_payload import (
    EXIT_STATE_KEY,
    OPEN_POSITION_KEY,
    UnsupportedStateVersion,
    merge_payload,
    read_payload,
)
from common.engine.strategy import BaseStrategy
from common.execution import ExecutionRepository, OrderLifecycle
from common.feed.queues import TickDropNotice
from common.health import HealthState, HeartbeatWriter
from common.logging import get_logger
from common.notifications import NotificationEvent, SafeNotifier
from common.process import shutdown_signals
from common.utils.timeutils import now_ist

from .worker import (
    EngineWorkerConfig,
    WorkerConfig,
    WorkerOutcome,
    close_previous_session,
    resolved_config_stub,
)

log = get_logger(__name__)

#: How long the candle-drain thread blocks before re-checking whether the run is
#: over. Only has to be fine enough that the thread does not outlive the run
#: noticeably; it is a daemon either way.
_DRAIN_POLL_SECONDS = 0.2

#: How long to wait for that thread once the engine has returned.
_DRAIN_JOIN_SECONDS = 2.0


# --------------------------------------------------------------- strategy loading
def load_strategy(strategy_ref: str, kwargs: dict[str, Any]) -> BaseStrategy:
    """Resolve a ``"package.module:ClassName"`` reference and construct it.

    Deliberately *not* :func:`common.engine.strategy.get_strategy`, which takes a
    single positional config argument that the engine's strategies — whose
    constructors are keyword-only — do not fit. Changing the ported registry to suit
    the worker would be changing a port to suit its first caller; carrying the
    reference in configuration instead leaves it untouched.

    The ``isinstance`` check is what makes the dotted reference type-safe: without it
    a typo naming some other class in the same module would be discovered on the
    first tick rather than at startup.
    """
    module_name, separator, class_name = strategy_ref.partition(":")
    if not separator or not module_name or not class_name:
        raise ValueError(f"strategy_ref must be 'package.module:ClassName', got {strategy_ref!r}")
    module = import_module(module_name)
    try:
        factory = getattr(module, class_name)
    except AttributeError as exc:
        raise ValueError(f"{module_name!r} has no attribute {class_name!r}") from exc
    strategy = factory(**kwargs)
    if not isinstance(strategy, BaseStrategy):
        raise TypeError(
            f"{strategy_ref!r} resolved to {type(strategy).__name__}, which is not a "
            "BaseStrategy; the engine cannot drive it."
        )
    return strategy


# ------------------------------------------------------------- restart recovery
def recover_position(
    config: WorkerConfig,
    repository: ExecutionRepository,
) -> AdoptedPosition | None:
    """Rebuild the position a previous process left open, or ``None``.

    Two records are consulted and they check each other. The ``positions`` row is
    authoritative for quantity, price and charges; ``strategy_state.payload`` carries
    the contract identity the row cannot express (option type, strike, expiry, lot
    size — see :mod:`common.engine.state_payload`).

    **Every disagreement between them raises.** An open position whose contract
    cannot be rebuilt is worse than no position at all: the engine would believe it
    was flat and could open a second one. Raising here reaches
    ``TradingEngine._adopt_recovered_position``, which latches entries off for the
    day — so the outcome of any inconsistency is "manage nothing new", never "trade
    alongside something unknown".
    """
    positions = repository.open_positions(
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
    )
    if not positions:
        return None
    position = positions[0]

    try:
        payload = read_payload(
            repository,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            trading_date=config.trading_date,
        )
    except UnsupportedStateVersion as exc:
        # Phase 6 Part 3. read_payload narrows its own "never raises on bad
        # data" rule specifically for this — an unreadable strategy_state.payload
        # is cosmetic, but a state_version this build does not understand risks
        # confidently misreading a foreign shape, which is worse than the loud
        # refusal here. Same CRITICAL-row treatment as every other recovery
        # failure in this function, so a dashboard query for
        # component='engine.recovery' catches this one too.
        _record_recovery_failure(config, repository, str(exc))
        raise RuntimeError(str(exc)) from exc
    record = payload.get(OPEN_POSITION_KEY)
    if not isinstance(record, dict):
        _record_recovery_failure(
            config,
            repository,
            f"position {position.security_id} is open in the database but "
            f"strategy_state.payload carries no {OPEN_POSITION_KEY!r} record, so its "
            "option contract cannot be rebuilt",
        )
        raise RuntimeError(f"no contract record for the open position {position.security_id}")

    try:
        contract = OptionContract(
            symbol=str(record["symbol"]),
            security_id=str(record["security_id"]),
            strike=float(record["strike"]),
            option_type=OptionType(record["option_type"]),
            expiry=str(record["expiry"]),
            lot_size=int(record["lot_size"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        _record_recovery_failure(
            config, repository, f"the stored contract record is unusable: {exc}"
        )
        raise RuntimeError(f"unusable contract record: {exc}") from exc

    if contract.security_id != position.security_id:
        _record_recovery_failure(
            config,
            repository,
            f"the stored contract record names {contract.security_id} but the open "
            f"position is {position.security_id}; the payload is stale",
        )
        raise RuntimeError("the stored contract record does not match the open position")

    if contract.lot_size <= 0 or abs(position.quantity) % contract.lot_size:
        _record_recovery_failure(
            config,
            repository,
            f"quantity {position.quantity} is not a whole number of "
            f"{contract.lot_size}-lot contracts",
        )
        raise RuntimeError("the open quantity is not a whole number of lots")

    # The sign of the persisted quantity is what execution actually wrote
    # (``_upsert_position`` stores ``side.sign * fill.quantity``), so it wins over the
    # payload's copy. Disagreement is logged rather than swallowed: it means one of
    # the two writes did not land, which is worth knowing even though it is
    # recoverable.
    side = OrderSide.BUY if position.quantity > 0 else OrderSide.SELL
    stored_side = record.get("side")
    if stored_side is not None and str(stored_side) != side.value:
        log.warning(
            "the stored contract record says %s but the persisted quantity says %s; "
            "trusting the positions row",
            stored_side,
            side.value,
        )

    lots = abs(position.quantity) // contract.lot_size
    log.info(
        "recovering %s %s x%d @ %.2f opened at %s",
        side.value,
        contract.symbol,
        lots,
        position.average_price,
        position.opened_at.isoformat(),
    )

    # Phase 6 Part 3. A separate, cheap read rather than widening read_payload's
    # return shape -- this is a column on the same row, not part of the payload
    # JSON it decodes. None (no row, empty column, or an unparsable one) leaves
    # the engine's idempotency guard unarmed, exactly as before this part existed.
    last_candle_end_at: datetime | None = None
    state_row = repository.load_strategy_state(
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
    )
    if state_row is not None and state_row["last_candle_end_at"]:
        try:
            last_candle_end_at = datetime.fromisoformat(state_row["last_candle_end_at"])
        except ValueError:
            log.warning(
                "unreadable strategy_state.last_candle_end_at %r for %s; the restart "
                "candle-idempotency guard will not be armed",
                state_row["last_candle_end_at"],
                config.strategy_id,
            )

    return AdoptedPosition(
        contract=contract,
        side=side,
        lots=lots,
        entry_price=position.average_price,
        entry_time=position.opened_at,
        entry_charges=position.charges,
        # Phase 6 Part 3. NULL only for a row Part 3's per-candle persistence
        # never touched (pre-Part-3, or a position that closed before its
        # first candle checkpoint) — 0.0 matches a fresh OpenPosition's own
        # starting value in that case.
        max_favorable_pnl=(
            0.0 if position.highest_favourable is None else position.highest_favourable
        ),
        max_adverse_pnl=0.0 if position.lowest_favourable is None else position.lowest_favourable,
        last_candle_end_at=last_candle_end_at,
    )


def _record_recovery_failure(
    config: WorkerConfig, repository: ExecutionRepository, detail: str
) -> None:
    """Make an unrecoverable open position visible before failing closed."""
    message = (
        f"cannot adopt the open position for {config.strategy_id}: {detail}. "
        "New entries are blocked for the day; the position remains OPEN in the "
        "database and is NOT being managed by this process — close it manually."
    )
    log.error("%s", message)
    repository.record_error(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        severity="CRITICAL",
        component="engine.recovery",
        message=message,
    )


def recover_daily_risk(
    config: WorkerConfig,
    repository: ExecutionRepository,
) -> DailyRiskRecovery | None:
    """Today's already-booked realised P&L and trade count, or ``None``.

    Phase 6 Part 1. ``None`` means there is nothing to restore — either this is
    the day's first session (no ``strategy_state`` row yet), or the trading date
    has rolled since the last one, which the caller (``strategy_id``,
    ``execution_mode``, ``trading_date`` scoped, same as :func:`recover_position`)
    already guarantees cannot cross into a different day. The daily guard's own
    :meth:`~common.engine.daily_guard.DailyRiskGuard.reset` — called immediately
    before this runs — is what makes a fresh day start at zero either way.

    ``realised_pnl`` comes straight from ``strategy_state.daily_realised_pnl``,
    which every closed trade already writes (``_touch_strategy_state``) but which
    nothing previously read back. ``trade_count`` is not a stored counter — it is
    derived from ``positions.status = 'CLOSED'`` for the same strategy/mode/date,
    so it can never drift from the position rows recovery itself trusts as
    authoritative (see :func:`recover_position`'s docstring on why disagreement
    between two records is treated as unrecoverable rather than merged).

    A value that will not convert **raises**, exactly as an unusable contract
    record does in :func:`recover_position`: a daily loss cap seeded from a
    guess is worse than one that refuses to seed at all, because the guess could
    be wrong in the unsafe direction (too low), letting a strategy trade past a
    limit it had already hit.
    """
    row = repository.load_strategy_state(
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
    )
    if row is None:
        return None
    try:
        raw = row["daily_realised_pnl"]
        realised_pnl = 0.0 if raw is None else float(raw)
    except (TypeError, ValueError) as exc:
        _record_daily_risk_recovery_failure(
            config, repository, f"strategy_state.daily_realised_pnl is unusable: {exc}"
        )
        raise RuntimeError(f"unusable daily_realised_pnl: {exc}") from exc
    trade_count = repository.closed_position_count(
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
    )
    return DailyRiskRecovery(realised_pnl=realised_pnl, trade_count=trade_count)


def _record_daily_risk_recovery_failure(
    config: WorkerConfig, repository: ExecutionRepository, detail: str
) -> None:
    """Make an unrestorable daily-risk state visible before failing closed.

    Mirrors :func:`_record_recovery_failure` — same severity, same component, so
    a dashboard query for ``component='engine.recovery'`` catches either failure
    without needing to know which kind it was.
    """
    message = (
        f"cannot restore today's daily risk state for {config.strategy_id}: {detail}. "
        "New entries are blocked for the day rather than seeding a loss cap that "
        "might be wrong."
    )
    log.error("%s", message)
    repository.record_error(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        severity="CRITICAL",
        component="engine.recovery",
        message=message,
    )


def recover_exit_state(
    config: WorkerConfig,
    repository: ExecutionRepository,
) -> dict[str, Any] | None:
    """The previous process's exit-policy snapshot for the open position, or ``None``.

    Phase 6 Part 2. Read straight off ``strategy_state.payload[EXIT_STATE_KEY]`` —
    no consistency check against the ``positions`` row here, because
    ``TradingEngine._restore_exit_state`` already does the one that matters (the
    snapshot's own ``security_id`` against the position actually adopted), and
    doing it twice would just be two copies of the same comparison. Never raises:
    an unreadable snapshot degrades exit timing quality, not safety — see
    ``_restore_exit_state``'s docstring for why that makes fail-open correct here,
    unlike :func:`recover_position` / :func:`recover_daily_risk`.
    """
    payload = read_payload(
        repository,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
    )
    snapshot = payload.get(EXIT_STATE_KEY)
    return snapshot if isinstance(snapshot, dict) else None


# ------------------------------------------------------------------- the run
def run_engine(
    config: WorkerConfig,
    engine_config: EngineWorkerConfig,
    *,
    repository: ExecutionRepository,
    session_id: int,
    heartbeat: HeartbeatWriter,
    notifier: SafeNotifier,
    outcome: WorkerOutcome,
    candle_queue: Any,
    tick_queue: Any,
    control_queue: Any,
) -> WorkerOutcome:
    """Drive the ported engine to completion. Called only from ``worker.py``."""
    if tick_queue is None:
        # Refused, not degraded to the fixture path. A worker configured for the
        # engine and silently running something else would trade a different
        # strategy than the one that was reviewed, which is worse than not trading.
        outcome.exit_code = 1
        outcome.error = (
            "this worker is configured for the engine but was given no tick queue; "
            "the supervisor must register it with tick_channel=True"
        )
        log.error("refusing to start: %s", outcome.error)
        repository.record_error(
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            severity="CRITICAL",
            component="engine",
            message=outcome.error,
        )
        heartbeat.beat(HealthState.FAILED, force=True)
        return outcome

    close_previous_session(config, repository, session_id)

    try:
        engine, gateway, feed, positions, recovered = _build(
            config,
            engine_config,
            repository=repository,
            session_id=session_id,
            heartbeat=heartbeat,
            notifier=notifier,
            tick_queue=tick_queue,
            control_queue=control_queue,
        )
        notifier.send(
            NotificationEvent(
                event_type="worker_started",
                message=(
                    f"{config.strategy_id} started in {config.execution_mode.value} "
                    "mode on the ported engine"
                ),
                runtime_id=config.runtime_id,
                strategy_id=config.strategy_id,
                execution_mode=config.execution_mode,
            )
        )
        requested = _drive(engine, config, candle_queue)
    except Exception as exc:
        outcome.exit_code = 1
        outcome.error = str(exc)
        repository.record_error(
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            severity="CRITICAL",
            component="engine",
            message=str(exc),
        )
        heartbeat.beat(HealthState.FAILED, force=True)
        log.exception("engine worker failed strategy_id=%s", config.strategy_id)
        return outcome

    outcome.ticks_processed = feed.ticks_received
    outcome.ticks_dropped_upstream = feed.ticks_dropped_upstream
    outcome.trades_closed = len(positions.trades)
    outcome.orders_placed = gateway.executions
    outcome.recovered_position = recovered.adopted
    outcome.stopped_by_request = requested or engine.stopped_by_request
    outcome.square_off_completed = _square_off_completed(config, repository)

    # The alarm's condition, checked against the outcome rather than a proxy for it:
    # we asked the engine to close everything, it returned, and something is still
    # open. A position left open by an ordinary end-of-tape run is *not* this — that
    # is the normal case restart recovery exists to adopt.
    still_open = (
        repository.open_positions(
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            trading_date=config.trading_date,
        )
        if outcome.stopped_by_request
        else []
    )
    outcome.clean_engine_shutdown = not still_open

    if outcome.clean_engine_shutdown:
        notifier.send(
            NotificationEvent(
                event_type="worker_stopped",
                message=f"{config.strategy_id} stopped cleanly",
                runtime_id=config.runtime_id,
                strategy_id=config.strategy_id,
                execution_mode=config.execution_mode,
            )
        )
        repository.close_session(
            session_id, reason="signal" if outcome.stopped_by_request else "clean_shutdown"
        )
        heartbeat.beat(HealthState.STOPPED, force=True)
    else:
        _raise_silent_engine_alarm(config, repository, heartbeat, notifier, still_open)
        # DEGRADED has now been written, forced. Nothing may follow it — a tidy
        # STOPPED here would erase the alarm from the dashboard tile, which is the
        # same ordering rule supervisor._publish_final_health keeps.
        repository.close_session(session_id, reason="engine_did_not_stop")
    return outcome


class _Recovered:
    """Whether the recovery provider handed back a position. One mutable flag.

    The provider is a callable the engine invokes at its own moment, so the worker
    cannot learn the answer from a return value.
    """

    def __init__(self) -> None:
        self.adopted = False


def _build(
    config: WorkerConfig,
    engine_config: EngineWorkerConfig,
    *,
    repository: ExecutionRepository,
    session_id: int,
    heartbeat: HeartbeatWriter,
    notifier: SafeNotifier,
    tick_queue: Any,
    control_queue: Any,
) -> tuple[TradingEngine, LifecycleGateway, HubTickFeed, PositionManager, _Recovered]:
    """Assemble the engine and everything behind it."""
    option_selector, option_segment = build_option_selector(config, engine_config)
    # Full (21) is the only mode carrying a bid/ask book, and the paper fill model
    # prices against it (Phase 4 Part 5). Derived from ``option_segment`` rather
    # than configured beside it, because the two answer the same question: a
    # synthetic contract has neither a real exchange segment nor a real book, so
    # the recorded paths keep the hub's Ticker default and behave as they did.
    option_mode = None if option_segment is None else int(SubscriptionMode.FULL)

    # One book, two readers (Phase 4 Part 5): the lifecycle takes the *current*
    # quote from it to price a fill against a real bid/ask, and the paper broker
    # takes a *range* of recent quotes from it to select one at the simulated
    # latency deadline and to settle resting limit orders. It is filled by the
    # feed observer registered below, on the same thread that then reads it.
    quotes = QuoteBook()

    # The lot size and tick size the broker's rejection rules check against come
    # from the exchange's own daily master when there is one. ``None`` for the
    # simulated resolver, which leaves those rules inactive — inventing a lot size
    # to validate synthetic contracts against would reject correct orders.
    instrument_rules = getattr(option_selector.resolver, "instrument_rules", None)

    # The live gate stays where Phase 1 put it (deviation D5/D19): the factory
    # refuses live, and the engine's arrival does not get its own opinion about it.
    broker = build_broker(
        resolved_config_stub(config),
        paper_execution=config.paper_execution,
        cost_rates=config.cost_rates,
        quotes=quotes,
        instrument_rules=instrument_rules,
    )
    lifecycle = OrderLifecycle(
        repository=repository,
        broker=broker,
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        session_id=session_id,
        config_fingerprint=config.config_fingerprint,
        quotes=quotes,
    )
    gateway = LifecycleGateway(
        lifecycle,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
        repository=repository,
        runtime_id=config.runtime_id,
    )
    positions = PositionManager(gateway, lots=engine_config.lots)

    cfg = EngineConfig(
        timeframe=engine_config.timeframe,
        # Derived, never configured twice: the entry cutoff and square-off time come
        # from the one policy the worker already holds, so they cannot drift apart.
        session=SessionConfig.from_square_off_policy(
            config.square_off_policy,
            start_time=engine_config.session_start_time,
            holidays=tuple(engine_config.holidays),
        ),
        execution_mode=config.execution_mode,
        max_daily_loss_percent=engine_config.max_daily_loss_percent,
        starting_capital=engine_config.starting_capital,
        warmup_from_history=engine_config.warmup_from_history,
        regime_enabled=engine_config.regime_enabled,
        parameters=dict(engine_config.parameters),
    )

    # The engine and its feed refer to each other, so one of them is wired after
    # construction. The holder keeps that to a single, obvious indirection rather
    # than reaching into HubTickFeed's privates from outside.
    holder: list[TradingEngine] = []

    def _on_square_off(reason: str) -> None:
        holder[0].request_square_off(reason)

    def _on_tick_dropped(notice: TickDropNotice) -> None:
        # Runbook limitation 14 / deviation D23: this worker builds its own candles
        # from these ticks, so a drop upstream means its OHLC may be quietly wrong.
        # Entry side only — an open position keeps its exits and its square-off.
        holder[0].block_entries(
            f"the hub dropped {notice.dropped} tick(s) for this worker; candles built "
            "here may differ from the hub's, so trading on them is not safe"
        )

    warmup_manager, warmup_source = build_warmup_manager(config, engine_config)

    # Hoisted above the feed so the wall-clock net below and the engine share **one**
    # authority instance. Two would mean two `_load_state()` reads and two
    # `_attempt_recorded` flags — i.e. two deciders against one position, which is
    # the collision Part 2b-ii-B-1 settled.
    square_off_authority = PersistedSquareOffAuthority(
        config.square_off_policy,
        repository,
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
    )

    feed = HubTickFeed(
        tick_queue,
        request_subscription=_subscription_sender(
            config, control_queue, option_segment=option_segment, option_mode=option_mode
        ),
        on_square_off=_on_square_off,
        on_tick_dropped=_on_tick_dropped,
        # What makes a SIGTERM land during a silent stretch. Without it the request
        # would wait for a tick to carry it into on_tick, and a live session runs
        # with no idle timeout at all — so that wait would be unbounded.
        should_stop=lambda: bool(holder) and holder[0].square_off_requested,
        on_poll=_wall_clock_square_off(
            holder, square_off_authority, trading_date=config.trading_date
        ),
        poll_seconds=engine_config.feed_poll_seconds,
        idle_timeout_seconds=config.idle_timeout_seconds,
    )
    # Ahead of the engine's own handler, which the feed contract guarantees: the
    # engine's reaction to a tick may be to place an order priced off exactly that
    # tick's quote, so the book must already hold it by then.
    feed.add_tick_observer(quotes.record)

    recovered = _Recovered()

    def _recover() -> AdoptedPosition | None:
        adopted = recover_position(config, repository)
        recovered.adopted = adopted is not None
        return adopted

    def _recover_daily_risk() -> DailyRiskRecovery | None:
        return recover_daily_risk(config, repository)

    def _recover_exit_state() -> dict[str, Any] | None:
        return recover_exit_state(config, repository)

    def _persist_exit_state(data: dict[str, Any] | None, last_candle_end_at: str | None) -> None:
        merge_payload(
            repository,
            {EXIT_STATE_KEY: data},
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            trading_date=config.trading_date,
            last_candle_end_at=last_candle_end_at,
        )

    def _persist_position_marks(highest_favourable: float, lowest_favourable: float) -> None:
        # positions (the same PositionManager instance the engine drives) is
        # already in scope here -- the engine only calls this while it holds
        # exactly one open position, so this is the one it means.
        current = positions.positions
        if not current:  # pragma: no cover - guarded by the engine's own check
            return
        repository.update_position_marks(
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            trading_date=config.trading_date,
            security_id=current[0].contract.security_id,
            highest_favourable=highest_favourable,
            lowest_favourable=lowest_favourable,
        )

    engine = TradingEngine(
        cfg,
        feed=feed,
        option_selector=option_selector,
        strategy=load_strategy(engine_config.strategy_ref, engine_config.strategy_kwargs),
        position_manager=positions,
        underlying_security_id=config.security_id,
        underlying_instrument=engine_config.underlying_instrument or config.instrument,
        runtime_id=config.runtime_id,
        notifier=notifier,
        reporter=HeartbeatEngineReporter(
            heartbeat,
            execution_mode=config.execution_mode,
            entries_blocked=lambda: holder[0].entries_blocked if holder else None,
        ),
        report=RepositoryReportWriter(
            repository,
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
            trading_date=config.trading_date,
        ),
        # The only decider that survives a restart. Constructed above so the
        # wall-clock net consults this same instance rather than a second one.
        square_off_authority=square_off_authority,
        recover_position=_recover,
        recover_daily_risk=_recover_daily_risk,
        recover_exit_state=_recover_exit_state,
        persist_exit_state=_persist_exit_state,
        persist_position_marks=_persist_position_marks,
        warmup_manager=warmup_manager,
        warmup_source=warmup_source,
    )
    holder.append(engine)
    return engine, gateway, feed, positions, recovered


def build_option_selector(
    config: WorkerConfig, engine_config: EngineWorkerConfig
) -> tuple[OptionSelector, int | None]:
    """Build the strike selector, and say which segment its contracts live in.

    Returns ``(selector, option_segment)``. ``option_segment`` is ``None`` for
    the simulated resolver — a synthetic id has no segment, and the recorded
    paths that use it must keep behaving exactly as they did.

    ``"dhan"`` closes runbook limitation 17: the resolver reads Dhan's daily
    instrument master, so the id, the strike, the expiry **and the lot size** all
    come from the exchange rather than from configuration. The master is loaded
    once here, at worker start, rather than per entry — resolution during the
    session must not be able to block on an HTTP call or a rate limit.
    """
    if engine_config.contract_resolver == "simulated":
        return (
            OptionSelector(
                SimulatedOptionChainResolver(config.instrument, lot_size=engine_config.lot_size),
                strike_step=engine_config.strike_step,
                expiry=engine_config.expiry,
            ),
            None,
        )

    if engine_config.contract_resolver != "dhan":
        raise ValueError(
            f"Unknown contract_resolver {engine_config.contract_resolver!r}; "
            "expected 'simulated' or 'dhan'."
        )

    # Deferred: the master reaches the network and belongs nowhere near a
    # simulated run's import path.
    from common.market_data.scrip_master import (
        ScripMaster,
        ScripMasterCache,
        resolve_index_meta,
        segment_code,
    )

    meta = resolve_index_meta(
        config.instrument,
        index_security_id=engine_config.index_security_id or None,
        index_segment=engine_config.index_segment or None,
        fno_segment=engine_config.fno_segment or None,
    )
    cache_dir = (
        Path(engine_config.scrip_master_cache_dir)
        if engine_config.scrip_master_cache_dir
        else load_paths().cache_root
    )
    master = ScripMaster(config.instrument, exchange=meta.exchange).load(
        cache=ScripMasterCache(cache_dir)
    )
    resolver = DhanOptionChainResolver(master, expiry=engine_config.expiry)
    log.info(
        "resolving real %s contracts: expiry=%s lot_size=%s segment=%s",
        config.instrument,
        resolver.expiry,
        resolver.lot_size,
        meta.fno_segment,
    )
    return (
        OptionSelector(
            resolver,
            strike_step=engine_config.strike_step,
            # The resolver already fixed the session's expiry, including when
            # none was configured. Passing it on keeps the selector and the
            # resolver from disagreeing about which series is being traded.
            expiry=resolver.expiry,
        ),
        segment_code(meta.fno_segment),
    )


def build_warmup_manager(
    config: WorkerConfig, engine_config: EngineWorkerConfig
) -> tuple[Any, Any]:
    """Build ``(warmup_manager, warmup_source)``, or ``(None, None)`` when opted
    out — Phase 4 Part 4, runbook limitation 16.

    Returned as ``Any``, not the concrete types: importing
    ``common.warmup.manager``/``.source`` at this module's top level would be
    fine on its own, but the whole point of ``engine_worker.py`` is that
    nothing forces ``common.engine``'s import cost onto a config that never
    asked for it — see the module docstring. The concrete types stay behind
    the same deferred-import discipline ``build_option_selector`` already
    uses for ``common.market_data.scrip_master``.

    Never raises. Warm-up is a correctness nicety, never a precondition for
    trading (the same posture ``TradingEngine._warm_up`` already has) — an
    operator who opts in to ``warmup_source="dhan"`` but has no usable
    credentials, or whose config can't resolve an underlying, gets a cold
    start and a logged reason, not a startup failure.

    **Underlying-only.** At this point in ``_build()`` no option contract has
    been resolved yet — the strategy picks its strike on its first signal —
    and every continuity-required indicator in this repository runs on the
    *underlying's* candles (``TradingEngine._warm_up``'s ``_sink`` feeds
    ``strategy.on_candle`` unconditionally off the underlying stream). So this
    only ever builds ``WarmupSource.from_underlying(...)``; ``from_option`` is
    ported (see its own docstring) but unreachable from here.

    **Independent of ``contract_resolver``.** ``resolve_index_meta`` needs no
    scrip master — it is the *option* resolver that reads the daily
    instrument master, not the underlying lookup. So ``warmup_source="dhan"``
    does not require ``contract_resolver="dhan"``; a strategy can warm its
    underlying's indicators from real history while still selecting synthetic
    option contracts, or vice versa.
    """
    if engine_config.warmup_source == "none":
        return None, None
    if engine_config.warmup_source != "dhan":
        raise ValueError(
            f"Unknown warmup_source {engine_config.warmup_source!r}; expected 'none' or 'dhan'."
        )

    # Deferred for the same reason build_option_selector's "dhan" branch defers
    # its scrip-master imports: none of this belongs on a config that never
    # asked for it.
    from common.config import load_settings
    from common.config.secrets import read_secret
    from common.market_data.dhan_historical import DhanHistoricalDataClient
    from common.market_data.scrip_master import resolve_index_meta
    from common.warmup.historical import fetch_warmup_candles_range
    from common.warmup.manager import WarmupManager
    from common.warmup.source import WarmupSource

    meta = resolve_index_meta(
        config.instrument,
        index_security_id=engine_config.index_security_id or None,
        index_segment=engine_config.index_segment or None,
        fno_segment=engine_config.fno_segment or None,
    )
    if meta.security_id != config.security_id:
        # Not hypothetical: index_security_id is an independent override, and
        # a stale one would otherwise seed indicators from a different
        # instrument's history than the one ticks are actually arriving for
        # -- silently, since a REST fetch for the wrong id still succeeds.
        log.error(
            "%s: warmup_source='dhan' but the resolved underlying (%s) does not match "
            "this worker's security id (%s); refusing to warm from a possibly wrong "
            "instrument and falling back to a cold start",
            config.strategy_id,
            meta.security_id,
            config.security_id,
        )
        return None, None

    settings = load_settings()
    client_id = read_secret(settings.dhan_client_id)
    access_token = _resolve_access_token(settings, client_id)
    if not client_id or not access_token:
        log.warning(
            "%s: warmup_source='dhan' but no Dhan client id/access token is available "
            "(set DHAN_ACCESS_TOKEN, or run scripts/auth_bootstrap.py first); falling "
            "back to a cold start",
            config.strategy_id,
        )
        return None, None

    client = DhanHistoricalDataClient(client_id, access_token)
    source = WarmupSource.from_underlying(meta)

    def _fetch(
        source: Any,
        *,
        session: Any,
        timeframe_minutes: int,
        lookback_sessions: int,
        now: Any = None,
    ) -> Any:
        return fetch_warmup_candles_range(
            client,
            security_id=source.security_id,
            exchange_segment=source.exchange_segment,
            instrument_type=source.instrument_type,
            session=session,
            timeframe_minutes=timeframe_minutes,
            lookback_sessions=lookback_sessions,
            now=now,
            underlying_label=engine_config.underlying_instrument or config.instrument,
        )

    manager = WarmupManager(
        _fetch, max_lookback_sessions=engine_config.warmup_max_lookback_sessions
    )
    return manager, source


def _resolve_access_token(settings: Any, client_id: str | None) -> str | None:
    """The same precedence ``scripts/auth_bootstrap.py`` uses: an explicit env
    override first, then the cache the bootstrap itself writes. Never mints a
    token -- no PIN/TOTP flow here, since generation is the bootstrap's job
    alone (spec line 1178: no trading runtime generates its own token).
    """
    from common.config.secrets import read_secret

    token = read_secret(settings.dhan_access_token)
    if token:
        return token
    if not client_id:
        return None

    from common.authentication import TokenCache
    from common.authentication.bootstrap import TOKEN_CACHE_FILENAME

    paths = load_paths(settings=settings)
    cached = TokenCache(paths.cache_root / TOKEN_CACHE_FILENAME).load(expected_client_id=client_id)
    return cached.access_token if cached is not None else None


def _wall_clock_square_off(
    holder: list[TradingEngine],
    authority: SquareOffAuthority,
    *,
    trading_date: str,
    clock: Callable[[], datetime] = now_ist,
) -> Callable[[], None]:
    """The wall-clock square-off net — runbook limitation 7.

    The engine's square-off is driven by the **candle clock**: `on_tick` asks
    `authority.due(tick.exchange_time)`. That is the right primary mechanism —
    it is deterministic and a replay reaches the same decision — but it has one
    failure mode, and it is the expensive one. *If the feed stops before the
    square-off bar, square-off never triggers at all* and a position is carried
    overnight. `squareoff.py`'s own docstring named the candle clock as the
    design; this is the safety net it always needed.

    Wired into `HubTickFeed`'s poll loop, which is the only thing in a worker
    that runs on a timer. It fires whether ticks flow or not.

    **One owner is preserved.** This does not decide anything: it asks the *same*
    `SquareOffAuthority` the engine asks, merely supplying the clock reading the
    tick stream failed to supply. `PersistedSquareOffAuthority` returns `False`
    for a day already `COMPLETED`, so a restart cannot re-close — no new state
    and no migration. The close itself goes through `request_square_off`, i.e.
    the **D18** path the sentinel and `SIGTERM` already use, so there is no
    second square-off code path to keep in step.

    Runs on the worker's main thread (see `HubTickFeed.on_poll`), so the
    authority's SQLite write is safe under **D31**.

    Why the trading-date guard exists
    ---------------------------------
    `SquareOffPolicy.trigger_at` is a **time-of-day** decision — it has no notion
    of *which* day. The candle clock never needed one, because a tick's timestamp
    carries its own date and a replayed tape is self-consistent. A wall clock
    does not have that property: it always reports *today*.

    Without this guard, a worker whose `trading_date` is not today — a replay of
    a recorded tape, or a worker that outlived midnight — would ask "is the wall
    clock past 15:15?", get `True`, and square off before processing a single
    tick. Found exactly that way: the net fired in 25 tests on first run, because
    they replay a 2026-07-16 tape at whatever the real time happens to be.

    So the wall clock is treated as authoritative only for the day it belongs to.
    Off that day the net stays silent and the candle clock remains the only
    decider, which is the pre-Part-3 behaviour and the safe direction.
    """

    def _check() -> None:
        if not holder:
            return  # constructed before the engine; nothing to ask yet
        engine = holder[0]
        if engine.square_off_requested:
            return  # already asked; asking twice writes nothing but says nothing either
        now = clock()
        if now.date().isoformat() != trading_date:
            return  # the wall clock speaks only for its own day; see above
        if not authority.due(now):
            return
        log.warning(
            "wall-clock square-off: the square-off time has passed with no tick to "
            "carry it, so the close is being requested on the poll timer instead"
        )
        engine.request_square_off("wall clock reached the square-off time")

    return _check


def _subscription_sender(
    config: WorkerConfig,
    control_queue: Any,
    *,
    option_segment: int | None = None,
    option_mode: int | None = None,
) -> Callable[[str], None] | None:
    """Forward the engine's runtime subscriptions upstream, never blocking.

    ``None`` when there is no control queue, which makes ``HubTickFeed`` say so
    rather than silently swallowing a subscription whose ticks will never arrive.

    Non-blocking for the same reason the hub's own queues are: this runs on the tick
    callback path, and a blocked send here stalls the engine's whole feed.

    **Where the exchange segment and the subscription mode are decided.** The
    engine's feed contract is ``subscribe(security_id)`` — one string, no segment
    and no mode — and widening it would mean touching
    :class:`~common.engine.engine.TradingEngine`, whose ported session-gating test
    pins it attribute by attribute. It does not need widening: this runtime already
    knows the answer. Everything the engine subscribes at runtime other than the
    underlying **is** an option contract, so the underlying keeps the hub's
    defaults and every other id gets ``option_segment`` and ``option_mode``.
    Deciding it here also keeps the knowledge in the wiring, which is where the
    rest of the instrument resolution lives.

    The mode split is not cosmetic. An index carries no order book at all, so
    putting the underlying on Full would gain nothing and cost a wider frame per
    tick; an option contract on Ticker carries no book either, and the paper fill
    model would then price every fill off last price while its configuration said
    it was pricing off bid/ask.
    """
    if control_queue is None:
        return None

    def _send(security_id: str) -> None:
        is_underlying = security_id == config.security_id
        segment = None if is_underlying else option_segment
        mode = None if is_underlying else option_mode
        try:
            control_queue.put_nowait((security_id, segment, mode))
        except Exception:  # a full or closed queue must not stop the run
            log.exception(
                "could not forward a subscription request for %s from %s; ticks for it "
                "will not arrive",
                security_id,
                config.strategy_id,
            )

    return _send


def _drive(
    engine: TradingEngine,
    config: WorkerConfig,
    candle_queue: Any,
) -> bool:
    """Run the engine on this thread, with the candle drain beside it.

    Returns whether a square-off was requested, by signal or by candle sentinel.
    """
    requested = threading.Event()

    def _request(reason: str) -> None:
        requested.set()
        # Safe from any thread by contract: it sets a flag and returns. That is what
        # makes both callers of this — a signal handler and the drain thread —
        # legitimate without either touching a position or the database.
        engine.request_square_off(reason)

    stop_draining = threading.Event()
    drain_thread = threading.Thread(
        target=_drain_candle_queue,
        args=(candle_queue, _request, stop_draining),
        name=f"{config.strategy_id}:candles",
        daemon=True,
    )

    with shutdown_signals(lambda: _request("shutdown signal")):
        drain_thread.start()
        try:
            engine.run()
        finally:
            stop_draining.set()
            drain_thread.join(timeout=_DRAIN_JOIN_SECONDS)
    return requested.is_set()


def _drain_candle_queue(
    candle_queue: Any, on_sentinel: Callable[[str], None], stop: threading.Event
) -> None:
    """Discard the candle stream this worker does not consume, and watch for its sentinel.

    An engine worker builds its own bars from ticks (deviation D23), so the hub's
    completed candles are of no use to it — but the supervisor still publishes them,
    and an undrained queue fills, drops the oldest, and reports drops that mean
    nothing for this worker. Draining keeps ``SupervisorResult.dropped_events``
    honest.

    The ``None`` sentinel is what makes this more than tidiness. The supervisor
    publishes it on **both** channels, so a shutdown still reaches the engine here if
    the tick queue is starved or backed up behind a full buffer. The tick sentinel
    remains the primary path; this one costs a daemon thread and covers the case
    where it does not arrive.

    Touches no database, by design — see the module docstring. Its only outward call
    is ``on_sentinel``, which reaches ``request_square_off`` and nothing else.
    """
    if candle_queue is None:
        return
    while not stop.is_set():
        try:
            item = candle_queue.get(timeout=_DRAIN_POLL_SECONDS)
        except queue_module.Empty:
            continue
        except (OSError, ValueError):  # pragma: no cover - queue closed at shutdown
            return
        if item is None:
            on_sentinel("supervisor candle sentinel")
            return


def _square_off_completed(config: WorkerConfig, repository: ExecutionRepository) -> bool:
    """Read the persisted square-off state rather than tracking a second copy."""
    row = repository.load_strategy_state(
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=config.trading_date,
    )
    if row is None:
        return False
    try:
        return bool(row["square_off_state"] == "COMPLETED")
    except (IndexError, KeyError):  # pragma: no cover - the column exists
        return False


def _raise_silent_engine_alarm(
    config: WorkerConfig,
    repository: ExecutionRepository,
    heartbeat: HeartbeatWriter,
    notifier: SafeNotifier,
    still_open: list[Any],
) -> None:
    """Make a square-off that did not flatten the book impossible to miss.

    The engine-level twin of ``supervisor._raise_silent_feed_alarm``, and deliberately
    the same three channels: a log line is not an alarm, because nobody is tailing the
    file at 15:31. The heartbeat drives the dashboard tile, the ``errors`` row gives
    that tile its message, and the notification reaches a human looking at neither.

    This is the case where it matters most. The supervisor's version means a socket
    was left for process exit to reclaim; this one means **a position is still open
    and no process is managing it**.
    """
    instruments = ", ".join(sorted(position.security_id for position in still_open))
    message = (
        "the engine was asked to square off and the run has ended, but "
        f"{len(still_open)} position(s) are still OPEN in the database: {instruments}. "
        "No process is managing them now. Close them manually and check the positions "
        "table before the next session."
    )
    log.error("%s", message)
    repository.record_error(
        runtime_id=config.runtime_id,
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        severity="CRITICAL",
        component="engine",
        message=message,
    )
    notifier.send(
        NotificationEvent(
            event_type="engine_square_off_incomplete",
            message=message,
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
            execution_mode=config.execution_mode,
        )
    )
    # Forced, and deliberately the last heartbeat this run writes.
    heartbeat.beat(HealthState.DEGRADED, force=True)
