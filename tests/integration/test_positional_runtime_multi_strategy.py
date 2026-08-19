"""Phase 5 (runtime generalization) acceptance suite: two independent
positional strategy workers, spawned as real ``multiprocessing`` child
processes, driven by one real :class:`~common.feed.hub.SharedFeedHub` under
:class:`~runtimes.positional_options.supervisor.PositionalOptionsSupervisor`
— never one Dhan WebSocket per strategy. See
``strategies/positional_options/_fixture_second_strategy/strategy.py``'s
own docstring for why the fixture strategy used here never completes a
real entry, and ``tests/integration/_positional_multi_strategy_fixtures.py``
for why its chain/scrip-master/margin builders are module-level, picklable
functions rather than closures.

No live order API is constructed or called anywhere in this module; the
one shared "adapter" is a fully in-process fake.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from _weekly_delta_neutral_fixtures import OffsetClock

from common.engine.config import SessionConfig
from common.feed.reconnect import ReconnectingFeed
from common.models import Tick
from common.persistence import Database, MigrationRunner
from common.process import worker_lock
from common.process.square_off_requests import square_off_request_path, write_square_off_request
from common.utils.timeutils import now_ist
from runtimes.positional_options.config_adapter import WorkerConfig
from runtimes.positional_options.supervisor import (
    EXIT_OK,
    PositionalOptionsSupervisor,
    PositionalSupervisorConfig,
)
from runtimes.positional_options.worker import EXIT_DUPLICATE

NIFTY_SECURITY_ID = "13"
_FACTORY_MODULE = "_positional_multi_strategy_fixtures"
_STRATEGY_REF = (
    "strategies.positional_options._fixture_second_strategy.strategy:FixtureSecondStrategy"
)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id, instrument=security_id, last_price=price,
        exchange_time=ts, received_at=ts,
    )


class _FakeAdapter:
    """The one shared feed connection every worker in the group sees.

    Deliberately simpler than ``test_engine_over_hub.py``'s own
    ``_PacedAdapter``: neither fixture worker here ever makes a *dynamic*
    runtime subscription (see the module docstring), so there is no
    subscription round trip to pace around — every tick this replays is
    for an instrument already subscribed at ``hub.start()``.
    """

    def __init__(self, ticks: list[Tick]) -> None:
        self._ticks = ticks
        self._running = False
        #: (security_ids, segment, mode) per call — proves the union/dedup
        #: and segment/mode wiring without needing a real broker.
        self.subscribe_calls: list[tuple[tuple[str, ...], int | None, int | None]] = []

    def subscribe(
        self, security_ids: Any, *, segment: int | None = None, mode: int | None = None
    ) -> None:
        self.subscribe_calls.append((tuple(security_ids), segment, mode))

    def request_stop(self) -> None:
        self._running = False

    def stop(self) -> None:
        self._running = False

    def start(self, on_tick: Any, *, on_idle: Any = None) -> None:
        self._running = True
        for tick in self._ticks:
            if not self._running:
                break
            on_tick(tick)
        self._running = False


class _FlakyAdapter:
    """A scripted batch, a disconnect, then a second batch — the real
    socket shape ``test_feed_reconnect.py``'s own ``_ScriptedAdapter``
    already established, reused here (Phase 6A) to prove the supervisor's
    real ``ReconnectingFeed`` wrap survives a genuine reconnect under two
    real spawned workers without either one being disrupted."""

    def __init__(self, *batches: Any) -> None:
        self._batches = list(batches)
        self._running = False
        self.resubscribe_calls = 0

    def subscribe(
        self, security_ids: Any, *, segment: int | None = None, mode: int | None = None
    ) -> None:
        pass

    def resubscribe_all(self) -> None:
        self.resubscribe_calls += 1

    def request_stop(self) -> None:
        self._running = False

    def stop(self) -> None:
        self._running = False

    def start(self, on_tick: Any, *, on_idle: Any = None) -> None:
        self._running = True
        try:
            while self._batches:
                item = self._batches.pop(0)
                if isinstance(item, Exception):
                    raise item
                for tick in item:
                    if not self._running:
                        break
                    on_tick(tick)
                # Deliver, then die — the realistic shape of a dropped
                # socket (same "peek ahead" shape as test_feed_reconnect.
                # py's own _ScriptedAdapter): only actually return (a clean
                # end) if nothing scripted next is an exception; otherwise
                # keep looping so the next iteration raises it.
                if self._batches and isinstance(self._batches[0], Exception):
                    continue
                return
        finally:
            self._running = False


class _SlowAdapter:
    """Delivers its ticks, then stays "connected" (blocking ``start()``)
    for a short, fixed real-time window before returning cleanly — long
    enough for the real spawned child's own ``on_poll`` (fired every
    ``HubTickFeed``'s default ~0.5s, unrelated to any tick) to wake
    several times, so a test can prove something that only happens across
    *repeated* wakes: an operator's square-off request file being noticed,
    or more than one heartbeat row being written."""

    def __init__(self, ticks: list[Tick], *, hold_seconds: float = 2.5) -> None:
        self._ticks = ticks
        self._hold_seconds = hold_seconds
        self._running = False

    def subscribe(
        self, security_ids: Any, *, segment: int | None = None, mode: int | None = None
    ) -> None:
        pass

    def request_stop(self) -> None:
        self._running = False

    def stop(self) -> None:
        self._running = False

    def start(self, on_tick: Any, *, on_idle: Any = None) -> None:
        self._running = True
        for tick in self._ticks:
            if not self._running:
                break
            on_tick(tick)
        deadline = time.monotonic() + self._hold_seconds
        while self._running and time.monotonic() < deadline:
            if on_idle is not None:
                on_idle()
            time.sleep(0.05)
        self._running = False


def _worker_config(
    tmp_path: Path, *, strategy_id: str, database_path: Path
) -> WorkerConfig:
    runtime_root = tmp_path / "runtime"
    return WorkerConfig(
        runtime_id="positional_options",
        strategy_id=strategy_id,
        strategy_ref=_STRATEGY_REF,
        trading_date="2026-08-19",
        lots=1,
        timezone="Asia/Kolkata",
        underlying_security_id=NIFTY_SECURITY_ID,
        underlying_instrument="NIFTY",
        underlying_segment="IDX_I",
        option_segment="NSE_FNO",
        session=SessionConfig(
            timezone="Asia/Kolkata", start_time="09:15", end_time="15:15",
            square_off_time="15:20", holidays=(),
        ),
        risk_free_rate=0.065,
        dividend_yield=0.0,
        quote_max_age_seconds=3600.0,
        evaluation_interval_seconds=0.0,
        max_adjustments_per_day=1,
        max_adjustments_per_cycle=3,
        min_minutes_between_adjustments=90,
        parameters={},
        database_path=database_path,
        lock_dir=runtime_root / "locks",
        pid_dir=runtime_root / "pid",
        log_dir=tmp_path / "logs",
        runtime_root=runtime_root,
        cache_dir=tmp_path / "cache",
        chain_fetcher_factory=f"{_FACTORY_MODULE}:build_fixture_chain_fetcher",
        scrip_master_factory=f"{_FACTORY_MODULE}:fixture_scrip_master",
        margin_fetcher_factory=f"{_FACTORY_MODULE}:build_fixture_margin_fetcher",
    )


def _build_supervisor(
    tmp_path: Path, *, database_path: Path, adapter: _FakeAdapter, clock: Any = None
) -> PositionalOptionsSupervisor:
    runtime_root = tmp_path / "runtime"
    config = PositionalSupervisorConfig(
        runtime_id="positional_options",
        database_path=database_path,
        lock_dir=runtime_root / "locks",
        pid_dir=runtime_root / "pid",
        log_dir=tmp_path / "logs",
        runtime_root=runtime_root,
        cache_dir=tmp_path / "cache",
    )
    if clock is None:
        return PositionalOptionsSupervisor(config, adapter)
    return PositionalOptionsSupervisor(config, adapter, clock=clock)


def _incident_messages(database_path: Path, *, strategy_id: str) -> list[str]:
    db = Database(database_path)
    try:
        conn = db.connect()
        rows = conn.execute(
            "SELECT message FROM errors WHERE strategy_id = ? "
            "AND component = 'positional_multi_leg_engine.incident' ORDER BY id",
            (strategy_id,),
        ).fetchall()
        return [row["message"] for row in rows]
    finally:
        db.close()


def _session_rows(database_path: Path, *, strategy_id: str) -> list[sqlite3.Row]:
    db = Database(database_path)
    try:
        conn = db.connect()
        return list(
            conn.execute(
                "SELECT * FROM runtime_sessions WHERE strategy_id = ? ORDER BY id",
                (strategy_id,),
            ).fetchall()
        )
    finally:
        db.close()


def _heartbeat_rows(database_path: Path, *, strategy_id: str) -> list[sqlite3.Row]:
    db = Database(database_path)
    try:
        conn = db.connect()
        return list(
            conn.execute(
                "SELECT * FROM runtime_heartbeats WHERE strategy_id = ? ORDER BY id",
                (strategy_id,),
            ).fetchall()
        )
    finally:
        db.close()


# =========================================================== two workers
def test_two_workers_share_one_feed_and_are_independently_isolated(tmp_path: Any) -> None:
    """The group's own real acceptance gate: two strategies, one shared
    feed adapter, one shared NIFTY subscription — proves dedup, per-worker
    tick delivery (via each worker's own durable ``errors`` marker),
    independent sessions/heartbeats, and that one worker's induced failure
    (another process already holding its lock) never stops its sibling."""
    database_path = tmp_path / "positional_options.db"
    MigrationRunner(Database(database_path)).run_pending()

    ts = datetime(2026, 8, 19, 9, 26, tzinfo=UTC)
    ticks = [_tick(NIFTY_SECURITY_ID, 24000.0 + i, ts + timedelta(seconds=i)) for i in range(3)]
    adapter = _FakeAdapter(ticks)

    # Phase 6A: context.spot_is_fresh is now time-bounded against the
    # engine's own clock (previously it meant only "a spot tick has ever
    # arrived") — the spawned worker's clock must track this test's own
    # simulated tick timeline, not the real wall clock, or the fixture
    # strategy's fresh-spot marker (asserted below) never fires. Picklable,
    # so it survives the real multiprocessing.Process spawn boundary.
    clock = OffsetClock(offset=ts - now_ist())
    supervisor = _build_supervisor(
        tmp_path, database_path=database_path, adapter=adapter, clock=clock
    )
    alpha = _worker_config(tmp_path, strategy_id="_fixture_alpha", database_path=database_path)
    beta = _worker_config(tmp_path, strategy_id="_fixture_beta", database_path=database_path)
    supervisor.add_worker(alpha)
    supervisor.add_worker(beta)

    # Simulate a real crash-isolation scenario: another process already
    # holds _fixture_alpha's own worker lock (created directly through the
    # same production ProcessLock alpha's own spawned child will try to
    # acquire) before the group ever starts.
    assert alpha.lock_dir is not None and alpha.pid_dir is not None
    blocking_lock = worker_lock(
        runtime_id="positional_options", strategy_id="_fixture_alpha",
        lock_dir=alpha.lock_dir, pid_dir=alpha.pid_dir,
    )
    blocking_lock.acquire()
    try:
        result = supervisor.run()
    finally:
        blocking_lock.release()

    assert result.workers_started == 2
    assert result.worker_exit_codes["_fixture_alpha"] == EXIT_DUPLICATE
    assert result.worker_exit_codes["_fixture_beta"] == 0

    # The union was subscribed exactly once, at hub.start() — NIFTY is
    # both workers' own underlying, so this is the dedup proof: two
    # channels wanting the same instrument never becomes two broker
    # subscriptions.
    initial_calls = [c for c in adapter.subscribe_calls if c[0] == (NIFTY_SECURITY_ID,)]
    assert len(initial_calls) == 1

    # _fixture_beta genuinely ran its engine against real ticks for its
    # own subscription — proven by its own durable, strategy_id-keyed
    # marker (see FixtureSecondStrategy's own docstring), not merely by
    # exit_code 0.
    beta_incidents = _incident_messages(database_path, strategy_id="_fixture_beta")
    assert len(beta_incidents) == 1
    assert f"underlying_security_id={NIFTY_SECURITY_ID}" in beta_incidents[0]
    assert "spot=24000.0" in beta_incidents[0]

    # _fixture_alpha never got far enough to evaluate anything at all —
    # refused before its engine, or any tick delivery, ever existed.
    assert _incident_messages(database_path, strategy_id="_fixture_alpha") == []

    # Each worker holds its own durable session/heartbeat identity — never
    # shared, never merged.
    beta_sessions = _session_rows(database_path, strategy_id="_fixture_beta")
    assert len(beta_sessions) == 1
    assert beta_sessions[0]["process_role"] == "worker"
    assert beta_sessions[0]["ended_at"] is not None  # closed cleanly
    assert _session_rows(database_path, strategy_id="_fixture_alpha") == [], (
        "the duplicate-refused worker never opened its own session at all"
    )
    beta_heartbeats = _heartbeat_rows(database_path, strategy_id="_fixture_beta")
    assert len(beta_heartbeats) >= 1

    # The group's own duplicate-worker report — the mechanism that makes a
    # refused worker visible to an operator rather than a silent
    # zero-length run. A different component than the engine-incident
    # marker query above, so queried separately here.
    db = Database(database_path)
    try:
        conn = db.connect()
        duplicate_report_rows = conn.execute(
            "SELECT message FROM errors WHERE strategy_id = ? "
            "AND component = 'supervisor.duplicate_worker'",
            ("_fixture_alpha",),
        ).fetchall()
    finally:
        db.close()
    assert len(duplicate_report_rows) == 1
    assert "EXIT_DUPLICATE" in duplicate_report_rows[0]["message"]


# ================================================================== N = 1
def test_single_worker_n_equals_1_case_is_unchanged(tmp_path: Any) -> None:
    """N=1 is the exact same machinery with one registered channel — no
    separate single-worker code path (see supervisor.py's own module
    docstring) — proven by running the group with only one worker and
    confirming it behaves exactly like the two-worker case's own surviving
    half: real ticks delivered, its own session/heartbeat, clean exit."""
    database_path = tmp_path / "positional_options.db"
    MigrationRunner(Database(database_path)).run_pending()

    ts = datetime(2026, 8, 19, 9, 26, tzinfo=UTC)
    ticks = [_tick(NIFTY_SECURITY_ID, 24000.0, ts)]
    adapter = _FakeAdapter(ticks)

    # See the two-worker test's own comment: the fixture strategy's
    # fresh-spot marker needs the worker's clock on the same timeline as
    # this test's own simulated tick.
    clock = OffsetClock(offset=ts - now_ist())
    supervisor = _build_supervisor(
        tmp_path, database_path=database_path, adapter=adapter, clock=clock
    )
    solo = _worker_config(tmp_path, strategy_id="_fixture_solo", database_path=database_path)
    supervisor.add_worker(solo)

    result = supervisor.run()

    assert result.workers_started == 1
    assert result.worker_exit_codes["_fixture_solo"] == EXIT_OK
    incidents = _incident_messages(database_path, strategy_id="_fixture_solo")
    assert len(incidents) == 1
    sessions = _session_rows(database_path, strategy_id="_fixture_solo")
    assert len(sessions) == 1 and sessions[0]["ended_at"] is not None


# ================================================ dynamic subscription
def test_dynamic_subscription_routes_to_the_correct_worker_in_full_mode(tmp_path: Any) -> None:
    """The supervisor's own control-queue -> hub.request_subscription
    plumbing (Phase 5's actual new mechanism), proven directly and
    deterministically: a runtime subscription from one worker's control
    queue reaches the real, imported-unchanged SharedFeedHub, is applied
    under the correct exchange segment and Full (21) mode — the option
    side of worker._request_subscription's own segment/mode split — and
    is routed to *that* worker's own channel only, never its sibling's.
    Deliberately not routed through a real spawned child or a real engine
    entry (see FixtureSecondStrategy's own docstring for why that path
    cannot complete a genuinely new subscription's round trip within one
    evaluation) — this is the mechanism itself, tested directly."""
    database_path = tmp_path / "positional_options.db"
    MigrationRunner(Database(database_path)).run_pending()
    adapter = _FakeAdapter([])
    supervisor = _build_supervisor(tmp_path, database_path=database_path, adapter=adapter)

    alpha = _worker_config(tmp_path, strategy_id="_fixture_alpha", database_path=database_path)
    beta = _worker_config(tmp_path, strategy_id="_fixture_beta", database_path=database_path)
    alpha_channel = supervisor.add_worker(alpha)
    beta_channel = supervisor.add_worker(beta)

    option_security_id = "970001"  # the fixture scrip master's own CE contract
    option_segment_code = 2  # NSE_FNO, per common.market_data.scrip_master.SEGMENT_CODES
    full_mode = 21  # common.engine.feed.SubscriptionMode.FULL

    # Exactly what worker._request_subscription puts on the control queue
    # for a non-underlying id — this is the real wire format, not a
    # simplification of it.
    supervisor.control_queue("_fixture_alpha").put_nowait(
        (option_security_id, option_segment_code, full_mode)
    )
    # multiprocessing.Queue.put_nowait() hands off to an internal feeder
    # thread — a get_nowait() issued immediately afterward can genuinely
    # race it and see an empty queue, even within one process (a real,
    # documented quirk of that class, unrelated to anything this phase
    # changed). Production's own drain loop simply runs again on its next
    # ~1s heartbeat poll; here, retried directly and bounded so a real
    # failure to apply still fails the test rather than hanging it.
    for _ in range(50):
        supervisor._drain_control_queues()
        supervisor.hub._apply_pending_subscriptions()
        if alpha_channel.wants(option_security_id):
            break
        time.sleep(0.05)

    assert alpha_channel.wants(option_security_id) is True
    assert beta_channel.wants(option_security_id) is False
    subscribe_calls = [c for c in adapter.subscribe_calls if c[0] == (option_security_id,)]
    assert len(subscribe_calls) == 1
    assert subscribe_calls[0][1] == option_segment_code
    assert subscribe_calls[0][2] == full_mode


# ============================================ Phase 6A: reconnect + isolation
def test_the_supervisor_wraps_its_adapter_in_a_reconnecting_feed(tmp_path: Any) -> None:
    """Direct wiring proof — mirrors ``test_supervisor_health_wiring.py``/
    ``test_candle_gap_policy_wiring.py``'s own established pattern for this
    exact kind of assertion on the intraday supervisor."""
    database_path = tmp_path / "positional_options.db"
    MigrationRunner(Database(database_path)).run_pending()
    adapter = _FakeAdapter([])
    supervisor = _build_supervisor(tmp_path, database_path=database_path, adapter=adapter)

    assert isinstance(supervisor._feed, ReconnectingFeed)
    assert supervisor.hub._adapter is supervisor._feed


def test_two_workers_share_one_feed_and_stay_isolated_through_a_reconnect(tmp_path: Any) -> None:
    """A real scripted disconnect/reconnect cycle (the real default
    ``ReconnectPolicy`` backoff — no override, mirroring
    ``IntradayOptionsSupervisor``'s own precedent of not exposing one) under
    two real spawned worker processes sharing one hub. Both workers'
    sessions/incident markers survive it independently — the reconnect wrap
    never leaks across worker boundaries, and neither worker is disrupted or
    duplicated by it."""
    database_path = tmp_path / "positional_options.db"
    MigrationRunner(Database(database_path)).run_pending()

    t0 = datetime(2026, 8, 19, 9, 26, tzinfo=UTC)
    first_batch = [_tick(NIFTY_SECURITY_ID, 24000.0, t0)]
    second_batch = [_tick(NIFTY_SECURITY_ID, 24001.0, t0 + timedelta(seconds=5))]
    adapter = _FlakyAdapter(first_batch, ConnectionResetError("scripted drop"), second_batch)

    clock = OffsetClock(offset=t0 - now_ist())
    supervisor = _build_supervisor(
        tmp_path, database_path=database_path, adapter=adapter, clock=clock
    )
    alpha = _worker_config(tmp_path, strategy_id="_fixture_alpha", database_path=database_path)
    beta = _worker_config(tmp_path, strategy_id="_fixture_beta", database_path=database_path)
    supervisor.add_worker(alpha)
    supervisor.add_worker(beta)

    result = supervisor.run()

    assert result.workers_started == 2
    assert result.worker_exit_codes["_fixture_alpha"] == EXIT_OK
    assert result.worker_exit_codes["_fixture_beta"] == EXIT_OK
    assert adapter.resubscribe_calls >= 1, "the scripted drop must have actually reconnected"

    # Each worker's own fresh-spot marker fired (from the first batch, before
    # the drop) and its own session ended cleanly — independently keyed by
    # its own strategy_id, proving the reconnect never wedged or
    # cross-contaminated either sibling.
    for strategy_id in ("_fixture_alpha", "_fixture_beta"):
        incidents = _incident_messages(database_path, strategy_id=strategy_id)
        assert len(incidents) == 1
        sessions = _session_rows(database_path, strategy_id=strategy_id)
        assert len(sessions) == 1 and sessions[0]["ended_at"] is not None


# =========================== Phase 6A gap-closing: operator square-off + heartbeat
def test_operator_square_off_is_noticed_and_heartbeats_are_written_periodically(
    tmp_path: Any,
) -> None:
    """Before this fix, a real spawned positional worker's own square-off-
    request-file polling and periodic liveness heartbeat both ran on a
    separate background thread that reused the worker's own sqlite3
    connection from a different thread than the one that opened it —
    which sqlite3 refuses outright, so the thread crashed, uncaught, on
    its very first wake (5s in). Operator square-off had therefore never
    actually worked for this runtime, and a heartbeat was written exactly
    once (at start) for the life of any real run. Both are now driven off
    the correctly-threaded ``on_poll`` callback instead — this proves both
    directly, through a real spawned child process, not a unit-level
    stand-in."""
    database_path = tmp_path / "positional_options.db"
    MigrationRunner(Database(database_path)).run_pending()

    t0 = datetime(2026, 8, 19, 9, 26, tzinfo=UTC)
    adapter = _SlowAdapter([_tick(NIFTY_SECURITY_ID, 24000.0, t0)], hold_seconds=2.5)

    clock = OffsetClock(offset=t0 - now_ist())
    supervisor = _build_supervisor(
        tmp_path, database_path=database_path, adapter=adapter, clock=clock
    )
    solo = _worker_config(tmp_path, strategy_id="_fixture_solo", database_path=database_path)
    supervisor.add_worker(solo)

    assert solo.runtime_root is not None
    request_file = square_off_request_path(solo.runtime_root, "positional_options", "_fixture_solo")
    write_square_off_request(request_file, requested_by="test-operator", reason="phase 6a proof")

    result = supervisor.run()

    assert result.worker_exit_codes["_fixture_solo"] == EXIT_OK

    # Noticed and honoured: FixtureSecondStrategy never opens a cycle, so
    # "square off" completes immediately once request_square_off is ever
    # called at all — the request file is cleared and an audit row is
    # written only on that path (worker.py's own post-run check).
    assert not request_file.exists(), "the request file should have been cleared"
    db = Database(database_path)
    try:
        conn = db.connect()
        audit_rows = conn.execute(
            "SELECT * FROM audit_events WHERE strategy_id = ? AND action = 'square_off_completed'",
            ("_fixture_solo",),
        ).fetchall()
        assert len(audit_rows) == 1
        heartbeat_rows = conn.execute(
            "SELECT * FROM runtime_heartbeats WHERE strategy_id = ? ORDER BY id",
            ("_fixture_solo",),
        ).fetchall()
    finally:
        db.close()
    # More than the single start-of-run beat — proof the periodic beat
    # inside _evaluate actually ran repeatedly across the ~2.5s the feed
    # stayed connected, not just once.
    assert len(heartbeat_rows) >= 2

