"""Child process for the notification-guard end-to-end test. Not a test module.

Run as ``python tests/end_to_end/notification_guard_child.py <work-dir> <tape>``.
It runs a **real** :class:`~runtimes.intraday_options.supervisor.
IntradayOptionsSupervisor` over the recorded tape with the ``skelfix`` fixture
strategy — the same run that once produced the flood of real Telegram
messages: the supervisor ``spawn``s its worker with
:data:`~runtimes.intraday_options.worker.NOTIFIER_FROM_SETTINGS`, and that
worker builds its own notifier from its own freshly-loaded ``Settings``.

The test starts this with ``cwd`` set to a directory holding a ``.env`` full of
real-*looking* (entirely fake) Telegram credentials, reproducing what the
supervisor-signal child saw at ``REPO_ROOT``. Everything about the run is real
except the credentials and the tape; the only thing standing between it and
``api.telegram.org`` is the guard.

Reports one ``RESULT <json>`` line on stdout: what the child's own settings
loaded, what the run did, and what the spawned worker's log says its notifier
resolved to.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from common.config import load_settings
from common.market_data import RecordedFeedAdapter, load_tick_tape
from common.persistence import Database
from runtimes.intraday_options.supervisor import IntradayOptionsSupervisor, SupervisorConfig
from runtimes.intraday_options.worker import WorkerConfig

RUNTIME_ID = "intraday_options"
STRATEGY_ID = "skelfix"
SECURITY_ID = "99926000"
TRADING_DATE = "2026-07-29"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workdir", type=Path)
    parser.add_argument("tape", type=Path)
    args = parser.parse_args(argv)

    work: Path = args.workdir
    lock_dir = work / "runtime" / "locks"
    pid_dir = work / "runtime" / "pid"
    log_dir = work / "logs"
    operational = work / "operational"
    for directory in (lock_dir, pid_dir, log_dir, operational):
        directory.mkdir(parents=True, exist_ok=True)

    database_path = operational / f"{RUNTIME_ID}.db"
    config = SupervisorConfig(
        runtime_id=RUNTIME_ID,
        database_path=database_path,
        lock_dir=lock_dir,
        pid_dir=pid_dir,
        log_dir=log_dir,
    )
    supervisor = IntradayOptionsSupervisor(config, RecordedFeedAdapter(load_tick_tape(args.tape)))
    supervisor.add_worker(
        WorkerConfig(
            runtime_id=RUNTIME_ID,
            strategy_id=STRATEGY_ID,
            security_id=SECURITY_ID,
            instrument="NIFTY",
            database_path=database_path,
            lock_dir=lock_dir,
            pid_dir=pid_dir,
            log_dir=log_dir,
            trading_date=TRADING_DATE,
        )
    )
    result = supervisor.run()

    connection = Database(database_path).connect()
    fills = connection.execute(
        "SELECT COUNT(*) FROM fills WHERE strategy_id = ?", (STRATEGY_ID,)
    ).fetchone()[0]

    worker_log = log_dir / f"{STRATEGY_ID}.log"
    log_text = worker_log.read_text(encoding="utf-8") if worker_log.is_file() else ""

    settings = load_settings()
    print(
        "RESULT "
        + json.dumps(
            {
                # Proof the credentials really were loadable in this process
                # tree: without this the NullNotifier below would prove nothing.
                "has_telegram_credentials": settings.has_telegram_credentials(),
                "workers_started": result.workers_started,
                "worker_exit_codes": result.worker_exit_codes,
                "fills": fills,
                "worker_log_present": bool(log_text),
                # What the *spawned worker's* own build_notifier logged.
                "worker_log_says_disabled": "external notifications are disabled" in log_text,
                "worker_log_says_telegram_enabled": "Telegram notifications enabled" in log_text,
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
