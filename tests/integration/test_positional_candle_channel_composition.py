"""The positional runtime's *real* channel composition never takes candles.

Requirement of this fix: the guarantee must be pinned against
:meth:`~runtimes.positional_options.supervisor.PositionalOptionsSupervisor.add_worker`
itself, not against a hand-built :class:`~common.feed.hub.WorkerChannel`. Every
existing fixture in ``tests/`` constructs ``WorkerChannel(...)`` directly, so a
regression in the supervisor's own ``build_channel`` call — the exact line that
caused the production ``worker queue overflow`` warnings — would slip past all
of them.

Two things are proven here, both through the production code path:

* the channel the real supervisor registers declares ``receive_candles=False``
  and still has its tick channel; and
* driving a real :class:`~common.feed.hub.SharedFeedHub` over that real channel
  for far more completed candles than the queue is deep publishes *nothing*
  into it and warns *not once*.

A third test runs the whole supervisor — real spawned child process, real
shutdown — to show the tick sentinel still reaps the child cleanly now that the
candle queue is never written to.

No live order API is constructed or called anywhere in this module; the one
shared "adapter" is a fully in-process fake, as in
``test_positional_runtime_multi_strategy.py``, whose fixtures this reuses.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from _weekly_delta_neutral_fixtures import OffsetClock
from test_positional_runtime_multi_strategy import (
    NIFTY_SECURITY_ID,
    _build_supervisor,
    _FakeAdapter,
    _tick,
    _worker_config,
)

from common.feed.hub import SharedFeedHub
from common.persistence import Database, MigrationRunner
from common.utils.timeutils import now_ist
from runtimes.positional_options.supervisor import DEFAULT_QUEUE_DEPTH

IST = ZoneInfo("Asia/Kolkata")
INTERVAL_SECONDS = 60
#: Comfortably past ``DEFAULT_QUEUE_DEPTH``: before this fix the queue was full
#: by candle 64 and every candle after it was a drop plus a warning.
CANDLE_COUNT = DEFAULT_QUEUE_DEPTH + 16

OVERFLOW_WARNING = "worker queue overflow"


def _minute_tape(count: int) -> list[Any]:
    """One tick per minute bucket; N+1 ticks complete N bars during ``start()``.

    Timestamped in IST from the open: the aggregator emits bars only inside
    market hours, so a tape long enough to fill a 64-deep queue has to start
    early enough to fit.
    """
    ts = datetime(2026, 8, 19, 9, 20, tzinfo=IST)
    return [
        _tick(NIFTY_SECURITY_ID, 24000.0 + i, ts + timedelta(seconds=INTERVAL_SECONDS * i))
        for i in range(count + 1)
    ]


def _release(channel: Any) -> None:
    """Mirror the supervisor's own ``_release_queues`` for a run we never start."""
    for q in (channel.queue, channel.tick_queue):
        if q is not None:
            q.raw.cancel_join_thread()


# ------------------------------------------------- the registration itself
def test_the_real_supervisor_registers_a_tick_only_channel(tmp_path: Path) -> None:
    database_path = tmp_path / "positional_options.db"
    supervisor = _build_supervisor(
        tmp_path, database_path=database_path, adapter=_FakeAdapter([])
    )
    config = _worker_config(tmp_path, strategy_id="_fixture_solo", database_path=database_path)

    channel = supervisor.add_worker(config)
    try:
        # The capability, read off the object the production path built.
        assert channel.receive_candles is False
        # ...and it did not cost the channel its ticks: the tick queue is the
        # only one the spawned child is ever handed.
        assert channel.tick_queue is not None
        assert channel.queue.max_depth == DEFAULT_QUEUE_DEPTH
    finally:
        _release(channel)


# --------------------------------------------- the production regression
def test_the_real_channel_takes_no_candles_past_its_queue_depth(
    tmp_path: Path, caplog: Any
) -> None:
    """The observed production warning, reproduced at real composition.

    The channel is the real supervisor's; only the hub driving it is scripted,
    so this fails on any regression in ``add_worker``'s ``build_channel`` call
    or in ``_fan_out``'s gate.
    """
    database_path = tmp_path / "positional_options.db"
    supervisor = _build_supervisor(
        tmp_path, database_path=database_path, adapter=_FakeAdapter([])
    )
    config = _worker_config(tmp_path, strategy_id="_fixture_solo", database_path=database_path)
    channel = supervisor.add_worker(config)

    try:
        hub = SharedFeedHub(
            _FakeAdapter(_minute_tape(CANDLE_COUNT)), interval_seconds=INTERVAL_SECONDS
        )
        hub.register(channel)
        with caplog.at_level(logging.WARNING, logger="common.feed.hub"):
            hub.start()
            hub.stop()

        # The hub aggregated every bar; the channel received none of them.
        assert hub.candle_count == CANDLE_COUNT + 1
        assert channel.queue.published == 0
        assert channel.queue.dropped == 0
        assert OVERFLOW_WARNING not in caplog.text

        # Health cannot report a queue this worker does not have.
        assert {stat.name for stat in hub.queue_stats()} == {"_fixture_solo:ticks"}

        # The ticks themselves still flowed — this is a tick-driven runtime.
        assert channel.tick_queue is not None
        assert channel.tick_queue.published == CANDLE_COUNT + 1
        assert channel.tick_queue.dropped == 0
    finally:
        _release(channel)


# ----------------------------------------------------------- shutdown
def test_a_tick_only_worker_still_shuts_down_cleanly(tmp_path: Path) -> None:
    """The real child process, reaped through the tick sentinel alone.

    The candle sentinel is no longer published for a ``receive_candles=False``
    channel, so this is the proof that nothing depended on it: the child still
    exits 0, and its candle queue is provably untouched end to end.
    """
    database_path = tmp_path / "positional_options.db"
    MigrationRunner(Database(database_path)).run_pending()

    ts = datetime(2026, 8, 19, 9, 26, tzinfo=UTC)
    adapter = _FakeAdapter([_tick(NIFTY_SECURITY_ID, 24000.0, ts)])
    clock = OffsetClock(offset=ts - now_ist())
    supervisor = _build_supervisor(
        tmp_path, database_path=database_path, adapter=adapter, clock=clock
    )
    config = _worker_config(tmp_path, strategy_id="_fixture_solo", database_path=database_path)
    channel = supervisor.add_worker(config)

    result = supervisor.run()

    assert result.workers_started == 1
    assert result.worker_exit_codes["_fixture_solo"] == 0

    # Nothing was ever written to the candle queue — not a candle, not the
    # shutdown sentinel.
    assert channel.queue.published == 0
    assert channel.queue.dropped == 0

    # ...so the result reports the channel that exists and no key for the one
    # that does not, matching how the intraday result omits ``:ticks`` for a
    # channel with no tick queue.
    assert "_fixture_solo" not in result.dropped_events
    assert result.dropped_events["_fixture_solo:ticks"] == 0
