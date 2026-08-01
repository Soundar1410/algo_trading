"""The continuity policy actually runs (Phase 4 Part 3, limitation 4).

`tests/unit/test_candle_continuity.py` proves the policy's *decisions*. This
proves the two places it could have shipped unreachable:

1. **The engine acts on the mark.** A stitched bar must not reach indicators or
   produce a signal — it must reach `on_candle_gap` instead.
2. **`on_feed_gap` is wired.** Before Part 3, `ReconnectingFeed` had no
   constructor call outside tests: the supervisor handed the raw adapter to
   `SharedFeedHub`, so `mark_feed_gap` — the whole of limitation 4's existing
   mitigation — was never called in the deployed runtime. A continuity policy
   nothing invokes is the docstring-without-code failure Phase 3 already found
   in `hub.py`.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from common.engine.config import EngineConfig, SessionConfig
from common.engine.engine import TradingEngine
from common.engine.feed import SimulatedFeed
from common.engine.positions import InMemoryGateway, PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.feed.reconnect import ReconnectingFeed
from common.indicators.base import OHLC
from common.models import Tick
from strategies.intraday_options.engine_fixture_strategy import EngineFixtureStrategy

IST = ZoneInfo("Asia/Kolkata")
UNDERLYING = "INDEX"


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


class _RecordingStrategy(EngineFixtureStrategy):
    """Counts which of the two candle paths each bar took."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.clean_bars: list[OHLC] = []
        self.gapped_bars: list[OHLC] = []

    def on_candle(self, candle: OHLC, timestamp: datetime):  # type: ignore[override]
        self.clean_bars.append(candle)
        return super().on_candle(candle, timestamp)

    def on_candle_gap(self, candle: OHLC, timestamp: datetime) -> None:
        self.gapped_bars.append(candle)


def _engine(ticks, strategy):
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
        feed=SimulatedFeed(ticks),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=65), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=UNDERLYING,
    )
    return engine, positions


# ------------------------------------------------------- the engine acts on it
def test_a_stitched_bar_reaches_the_gap_hook_and_not_the_candle_hook():
    """The bar exists — this builder cannot discard — so the engine's refusal to
    trade on it is the whole mitigation."""
    strategy = _RecordingStrategy(enter_on_candle=1)
    # 09:16 then 09:41: buckets 09:15 and 09:40, so 09:20/09:25/09:30/09:35 are
    # all empty. The 09:15 bar is stitched.
    ticks = [
        _tick(UNDERLYING, 24000.0, _ts(9, 16)),
        _tick(UNDERLYING, 24010.0, _ts(9, 41)),
    ]
    engine, _positions = _engine(ticks, strategy)
    engine.run()

    assert len(strategy.gapped_bars) == 1
    assert strategy.gapped_bars[0].spans_gap is True
    assert strategy.clean_bars == [], "a stitched bar was handed to the strategy as clean"


def test_a_stitched_bar_produces_no_position():
    """The fixture strategy enters on its first candle. If the stitched bar had
    reached it, this would open a position on data assembled across a hole."""
    strategy = _RecordingStrategy(enter_on_candle=1)
    ticks = [
        _tick(UNDERLYING, 24000.0, _ts(9, 16)),
        _tick(UNDERLYING, 24010.0, _ts(9, 41)),
    ]
    engine, positions = _engine(ticks, strategy)
    engine.run()

    assert positions.has_position() is False
    assert positions.trades == []


def test_a_clean_stream_takes_the_normal_path_untouched():
    """The negative control: without it, a hook that swallowed *every* bar would
    pass both tests above."""
    strategy = _RecordingStrategy(enter_on_candle=1)
    ticks = [
        _tick(UNDERLYING, 24000.0, _ts(9, 16)),
        _tick(UNDERLYING, 24005.0, _ts(9, 21)),
        _tick(UNDERLYING, 24010.0, _ts(9, 26)),
    ]
    engine, _positions = _engine(ticks, strategy)
    engine.run()

    assert strategy.gapped_bars == []
    assert len(strategy.clean_bars) == 2
    assert all(bar.spans_gap is False for bar in strategy.clean_bars)


def test_a_gap_partway_through_skips_only_the_stitched_bar():
    """Trading resumes on the next clean bar rather than latching off."""
    strategy = _RecordingStrategy(enter_on_candle=99)  # never enters; we count bars
    ticks = [
        _tick(UNDERLYING, 24000.0, _ts(9, 16)),
        _tick(UNDERLYING, 24005.0, _ts(9, 21)),  # closes 09:15, clean
        _tick(UNDERLYING, 24010.0, _ts(9, 46)),  # closes 09:20, stitched
        _tick(UNDERLYING, 24015.0, _ts(9, 51)),  # closes 09:45, clean again
    ]
    engine, _positions = _engine(ticks, strategy)
    engine.run()

    assert len(strategy.gapped_bars) == 1
    assert len(strategy.clean_bars) == 2


# -------------------------------------------------------- on_feed_gap is wired
def test_the_supervisor_wraps_its_adapter_in_the_reconnect_layer(runtime_dirs, database_path):
    """Before Part 3 this was a raw adapter, so nothing below could ever run."""
    from common.market_data.recorded import RecordedFeedAdapter
    from runtimes.intraday_options.supervisor import IntradayOptionsSupervisor, SupervisorConfig

    supervisor = IntradayOptionsSupervisor(
        SupervisorConfig(
            runtime_id="intraday_options",
            database_path=database_path,
            lock_dir=runtime_dirs["lock_dir"],
            pid_dir=runtime_dirs["pid_dir"],
            log_dir=runtime_dirs["log_dir"],
        ),
        adapter=RecordedFeedAdapter([]),
    )
    assert isinstance(supervisor._feed, ReconnectingFeed)
    assert supervisor.hub._adapter is supervisor._feed


def test_a_feed_drop_reaches_the_hubs_aggregators_through_the_supervisors_own_feed(
    runtime_dirs, database_path
):
    """The end of the chain: `ReconnectingFeed` -> `on_feed_gap` ->
    `hub.mark_feed_gap` -> every aggregator. Proven on the supervisor's own
    wiring, not a hand-assembled one."""
    from common.feed.hub import WorkerChannel
    from common.feed.queues import BoundedWorkerQueue
    from common.market_data.recorded import RecordedFeedAdapter
    from runtimes.intraday_options.supervisor import IntradayOptionsSupervisor, SupervisorConfig

    supervisor = IntradayOptionsSupervisor(
        SupervisorConfig(
            runtime_id="intraday_options",
            database_path=database_path,
            lock_dir=runtime_dirs["lock_dir"],
            pid_dir=runtime_dirs["pid_dir"],
            log_dir=runtime_dirs["log_dir"],
        ),
        adapter=RecordedFeedAdapter([]),
    )
    hub = supervisor.hub
    hub.register(
        WorkerChannel(
            strategy_id="s1",
            security_ids=frozenset({UNDERLYING}),
            queue=BoundedWorkerQueue.in_process("s1"),
        )
    )
    # Open a bar so there is something for the outage to invalidate.
    hub.on_tick(_tick(UNDERLYING, 24000.0, _ts(9, 16)))

    # Drive the hook the reconnect layer calls on a disconnect.
    discarded = supervisor._feed._on_feed_gap()  # type: ignore[misc]

    assert discarded == 1, "the outage did not reach the hub's aggregators"
    assert hub.gap_candles_discarded == 1


def test_the_wrapper_does_not_break_a_recorded_tape(runtime_dirs, database_path):
    """`ReconnectingFeed._run` treats a clean return as 'the tape is finished'
    rather than a failure to retry. If that ever changes, every recorded test
    turns into an infinite replay — so it is asserted here rather than trusted."""
    from common.market_data.recorded import RecordedFeedAdapter

    tape = [
        _tick(UNDERLYING, 24000.0, _ts(9, 16)),
        _tick(UNDERLYING, 24005.0, _ts(9, 21)),
    ]
    adapter = RecordedFeedAdapter(tape)
    feed = ReconnectingFeed(adapter)
    feed.subscribe([UNDERLYING])

    seen: list[Tick] = []
    feed.start(seen.append)  # must return, not loop

    assert len(seen) == len(tape)
    assert feed.health.reconnect_count == 0
    assert feed.health.failed_attempts == 0


def test_the_hub_keeps_one_aggregator_across_the_outage(runtime_dirs, database_path):
    """`_published` is what makes duplicate publication structurally impossible
    across a reconnect, and it lives on the aggregator instance."""
    from common.feed.hub import WorkerChannel
    from common.feed.queues import BoundedWorkerQueue
    from common.market_data.recorded import RecordedFeedAdapter
    from runtimes.intraday_options.supervisor import IntradayOptionsSupervisor, SupervisorConfig

    supervisor = IntradayOptionsSupervisor(
        SupervisorConfig(
            runtime_id="intraday_options",
            database_path=database_path,
            lock_dir=runtime_dirs["lock_dir"],
            pid_dir=runtime_dirs["pid_dir"],
            log_dir=runtime_dirs["log_dir"],
        ),
        adapter=RecordedFeedAdapter([]),
    )
    hub = supervisor.hub
    hub.register(
        WorkerChannel(
            strategy_id="s1",
            security_ids=frozenset({UNDERLYING}),
            queue=BoundedWorkerQueue.in_process("s1"),
        )
    )
    hub.on_tick(_tick(UNDERLYING, 24000.0, _ts(9, 16)))
    before = hub._aggregators[UNDERLYING]

    supervisor._feed._on_feed_gap()  # type: ignore[misc]
    hub.on_tick(_tick(UNDERLYING, 24010.0, _ts(9, 26)))

    assert hub._aggregators[UNDERLYING] is before, "the aggregator was rebuilt across a gap"


def test_a_gapped_interval_publishes_no_candle_to_a_worker(runtime_dirs, database_path):
    from common.feed.hub import WorkerChannel
    from common.feed.queues import BoundedWorkerQueue
    from common.market_data.recorded import RecordedFeedAdapter
    from runtimes.intraday_options.supervisor import IntradayOptionsSupervisor, SupervisorConfig

    supervisor = IntradayOptionsSupervisor(
        SupervisorConfig(
            runtime_id="intraday_options",
            database_path=database_path,
            lock_dir=runtime_dirs["lock_dir"],
            pid_dir=runtime_dirs["pid_dir"],
            log_dir=runtime_dirs["log_dir"],
        ),
        adapter=RecordedFeedAdapter([]),
    )
    hub = supervisor.hub
    queue = BoundedWorkerQueue.in_process("s1")
    hub.register(WorkerChannel(strategy_id="s1", security_ids=frozenset({UNDERLYING}), queue=queue))
    hub.on_tick(_tick(UNDERLYING, 24000.0, _ts(9, 16)))
    supervisor._feed._on_feed_gap()  # type: ignore[misc]
    hub.on_tick(_tick(UNDERLYING, 24010.0, _ts(9, 26)))

    assert queue.published == 0, "a bar spanning the outage was fanned out"
