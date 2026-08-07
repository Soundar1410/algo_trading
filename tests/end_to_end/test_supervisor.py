"""The supervisor: real spawned worker processes over real IPC queues."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from common.config.models import ExecutionMode
from common.market_data import RecordedFeedAdapter, load_tick_tape
from common.persistence import Database
from runtimes.intraday_options.supervisor import (
    IntradayOptionsSupervisor,
    SupervisorConfig,
)
from runtimes.intraday_options.worker import EngineWorkerConfig, WorkerConfig

RUNTIME_ID = "intraday_options"
SECURITY_ID = "99926000"
TRADING_DATE = "2026-07-29"


@pytest.fixture
def supervisor_config(runtime_dirs: dict[str, Path], database_path: Path) -> SupervisorConfig:
    return SupervisorConfig(
        runtime_id=RUNTIME_ID,
        database_path=database_path,
        lock_dir=runtime_dirs["lock_dir"],
        pid_dir=runtime_dirs["pid_dir"],
        log_dir=runtime_dirs["log_dir"],
    )


def _worker(config: SupervisorConfig, strategy_id: str, **overrides) -> WorkerConfig:
    return WorkerConfig(
        runtime_id=RUNTIME_ID,
        strategy_id=strategy_id,
        security_id=SECURITY_ID,
        instrument="NIFTY",
        database_path=config.database_path,
        lock_dir=config.lock_dir,
        pid_dir=config.pid_dir,
        log_dir=config.log_dir,
        trading_date=TRADING_DATE,
        **overrides,
    )


def test_the_supervisor_spawns_a_worker_that_trades(
    supervisor_config, tick_tape_path, database_path
):
    """One live-shaped run: feed → hub → IPC queue → child process → SQLite."""
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    supervisor.add_worker(_worker(supervisor_config, "skelfix"))

    result = supervisor.run()

    assert result.workers_started == 1
    assert result.ticks_received == 24
    assert result.candles_published == 6
    assert result.worker_exit_codes["skelfix"] == 0

    conn = Database(database_path).connect()
    assert conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1


def test_two_workers_receive_identical_bars(supervisor_config, tick_tape_path, database_path):
    # Deliberately non-colliding correlation-ID prefixes ("alph"/"brav") —
    # this test's own property is "identical bars reach both workers", not
    # the admission-time token-collision guard (see
    # test_a_worker_whose_correlation_token_collides_is_refused below for
    # that, discovered via this test using "skelone"/"skeltwo" originally
    # and crashing on order_intents.correlation_id's UNIQUE constraint).
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    supervisor.add_worker(_worker(supervisor_config, "alphaskel"))
    supervisor.add_worker(_worker(supervisor_config, "bravoskel"))

    result = supervisor.run()

    assert result.workers_started == 2
    assert set(result.worker_exit_codes.values()) == {0}

    conn = Database(database_path).connect()
    rows = conn.execute(
        """
        SELECT strategy_id, candle_end_at, candle_close FROM signals
        WHERE side = 'BUY' ORDER BY strategy_id
        """
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["candle_end_at"] == rows[1]["candle_end_at"]
    assert rows[0]["candle_close"] == rows[1]["candle_close"]


def test_a_worker_whose_correlation_token_collides_is_refused_not_crashed(
    supervisor_config, tick_tape_path, database_path
):
    """D78: "skelone"/"skeltwo" both reduce to the correlation-ID token
    "skel" (common.execution.correlation.strategy_token, first four
    alphanumeric characters). Before this guard existed, the second
    strategy's first order raised sqlite3.IntegrityError on order_intents.
    correlation_id's UNIQUE constraint — a worker crash, not a controlled
    refusal — discovered via test_two_workers_receive_identical_bars using
    exactly this pair of names. add_worker now catches it at admission,
    the same way it already catches a live-mode strategy the gate blocks.
    """
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    first = supervisor.add_worker(_worker(supervisor_config, "skelone"))
    second = supervisor.add_worker(_worker(supervisor_config, "skeltwo"))

    assert first is not None
    assert second is None  # refused, not spawned

    result = supervisor.run()

    assert result.workers_started == 1
    assert "skeltwo" not in result.worker_exit_codes

    conn = Database(database_path).connect()
    row = conn.execute(
        "SELECT severity, component, message FROM errors WHERE strategy_id = 'skeltwo'"
    ).fetchone()
    assert row is not None
    assert row["severity"] == "ERROR"
    assert row["component"] == "supervisor.correlation_token_collision"
    assert "skelone" in row["message"]
    # The admitted strategy is completely unaffected.
    assert conn.execute(
        "SELECT COUNT(*) FROM fills WHERE strategy_id = 'skelone'"
    ).fetchone()[0] == 2


def test_no_events_are_dropped_at_normal_volume(supervisor_config, tick_tape_path):
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    supervisor.add_worker(_worker(supervisor_config, "skelfix"))

    result = supervisor.run()
    assert result.dropped_events["skelfix"] == 0


def test_a_run_that_ends_on_its_own_is_not_reported_as_signalled(supervisor_config, tick_tape_path):
    """An exhausted tape is not a shutdown signal, and must not be logged as one.

    Worth its own test because the tempting implementation — "the feed thread is
    still alive, so we must have been signalled" — is wrong: a thread that has
    just finished its work is briefly still alive while it unwinds, so an ordinary
    end-of-tape run would report itself as signalled about as often as the timing
    happened to fall that way.
    """
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    supervisor.add_worker(_worker(supervisor_config, "skelfix"))

    result = supervisor.run()

    assert result.stopped_by_signal is False
    assert result.clean_feed_shutdown is True


def test_a_live_mode_worker_is_refused_and_never_spawned(supervisor_config, tick_tape_path):
    """A live worker is refused individually — add_worker never raises for it."""
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)

    channel = supervisor.add_worker(
        _worker(supervisor_config, "livestrat", execution_mode=ExecutionMode.LIVE)
    )
    assert channel is None


def test_a_blocked_live_worker_does_not_stop_the_paper_strategy(
    supervisor_config, tick_tape_path, database_path
):
    """Mixed-mode gate: the live strategy is blocked, the paper strategy trades on."""
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    supervisor.add_worker(_worker(supervisor_config, "skelfix"))
    supervisor.add_worker(
        _worker(supervisor_config, "livestrat", execution_mode=ExecutionMode.LIVE)
    )

    result = supervisor.run()

    assert result.workers_started == 1
    assert result.worker_exit_codes == {"skelfix": 0}
    assert "livestrat" not in result.worker_exit_codes

    conn = Database(database_path).connect()
    assert conn.execute(
        "SELECT COUNT(*) FROM fills WHERE strategy_id = 'skelfix'"
    ).fetchone()[0] == 2
    # The blocked strategy traded in no mode at all — see the mode-separation
    # end-to-end test for the exhaustive, schema-driven version of this check.
    for table in ("signals", "order_intents", "orders", "fills", "positions"):
        assert (
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE strategy_id = 'livestrat'"
            ).fetchone()[0]
            == 0
        )
    blocked_errors = conn.execute(
        "SELECT message FROM errors WHERE strategy_id = 'livestrat'"
    ).fetchall()
    assert len(blocked_errors) == 1
    assert "NOT rerouted to paper" in blocked_errors[0]["message"]


def test_a_duplicate_worker_is_reported_not_silent(
    supervisor_config, tick_tape_path, database_path
):
    """Phase 7 Part 4: EXIT_DUPLICATE has been recorded into worker_exit_codes
    since Phase 3 Part 2b-ii-B-2 but nothing ever inspected it — a refused
    worker was a silent zero-length run. This is what "act on it" means: an
    errors row and a notification, the same pattern the mixed-mode live-gate
    refusal above already uses.

    The pre-held lock is real, not simulated: worker_lock's flock exclusion
    works the same way against a lock this test process holds directly as it
    does against a second OS process (test_a_second_lock_on_the_same_identity
    _is_refused already proves the mechanism itself; this proves the
    supervisor notices when its own spawned child hits it).
    """
    from common.notifications import RecordingNotifier
    from common.process import worker_lock

    held = worker_lock(
        runtime_id=RUNTIME_ID,
        strategy_id="skelfix",
        lock_dir=supervisor_config.lock_dir,
        pid_dir=supervisor_config.pid_dir,
    ).acquire()
    try:
        adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
        notifier = RecordingNotifier()
        supervisor = IntradayOptionsSupervisor(supervisor_config, adapter, notifier=notifier)
        supervisor.add_worker(_worker(supervisor_config, "skelfix"))

        result = supervisor.run()
    finally:
        held.release()

    from runtimes.intraday_options.worker import EXIT_DUPLICATE

    assert result.worker_exit_codes["skelfix"] == EXIT_DUPLICATE

    conn = Database(database_path).connect()
    row = conn.execute(
        "SELECT severity, component, message FROM errors WHERE strategy_id = 'skelfix'"
    ).fetchone()
    assert row is not None
    assert row["severity"] == "CRITICAL"
    assert row["component"] == "supervisor.duplicate_worker"
    assert "did not start" in row["message"]

    duplicate_events = [e for e in notifier.events if e.event_type == "duplicate_worker_refused"]
    assert len(duplicate_events) == 1
    assert duplicate_events[0].strategy_id == "skelfix"


def test_the_database_is_consistent_after_a_supervised_run(
    supervisor_config, tick_tape_path, database_path
):
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    supervisor.add_worker(_worker(supervisor_config, "skelfix"))
    supervisor.run()

    database = Database(database_path)
    assert database.integrity_check() == []
    assert database.foreign_key_check() == []
    assert database.journal_mode() == "wal"


# ------------------------------------------------- tick channel (Part 2b-ii-A)
def test_a_worker_gets_no_tick_channel_unless_it_asks(supervisor_config, tick_tape_path):
    """Opt-in: the default run is exactly the run it was before this part."""
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    channel = supervisor.add_worker(_worker(supervisor_config, "skelfix"))

    assert channel.tick_queue is None
    assert supervisor.control_queue("skelfix") is None

    result = supervisor.run()

    assert result.worker_exit_codes["skelfix"] == 0
    assert supervisor.hub.ticks_published == 0


def test_an_opted_in_worker_gets_a_tick_queue_and_a_control_queue(
    supervisor_config, tick_tape_path
):
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    channel = supervisor.add_worker(_worker(supervisor_config, "skelfix"), tick_channel=True)

    assert channel.tick_queue is not None
    assert channel.tick_queue.max_depth == supervisor_config.tick_queue_depth
    assert supervisor.control_queue("skelfix") is not None

    result = supervisor.run()

    assert result.worker_exit_codes["skelfix"] == 0
    # Every tick on the tape reached the tick channel as well as the aggregator.
    assert supervisor.hub.ticks_published == 24


class _LiveishAdapter:
    """Replays a tape, then keeps the underlying ticking for a short while.

    The recorded tape is 24 ticks and finishes in microseconds, so a supervisor
    driven by it never completes a single heartbeat-loop iteration — there is no
    live feed left for a runtime subscription to be applied against. That is an
    artefact of an instant tape, not of the design: during market hours frames
    arrive continuously. This double supplies that continuity, bounded so a
    failure is an assertion rather than a hang.
    """

    def __init__(self, ticks, *, extra: int = 300, interval: float = 0.01) -> None:
        self._ticks = list(ticks)
        self._extra = extra
        self._interval = interval
        self._subscribed: set[str] = set()
        self._running = False
        self._stop = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def subscribed(self) -> frozenset[str]:
        return frozenset(self._subscribed)

    def subscribe(self, security_ids, *, segment=None) -> None:
        self._subscribed.update(security_ids)

    def request_stop(self) -> None:
        self._stop.set()
        self._running = False

    def stop(self) -> None:
        self.request_stop()

    def start(self, on_tick) -> None:
        self._running = True
        for tick in self._ticks:
            if not self._running:
                break
            if tick.security_id in self._subscribed:
                on_tick(tick)

        last = self._ticks[-1]
        for i in range(1, self._extra + 1):
            if self._stop.wait(timeout=self._interval):
                break
            on_tick(
                replace(
                    last,
                    exchange_time=last.exchange_time + timedelta(microseconds=i),
                )
            )
        self._running = False


def test_a_subscription_request_on_the_control_queue_reaches_the_hub(
    supervisor_config, tick_tape_path
):
    """The upstream hop: child → control queue → supervisor → hub → feed thread."""
    adapter = _LiveishAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    channel = supervisor.add_worker(_worker(supervisor_config, "skelfix"), tick_channel=True)

    control = supervisor.control_queue("skelfix")
    assert control is not None
    control.put("45678")

    supervisor.run()

    assert "45678" in channel.dynamic_ids
    assert "45678" in adapter.subscribed
    assert supervisor.hub.subscriptions_applied == 1


def test_a_malformed_subscription_request_does_not_kill_the_group(
    supervisor_config, tick_tape_path
):
    """A bad child is the child's problem, not the whole runtime's."""
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    supervisor.add_worker(_worker(supervisor_config, "skelfix"), tick_channel=True)

    control = supervisor.control_queue("skelfix")
    assert control is not None
    control.put(None)
    control.put(12345)
    control.put("")

    result = supervisor.run()

    assert result.worker_exit_codes["skelfix"] == 0
    assert supervisor.hub.subscriptions_applied == 0


def test_undelivered_ticks_do_not_wedge_the_supervisors_exit(supervisor_config, tick_tape_path):
    """A defect found in Part 2b-ii-A, and the reason ``_release_queues`` exists.

    A ``multiprocessing.Queue`` joins its feeder thread at interpreter exit, so a
    producer holding undelivered events behind a full pipe never exits — measured
    at ~65 KB, which the tick channel reaches in a few hundred ticks. The candle
    channel never came close; the tick channel carries ~100x the volume, and in
    Part 2b-ii-A nothing consumes it yet.

    Pre-fix this run hung with no error and no exit code. There is no assertion
    that can express "did not hang" more directly than reaching the end.
    """
    adapter = _LiveishAdapter(load_tick_tape(tick_tape_path), extra=600, interval=0.001)
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    channel = supervisor.add_worker(_worker(supervisor_config, "skelfix"), tick_channel=True)

    result = supervisor.run()

    assert channel.tick_queue is not None
    # Well past the ~65 KB that wedged it, and none of it was consumed.
    assert channel.tick_queue.published > 400
    assert result.worker_exit_codes["skelfix"] == 0


# ------------------------------------------- delivering the queues (Part 2b-ii-B-2)
ENGINE_STRATEGY = "strategies.intraday_options.engine_fixture_strategy:EngineFixtureStrategy"


def _engine_worker(config: SupervisorConfig, strategy_id: str) -> WorkerConfig:
    return _worker(
        config,
        strategy_id,
        idle_timeout_seconds=3.0,
        engine=EngineWorkerConfig(
            strategy_ref=ENGINE_STRATEGY,
            # One-minute bars, because that is what the recorded tape's six buckets
            # produce; the entry then fires on the first completed one.
            timeframe="1m",
            strategy_kwargs={"enter_on_candle": 1},
            lot_size=50,
            strike_step=50,
            feed_poll_seconds=0.05,
        ),
    )


def test_the_tick_channel_gets_the_shutdown_sentinel_too(supervisor_config, tick_tape_path):
    """Built in Part 2b-ii-A, unreachable as deployed until Part 2b-ii-B-2.

    ``HubTickFeed`` turns this sentinel into ``engine.request_square_off()`` — the
    path by which a ``SIGTERM`` delivered only to the supervisor closes each child's
    positions. Until this part only the **candle** queue was ever sentinelled, so
    that path could not fire in the deployed shape no matter how correct it was.

    A fixture worker is used deliberately: it never drains the tick queue, so what
    the supervisor published is still there to be counted.
    """
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    channel = supervisor.add_worker(_worker(supervisor_config, "skelfix"), tick_channel=True)

    supervisor.run()

    assert channel.tick_queue is not None
    assert channel.tick_queue.published == supervisor.hub.ticks_published + 1, (
        "exactly one non-tick item — the shutdown sentinel — must reach the tick channel"
    )


def test_tick_drops_are_reported_apart_from_candle_drops(supervisor_config, tick_tape_path):
    """One key per channel, because the two mean different things.

    A dropped tick latches that worker's entries off for the day (limitation 14); a
    dropped candle does not. Summing them into one number would hide which happened.
    """
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    supervisor.add_worker(_worker(supervisor_config, "ticks01"), tick_channel=True)
    supervisor.add_worker(_worker(supervisor_config, "plain01"))

    result = supervisor.run()

    assert result.dropped_events["ticks01"] == 0
    assert result.dropped_events["ticks01:ticks"] == 0
    # A worker with no tick channel has nothing to report about one.
    assert result.dropped_events["plain01"] == 0
    assert "plain01:ticks" not in result.dropped_events


def test_an_engine_child_receives_both_queues_and_uses_them(
    supervisor_config, tick_tape_path, database_path
):
    """The whole hop, across a real process boundary, with the real engine.

    ``subscriptions_applied`` is the assertion that carries the weight: the child can
    only ask for a contract if it *received ticks on the tick queue*, built a candle,
    got a signal, and had a *control queue* to send the request back on. One number
    proves both deliveries and the engine running between them.

    Before this part the child was spawned with the candle queue alone, so an engine
    worker would have sat on an empty feed until its idle timeout.
    """
    adapter = _LiveishAdapter(load_tick_tape(tick_tape_path), extra=800, interval=0.01)
    supervisor = IntradayOptionsSupervisor(supervisor_config, adapter)
    channel = supervisor.add_worker(
        _engine_worker(supervisor_config, "engine01"), tick_channel=True
    )

    result = supervisor.run()

    assert result.worker_exit_codes["engine01"] == 0
    assert supervisor.hub.subscriptions_applied >= 1, (
        "the child never asked for a contract, so it received no ticks or had no "
        "control queue to answer on"
    )
    assert channel.dynamic_ids, "no contract was chosen at runtime"
    # The chosen contract is one the configuration never mentioned.
    assert all(chosen not in channel.security_ids for chosen in channel.dynamic_ids)
    assert "engine01:ticks" in result.dropped_events

    # And the child really did run the engine to completion, in its own process.
    state = (
        Database(database_path)
        .connect()
        .execute("SELECT payload FROM strategy_state WHERE strategy_id = ?", ("engine01",))
        .fetchone()
    )
    assert state is not None and state["payload"], "the engine wrote no end-of-day state"
    assert "day_summary" in state["payload"]
