"""**The Part 2b-ii-B-2 acceptance gate**: a real signal to a real worker child.

A ``SIGTERM`` to a real process, running the real ported engine, holding a real
position in real SQLite — which must be **closed** before the process exits 0.

Nothing in the suite proved this before, and the gap was structural rather than an
oversight. ``tests/integration/test_engine_worker.py`` drives the same worker
in-process, where "a signal" is a method call and the handler is never installed.
``tests/end_to_end/test_supervisor_signal.py`` signals the *supervisor*, which
reaches its children through a queue sentinel — a different path entirely. What a
process manager actually does at 15:20, and what an operator does with Ctrl-C, is
deliver a signal to the worker itself, and until this file nothing exercised it.

It is also the first test of the whole D18 chain in one piece: handler sets a flag
and returns → ``HubTickFeed`` notices it on the thread that owns the feed →
``TradingEngine`` squares off on that same thread → ``LifecycleGateway`` persists the
closing leg → the process exits cleanly.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

CHILD = Path(__file__).parent / "engine_worker_signal_child.py"
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Bounds the whole run generously enough to survive a loaded machine while still
#: failing rather than hanging.
CHILD_TIMEOUT = 60.0
#: How long to wait for the child to report that it holds a position.
READY_TIMEOUT = 45.0


@pytest.fixture
def start_worker(tmp_path: Path):
    """Start the child worker, and guarantee it is reaped however the test ends."""
    started: list[subprocess.Popen[str]] = []

    def _start() -> subprocess.Popen[str]:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(REPO_ROOT)
        environment["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            [sys.executable, str(CHILD), str(tmp_path)],
            cwd=str(REPO_ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        started.append(process)
        return process

    try:
        yield _start
    finally:
        for process in started:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10.0)


def _wait_for_ready(process: subprocess.Popen[str]) -> None:
    """Block until the child holds a real position, or fail loudly.

    Waiting for the *position* rather than sleeping is what makes this test mean
    something: a signal delivered before the engine had anything to close would be
    squaring off an empty book, which a broken implementation passes just as easily.
    """
    assert process.stdout is not None
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            break
        if line.strip() == "READY":
            return
    process.kill()
    pytest.fail(f"the child never opened a position within {READY_TIMEOUT:.0f}s")


def _result(stdout: str, stderr: str) -> dict:
    for line in stdout.splitlines():
        if line.startswith("RESULT "):
            parsed: dict = json.loads(line.removeprefix("RESULT "))
            return parsed
    pytest.fail(f"the child reported no result\nstdout:\n{stdout}\nstderr:\n{stderr}")


def _signal_and_collect(process: subprocess.Popen[str], signum: int) -> tuple[dict, str, str]:
    _wait_for_ready(process)
    process.send_signal(signum)
    stdout, stderr = process.communicate(timeout=CHILD_TIMEOUT)
    return _result(stdout, stderr), stdout, stderr


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT], ids=["sigterm", "sigint"])
def test_a_real_signal_squares_off_and_exits_zero(start_worker, signum, tmp_path: Path):
    """The gate, both signals. SIGTERM is the process manager; SIGINT is Ctrl-C."""
    process = start_worker()
    signalled_at = time.monotonic()
    result, stdout, stderr = _signal_and_collect(process, signum)
    elapsed = time.monotonic() - signalled_at

    assert process.returncode == 0, f"non-zero exit\nstdout:\n{stdout}\nstderr:\n{stderr}"
    assert result["exit_code"] == 0, result["error"]
    assert result["stopped_by_request"] is True, "the run ended for some other reason"

    # The property that matters: the book is flat.
    assert result["positions_still_open"] == 0, "a position was left open by the shutdown"
    assert result["trades_closed"] == 1
    assert result["square_off_completed"] is True
    assert result["square_off_state"] == "COMPLETED"
    assert result["clean_engine_shutdown"] is True

    # The engine was genuinely running, not stalled at startup.
    assert result["ticks_processed"] > 3
    assert result["orders_placed"] == 2

    # A shutdown that merely *eventually* completes is not much better than one that
    # hangs: an orderly stop has to beat a process manager's own patience, which is
    # where SIGKILL waits.
    assert elapsed < 30.0, f"shutdown took {elapsed:.1f}s after the signal"


def test_sigint_does_not_escape_as_a_traceback(start_worker):
    """A terminal Ctrl-C gets the orderly path, not a KeyboardInterrupt out of main."""
    process = start_worker()
    _, _stdout, stderr = _signal_and_collect(process, signal.SIGINT)

    assert process.returncode == 0
    assert "KeyboardInterrupt" not in stderr, "SIGINT was not handled, it just escaped"
    assert "Traceback" not in stderr, stderr


def test_the_closing_leg_is_persisted_through_the_audited_path(start_worker, tmp_path: Path):
    """The square-off is a real order, not an in-memory bookkeeping entry.

    Squaring off by simply forgetting the position would satisfy every assertion
    about flatness above, so this checks the database the shutdown wrote.
    """
    process = start_worker()
    _signal_and_collect(process, signal.SIGTERM)

    connection = sqlite3.connect(str(tmp_path / "operational" / "intraday_options.db"))
    connection.row_factory = sqlite3.Row
    try:
        sides = [
            row["side"] for row in connection.execute("SELECT side FROM order_intents ORDER BY id")
        ]
        fills = connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        position = connection.execute("SELECT * FROM positions").fetchone()
        session = connection.execute(
            """
            SELECT shutdown_reason FROM runtime_sessions
            WHERE process_role = 'worker' ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()

    assert sides == ["BUY", "SELL"], "the square-off did not place a closing order"
    assert fills == 2
    assert position["status"] == "CLOSED"
    assert position["quantity"] == 0
    assert session["shutdown_reason"] == "signal"


def test_the_final_health_state_records_a_clean_stop(start_worker):
    """No alarm on a shutdown that did what it was asked."""
    process = start_worker()
    result, _stdout, _stderr = _signal_and_collect(process, signal.SIGTERM)

    assert result["last_health_state"] == "STOPPED", (
        "a signalled shutdown that flattened the book must not end DEGRADED"
    )
