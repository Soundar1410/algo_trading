"""Child process for the supervisor's SIGTERM test. Not a test module itself.

Run as ``python tests/end_to_end/supervisor_signal_child.py <work-dir>``. It
starts a real :class:`IntradayOptionsSupervisor` over a feed whose ``start()``
never returns on its own — the shape of a live ``DhanMarketFeedAdapter``, and the
shape under which the supervisor used to hang with no way out but an external
kill. The parent test sends it a real ``SIGTERM``.

It reports through stdout rather than a return value, because the point is what a
*separate process* does when signalled:

``READY``
    the feed is delivering and the signal handlers are installed, so it is safe
    to signal. Printed from the feed thread, which cannot run before the
    supervisor has installed them.
``CROSS_THREAD_STOP``
    the adapter's connection was closed from a thread that does not own its loop.
    Must never appear; the parent fails the test if it does.
``RESULT <json>``
    the outcome of the shutdown.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from common.market_data.adapter import TickCallback
from common.models import Tick
from common.notifications import RecordingNotifier
from runtimes.intraday_options.supervisor import IntradayOptionsSupervisor, SupervisorConfig
from runtimes.intraday_options.worker import WorkerConfig

RUNTIME_ID = "intraday_options"
STRATEGY_ID = "skelfix"
SECURITY_ID = "99926000"
TRADING_DATE = "2026-07-30"

#: Real seconds between frames. Fast enough that the shutdown is prompt, slow
#: enough that the loop spends its time waiting rather than spinning.
FRAME_INTERVAL = 0.01
#: Exchange-time step per frame. Larger than the frame interval so completed
#: candles are produced within a run that lasts a second or two.
EXCHANGE_STEP_SECONDS = 10
SESSION_START = datetime(2026, 7, 30, 9, 15, 0, tzinfo=UTC) - timedelta(hours=5, minutes=30)


class EndlessFeedAdapter:
    """A feed that never finishes by itself, like a live socket.

    Satisfies :class:`~common.market_data.adapter.MarketFeedAdapter` and honours
    its ownership rule: ``request_stop()`` only sets a flag, and the connection is
    released by the thread inside ``start()``.
    """

    def __init__(self, *, silent: bool = False) -> None:
        #: ``silent`` models the case nothing can force: a connected socket
        #: delivering no frames, so its owner is blocked with no boundary at which
        #: to notice ``request_stop()``. Deliberately does **not** honour it.
        self._silent = silent
        self._blocked = threading.Event()  # never set: there is no next frame
        self._security_ids: set[str] = set()
        self._running = False
        self.owner_thread: int | None = None
        self.closed_by: int | None = None
        self.delivered = 0

    @property
    def is_running(self) -> bool:
        return self._running

    def subscribe(self, security_ids: list[str], *, segment: int | None = None) -> None:
        self._security_ids.update(str(s) for s in security_ids)

    def start(self, on_tick: TickCallback) -> None:
        self.owner_thread = threading.get_ident()
        self._running = True
        sequence = 0
        if self._silent:
            # Blocked in recv() for good. The supervisor must give up on it
            # without closing the socket from another thread, and the process must
            # still exit — this thread is a daemon.
            print("READY", flush=True)
            self._blocked.wait()
            return
        try:
            while self._running:
                time.sleep(FRAME_INTERVAL)
                on_tick(_tick(sequence))
                self.delivered += 1
                sequence += 1
                if self.delivered == 3:
                    # Handlers are installed by now: this thread only exists
                    # because the supervisor started it inside their scope.
                    print("READY", flush=True)
        finally:
            self._running = False
            self.closed_by = threading.get_ident()

    def request_stop(self) -> None:
        self._running = False

    def stop(self) -> None:
        if self._running and threading.get_ident() != self.owner_thread:
            print("CROSS_THREAD_STOP", flush=True)
        self._running = False


def _tick(sequence: int) -> Tick:
    moment = SESSION_START + timedelta(seconds=EXCHANGE_STEP_SECONDS * sequence)
    return Tick(
        security_id=SECURITY_ID,
        instrument="NIFTY",
        last_price=100.0 + sequence,
        exchange_time=moment,
        received_at=moment,
        last_quantity=1,
    )


def main(work_dir: Path, *, silent: bool = False, grace: float | None = None) -> int:
    lock_dir = work_dir / "locks"
    pid_dir = work_dir / "pid"
    log_dir = work_dir / "logs"
    operational = work_dir / "operational"
    for directory in (lock_dir, pid_dir, log_dir, operational):
        directory.mkdir(parents=True, exist_ok=True)

    config = SupervisorConfig(
        runtime_id=RUNTIME_ID,
        database_path=operational / "intraday_options.db",
        lock_dir=lock_dir,
        pid_dir=pid_dir,
        log_dir=log_dir,
    )
    adapter = EndlessFeedAdapter(silent=silent)
    notifier = RecordingNotifier()
    supervisor = IntradayOptionsSupervisor(config, adapter, notifier)
    supervisor.add_worker(
        WorkerConfig(
            runtime_id=RUNTIME_ID,
            strategy_id=STRATEGY_ID,
            security_id=SECURITY_ID,
            instrument="NIFTY",
            database_path=config.database_path,
            lock_dir=lock_dir,
            pid_dir=pid_dir,
            log_dir=log_dir,
            trading_date=TRADING_DATE,
        )
    )

    started = time.monotonic()
    # The default grace period unless one is asked for: this file must be runnable
    # against a supervisor that has never heard of the parameter, so that the
    # failure the fail-first demonstration reports is a *behavioural* one. Only the
    # silent case overrides it, and only to keep that test from taking 10s.
    result = supervisor.run() if grace is None else supervisor.run(shutdown_grace=grace)
    for event in notifier.events:
        print(f"NOTIFIED {event.event_type}", flush=True)
    print(
        "RESULT "
        + json.dumps(
            {
                "stopped_by_signal": result.stopped_by_signal,
                "clean_feed_shutdown": result.clean_feed_shutdown,
                "ticks_received": result.ticks_received,
                "workers_started": result.workers_started,
                "worker_exit_codes": result.worker_exit_codes,
                "closed_on_owner_thread": adapter.closed_by == adapter.owner_thread,
                "feed_still_running": adapter.is_running,
                "shutdown_seconds": round(time.monotonic() - started, 3),
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("work_dir", type=Path)
    parser.add_argument(
        "--silent",
        action="store_true",
        help="Model a connected socket that delivers nothing and cannot be closed.",
    )
    parser.add_argument("--grace", type=float, default=None, help="Shutdown grace override.")
    parsed = parser.parse_args()
    sys.exit(main(parsed.work_dir, silent=parsed.silent, grace=parsed.grace))
