"""Who owns ``SIGINT``, and how the engine is told to square off.

Phase 3 Part 2b-i. These tests have no counterpart upstream: the reference
repository has **zero** coverage of its engine's interrupt path —
``grep -rn "SIGINT\\|KeyboardInterrupt\\|signal\\.signal" tests/ strategies/``
returns nothing there. The path they cover is also the one Part 2b could not be
started without resolving, so they are the acceptance gate for it.

The problem, as the runbook recorded it (§8, "Blocker: the signal-ownership
collision"): ``supervisor.py`` and the reference engine both install
``signal.signal(SIGINT, ...)``. The engine's run loop sits *inside* the
supervisor's feed lifetime, so the engine installs second and wins delivery. On
Ctrl-C the engine would square off and re-raise, and the supervisor's ordered feed
shutdown — the whole of Phase 3 Part 1 — would be reached only via that re-raise,
if at all. Save/restore nesting is LIFO and correct on both sides, so it never
shows up as a crash; it shows up as a shutdown path that quietly stops running.

The resolution is the same direction Part 1 took with ``request_stop()``: the
engine installs **no** handler and instead exposes
:meth:`~common.engine.engine.TradingEngine.request_square_off`, a thread-safe
setter that touches an event and nothing else. The engine acts on it at boundaries
owned by the thread already running it — chiefly ``on_tick``, which runs on the
feed's own callback thread and is therefore the one thread permitted to call
``feed.stop()``. A handler that squared off would instead be mutating positions
from an interrupted stack and then closing the feed cross-thread, which is exactly
the deadlock Part 1 exists to prevent.
"""

from __future__ import annotations

import ast
import queue
import signal
import threading
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from common.config.models import ExecutionMode
from common.engine import engine as engine_module
from common.engine.config import EngineConfig, SessionConfig
from common.engine.engine import TradingEngine
from common.engine.feed import MarketDataFeed, SimulatedFeed
from common.engine.models import ExitReason
from common.engine.positions import InMemoryGateway, PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.engine.square_off import PersistedSquareOffAuthority, SquareOffAuthority
from common.execution import ExecutionRepository
from common.models import Tick
from common.persistence import Database, MigrationRunner
from common.risk import SquareOffPolicy
from strategies.intraday_options.engine_fixture_strategy import EngineFixtureStrategy

IST = ZoneInfo("Asia/Kolkata")
UNDERLYING = "INDEX"
CE_CONTRACT = "SIM:NIFTY:WEEKLY:24000:CE"

#: Long enough that a genuine failure reports instead of hanging the suite, short
#: enough that the file stays fast. Every wait in here is bounded.
JOIN_TIMEOUT = 5.0


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 7, 16, hour, minute, second, tzinfo=IST)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id,
        instrument=security_id,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


class _BlockingFeed(MarketDataFeed):
    """A feed whose ``run()`` genuinely blocks, the way a live socket does.

    ``queue.get()`` with no timeout, deliberately: a double that polled would let a
    broken implementation pass by simply waiting for the next poll. It also records
    **which thread** called :meth:`stop`, which is the property under test — under
    the Part 1 ownership rule that must always be the thread inside ``run()``.
    """

    def __init__(self) -> None:
        super().__init__()
        self._inbox: queue.Queue[Tick | None] = queue.Queue()
        self.stop_callers: list[str] = []
        self.run_thread: str | None = None
        self.returned = threading.Event()

    def deliver(self, tick: Tick) -> None:
        self._inbox.put(tick)

    def finish(self) -> None:
        """End the tape without a stop request, as an exhausted feed would."""
        self._inbox.put(None)

    def run(self) -> None:
        self.run_thread = threading.current_thread().name
        self._running = True
        try:
            while self._running:
                item = self._inbox.get()  # no timeout: genuinely blocking
                if item is None:
                    break
                self._emit(item)
        finally:
            self._running = False
            self.returned.set()

    def stop(self) -> None:
        self.stop_callers.append(threading.current_thread().name)
        self._running = False
        # Release the blocked get() so run() can return on its own thread.
        self._inbox.put(None)


class _SilentFeed(MarketDataFeed):
    """Connected, delivering nothing, and never noticing a stop.

    The residual case: a live socket with no traffic gives ``on_tick`` no boundary
    at which to act. Mirrors the supervisor's unclosable-feed double from Part 1.
    """

    def __init__(self) -> None:
        super().__init__()
        self._release = threading.Event()

    def run(self) -> None:
        self._running = True
        self._release.wait(JOIN_TIMEOUT)
        self._running = False

    def stop(self) -> None:
        return None  # deliberately ignores it

    def release(self) -> None:
        self._release.set()


def _build_engine(
    feed: MarketDataFeed,
    *,
    square_off_authority: SquareOffAuthority | None = None,
    expiry: str | None = None,
    notifier: object | None = None,
) -> tuple[TradingEngine, PositionManager]:
    positions = PositionManager(InMemoryGateway(slippage_points=0.0), lots=1)
    engine = TradingEngine(
        EngineConfig(
            timeframe="5m",
            session=SessionConfig(
                timezone="Asia/Kolkata",
                start_time="09:15",
                end_time="15:15",
                square_off_time="15:20",
            ),
        ),
        feed=feed,
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=65), strike_step=50, expiry=expiry
        ),
        strategy=EngineFixtureStrategy(enter_on_candle=1),
        position_manager=positions,
        underlying_security_id=UNDERLYING,
        square_off_authority=square_off_authority,
        notifier=notifier,  # type: ignore[arg-type]
    )
    return engine, positions


def _open_a_position(feed: _BlockingFeed, positions: PositionManager) -> None:
    """Drive the tape far enough that a real position is open."""
    for tick in (
        _tick(UNDERLYING, 24000.0, _ts(9, 16)),
        _tick(UNDERLYING, 24010.0, _ts(9, 21)),  # closes candle #1 -> ENTER
        _tick(CE_CONTRACT, 100.0, _ts(9, 21, 30)),  # fills the pending entry
    ):
        feed.deliver(tick)
    deadline = threading.Event()
    for _ in range(100):  # bounded spin: the feed thread fills asynchronously
        if positions.has_position():
            return
        deadline.wait(0.02)
    raise AssertionError("the tape never opened a position")


# ------------------------------------------------------- 1. structural: no handler
def test_the_engine_module_installs_no_signal_handler() -> None:
    """Enforced structurally, not by convention.

    The same technique Part 2a used for the no-``framework.*``-import rule: read
    the module's AST rather than trusting a grep or a comment. A future edit that
    reintroduces a handler fails here, with the reason attached.
    """
    source = Path(engine_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "signal" not in imported, (
        "common/engine/engine.py imports the signal module. The engine must not "
        "install a handler: the supervisor owns signals for the process, and a "
        "second installer silently wins delivery (runbook §8)."
    )

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "signal"
    ]
    assert calls == [], "common/engine/engine.py calls signal.signal(...)"


# --------------------------------------------- 2. it does not disturb the process
def test_running_the_engine_leaves_the_processes_signal_handlers_untouched() -> None:
    """A control, and a deliberately weak one.

    This passes even against the unfixed engine, because the reference's
    save/restore pair is LIFO and correct — which is precisely why the collision
    was invisible. Checking after ``run()`` has returned can never catch it. The
    test that can is the next one, which looks *during* the run.
    """
    before_int = signal.getsignal(signal.SIGINT)
    before_term = signal.getsignal(signal.SIGTERM)

    engine, _positions = _build_engine(SimulatedFeed([_tick(UNDERLYING, 24000.0, _ts(9, 16))]))
    engine.run()

    assert signal.getsignal(signal.SIGINT) is before_int
    assert signal.getsignal(signal.SIGTERM) is before_term


class _SignallingFeed(MarketDataFeed):
    """Raises ``SIGINT`` from inside ``run()`` — i.e. while the engine is running.

    This is where the collision actually lives. The engine's run loop sits inside
    the owner's handler lifetime, so "who is installed *now*" is the only question
    that matters, and it can only be asked from in here.
    """

    def __init__(self, ticks: Sequence[Tick]) -> None:
        super().__init__()
        self._ticks = list(ticks)
        self.handler_during_run: object = None

    def run(self) -> None:
        self._running = True
        self.handler_during_run = signal.getsignal(signal.SIGINT)
        signal.raise_signal(signal.SIGINT)
        for tick in self._ticks:
            if not self._running:
                break
            self._emit(tick)
        self._running = False


# ------------------------------------- 3. the nesting regression, the real gate
def test_an_engine_run_cannot_displace_the_supervisors_handler() -> None:
    """The collision itself: the owner's handler must still be the live one.

    This is the test that fails against the unfixed port. With the engine
    installing its own ``SIGINT`` handler, the handler live *during* ``run()`` is
    the engine's closure, so the owner's handler never runs and the event it
    exists to set — the supervisor's ordered feed shutdown, the whole of Part 1 —
    stays clear.
    """
    fired = threading.Event()

    def _owner_handler(signum: int, frame: object) -> None:
        fired.set()

    feed = _SignallingFeed([_tick(UNDERLYING, 24000.0, _ts(9, 16))])
    previous = signal.signal(signal.SIGINT, _owner_handler)
    try:
        engine, _positions = _build_engine(feed)
        engine.run()

        # The owner's handler was the one installed while the engine ran...
        assert feed.handler_during_run is _owner_handler, (
            "the engine displaced the owner's SIGINT handler for the duration of "
            "its run; the owner's ordered shutdown would never execute (runbook §8)"
        )
        # ...and it is the one that actually received the signal.
        assert fired.is_set(), "the owner's handler never ran: the engine took delivery"
        # Still installed afterwards, too.
        assert signal.getsignal(signal.SIGINT) is _owner_handler
    finally:
        signal.signal(signal.SIGINT, previous)


# ------------------------------------------------- 4. the cross-thread request
def test_a_square_off_request_from_another_thread_closes_on_the_feed_thread() -> None:
    """The whole point: signal from anywhere, act only where it is safe to act."""
    feed = _BlockingFeed()
    engine, positions = _build_engine(feed)

    runner = threading.Thread(target=engine.run, name="engine-run", daemon=True)
    runner.start()
    _open_a_position(feed, positions)
    assert positions.has_position()

    # The request comes from this thread, which does NOT own the feed.
    engine.request_square_off("test shutdown")
    assert engine.square_off_requested
    # Nothing has been closed yet: the request only set a flag.
    assert positions.has_position()
    assert feed.stop_callers == []

    # The next tick is the boundary at which the engine acts.
    feed.deliver(_tick(CE_CONTRACT, 95.0, _ts(9, 23)))
    runner.join(timeout=JOIN_TIMEOUT)

    assert not runner.is_alive(), "the engine thread never returned after a square-off request"
    assert len(positions.trades) == 1
    assert positions.trades[0].exit_reason is ExitReason.SQUARE_OFF
    assert not positions.has_position()
    # The close was performed by the thread that owns the feed, never by ours.
    # Both halves matter: that it happened exactly once, and that no stop ever
    # originated on the requesting thread — the cross-thread close Part 1 forbids.
    assert feed.stop_callers == ["engine-run"]
    assert threading.current_thread().name not in feed.stop_callers
    assert feed.run_thread == "engine-run"
    assert engine.stopped_by_request
    assert engine.wait_until_stopped(JOIN_TIMEOUT)


# ------------------------------------------------------- 5. the no-tick boundary
def test_a_square_off_request_is_honoured_when_the_feed_returns_without_a_tick() -> None:
    """No tick ever arrives, so ``run()``'s finally is the only boundary left."""
    feed = _BlockingFeed()
    engine, positions = _build_engine(feed)

    runner = threading.Thread(target=engine.run, name="engine-run", daemon=True)
    runner.start()
    _open_a_position(feed, positions)

    engine.request_square_off("no more ticks")
    feed.finish()  # the tape ends; no further tick is ever delivered
    runner.join(timeout=JOIN_TIMEOUT)

    assert not runner.is_alive()
    assert len(positions.trades) == 1
    assert positions.trades[0].exit_reason is ExitReason.SQUARE_OFF


# ----------------------------------------------------------- 6. the residual case
def test_a_feed_that_delivers_nothing_and_never_stops_is_reported_not_hung() -> None:
    """The engine-level twin of runbook limitation 13.

    A connected socket with no traffic has no boundary. The engine cannot square
    off, and the honest behaviour is to say so within a bounded wait rather than
    block its owner forever.
    """
    feed = _SilentFeed()
    engine, _positions = _build_engine(feed)

    runner = threading.Thread(target=engine.run, name="engine-run", daemon=True)
    runner.start()
    engine.request_square_off("shutdown")

    assert engine.wait_until_stopped(0.2) is False
    assert not engine.stopped_by_request

    feed.release()  # let the daemon thread unwind so the suite stays clean
    runner.join(timeout=JOIN_TIMEOUT)


# --------------------------------------------------------------- 7. idempotence
def test_repeated_square_off_requests_close_the_position_once() -> None:
    """A repeated Ctrl-C must not double-close, exactly as the reference guarded."""
    feed = _BlockingFeed()
    engine, positions = _build_engine(feed)

    runner = threading.Thread(target=engine.run, name="engine-run", daemon=True)
    runner.start()
    _open_a_position(feed, positions)

    for _ in range(5):
        engine.request_square_off("impatient operator")
    feed.deliver(_tick(CE_CONTRACT, 95.0, _ts(9, 23)))
    runner.join(timeout=JOIN_TIMEOUT)

    assert not runner.is_alive()
    assert len(positions.trades) == 1


def test_square_off_completion_sends_exactly_one_notification() -> None:
    """Phase 7 Part 2: before this, the engine path notified only on *failure*
    (engine_worker._raise_silent_engine_alarm) — the fixture path's own
    square_off_completed (worker.py's _maybe_square_off) had no engine-side
    counterpart at all."""
    from common.notifications import RecordingNotifier

    feed = _BlockingFeed()
    recorder = RecordingNotifier()
    engine, positions = _build_engine(feed, notifier=recorder)

    runner = threading.Thread(target=engine.run, name="engine-run", daemon=True)
    runner.start()
    _open_a_position(feed, positions)

    for _ in range(3):  # repeated requests must not repeat the notification either
        engine.request_square_off("operator")
    feed.deliver(_tick(CE_CONTRACT, 95.0, _ts(9, 23)))
    runner.join(timeout=JOIN_TIMEOUT)

    completions = [e for e in recorder.events if e.event_type == "square_off_completed"]
    assert len(completions) == 1
    assert "squared off" in completions[0].message


def test_the_engine_reuses_an_already_built_safenotifier_rather_than_double_wrapping() -> None:
    """worker.py builds one SafeNotifier per process and hands it straight
    through (via engine_worker.run_engine) into this constructor. Wrapping it
    again would silently double every success/failure count and apply two
    independent aggregation windows to the same event."""
    from common.notifications import SafeNotifier
    from common.notifications.base import NullNotifier

    already_wrapped = SafeNotifier(NullNotifier(), deferred=False)
    engine, _positions = _build_engine(SimulatedFeed([]), notifier=already_wrapped)

    assert engine.notifier is already_wrapped


def test_a_bare_notifier_is_wrapped_deferred_by_default() -> None:
    """The fallback wrap (no pre-built SafeNotifier supplied) must still
    protect the tick thread — on_tick is exactly where this notifier's
    send() is called from."""
    from common.notifications import SafeNotifier
    from common.notifications.base import RecordingNotifier

    engine, _positions = _build_engine(SimulatedFeed([]), notifier=RecordingNotifier())

    assert isinstance(engine.notifier, SafeNotifier)
    assert engine.notifier.deferred is True
    engine.notifier.close()


def test_request_square_off_never_touches_the_feed_from_the_calling_thread() -> None:
    """The contract in one assertion: it sets a flag and returns.

    Called before ``run()`` has even started, so if it reached the feed at all the
    call would be recorded here.
    """
    feed = _BlockingFeed()
    engine, _positions = _build_engine(feed)

    engine.request_square_off("before the run")

    assert engine.square_off_requested
    assert feed.stop_callers == []
    assert feed.run_thread is None


def test_an_untriggered_run_is_not_reported_as_stopped_by_request() -> None:
    """The control: an ordinary end-of-tape run must not look like a shutdown.

    The mirror of the supervisor test Part 1 added for the same reason — a status
    flag that is always true carries no information.
    """
    ticks: Sequence[Tick] = [_tick(UNDERLYING, 24000.0, _ts(9, 16))]
    engine, _positions = _build_engine(SimulatedFeed(ticks))
    engine.run()

    assert not engine.square_off_requested
    assert not engine.stopped_by_request
    assert engine.wait_until_stopped(0.0)


# -------------------------------------- 8. expiry-driven (Phase 6 Part 4)
def test_an_overdue_expiry_force_closes_an_open_position_end_to_end(tmp_path: Path) -> None:
    """The composed trigger, proven through the real engine and a real database.

    Entry happens on the contract's own expiry date (2026-07-16), which is not
    yet overdue at the default zero-day lead. The position stays open until a
    tick dated the *next* calendar day arrives — at which point ``due()`` must
    return ``True`` immediately, before any time-of-day check, closing the
    position with ``ExitReason.SQUARE_OFF`` rather than leaving it simply
    absent (spec ARCH:1965), and persisting ``square_off_state=COMPLETED`` to
    the same repository row the ordinary time-of-day trigger writes.

    The engine (and the authority behind it, and the SQLite connection behind
    that) is built **inside** the engine-run thread, not handed across from the
    main thread: sqlite3 connections are thread-bound, and every other test in
    this file that reaches a real connection does so from a single thread —
    this is the first to run the engine on a *different* thread while also
    touching a database, so it has to open that database where it will be used.
    """
    database_path = tmp_path / "expiry_e2e.sqlite"
    expiry = "2026-07-16"
    contract_id = f"SIM:NIFTY:{expiry}:24000:CE"
    feed = _BlockingFeed()
    holder: dict[str, object] = {}

    def _run_on_its_own_thread() -> None:
        database = Database(database_path)
        MigrationRunner(database).run_pending()
        repository = ExecutionRepository(database)
        authority = PersistedSquareOffAuthority(
            SquareOffPolicy(),  # default expiry_policy, square_off_before_expiry_days=0
            repository,
            runtime_id="intraday_options",
            strategy_id="expiry_e2e",
            execution_mode=ExecutionMode.PAPER,
            trading_date="2026-07-16",
            expiry=expiry,
        )
        engine, positions = _build_engine(feed, square_off_authority=authority, expiry=expiry)
        holder["engine"] = engine
        holder["positions"] = positions
        holder["authority"] = authority
        try:
            engine.run()
        finally:
            database.close()

    runner = threading.Thread(target=_run_on_its_own_thread, name="engine-run", daemon=True)
    runner.start()
    try:
        for tick in (
            _tick(UNDERLYING, 24000.0, _ts(9, 16)),
            _tick(UNDERLYING, 24010.0, _ts(9, 21)),  # closes candle #1 -> ENTER
            _tick(contract_id, 100.0, _ts(9, 21, 30)),  # fills the pending entry
        ):
            feed.deliver(tick)
        deadline = threading.Event()
        for _ in range(150):
            positions = holder.get("positions")
            if positions is not None and positions.has_position():  # type: ignore[union-attr]
                break
            deadline.wait(0.02)
        else:
            raise AssertionError("the tape never opened a position")

        # A tick dated the day after expiry: overdue, at any time of day.
        next_day = datetime(2026, 7, 17, 9, 16, tzinfo=IST)
        feed.deliver(_tick(UNDERLYING, 24005.0, next_day))
        runner.join(timeout=JOIN_TIMEOUT)
    finally:
        if runner.is_alive():  # pragma: no cover - only on an unexpected hang
            feed.finish()
            runner.join(timeout=JOIN_TIMEOUT)

    assert not runner.is_alive(), "the engine never returned after the overdue tick"
    positions = holder["positions"]
    assert not positions.has_position()  # type: ignore[union-attr]
    assert len(positions.trades) == 1  # type: ignore[union-attr]
    assert positions.trades[0].exit_reason is ExitReason.SQUARE_OFF  # type: ignore[union-attr]

    verify_db = Database(database_path)
    verify_repository = ExecutionRepository(verify_db)
    state = verify_repository.load_strategy_state(
        strategy_id="expiry_e2e", execution_mode=ExecutionMode.PAPER, trading_date="2026-07-16"
    )
    assert state["square_off_state"] == "COMPLETED"
    verify_db.close()


def test_a_feed_that_raises_still_squares_off_before_the_error_propagates() -> None:
    """Unchanged from the reference: an unhandled error must not orphan a position."""

    class _ExplodingFeed(MarketDataFeed):
        def run(self) -> None:
            raise RuntimeError("socket exploded")

    engine, _positions = _build_engine(_ExplodingFeed())
    with pytest.raises(RuntimeError, match="socket exploded"):
        engine.run()
    assert engine._squared_off
