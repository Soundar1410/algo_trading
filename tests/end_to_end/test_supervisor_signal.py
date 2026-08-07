"""A real SIGTERM to a real supervisor process, over a feed that never ends.

Nothing else in the suite proves the signal path: the recorded adapter's
``start()`` returns on its own, so every other supervisor test would pass just as
happily against a runtime with no shutdown handling whatsoever. That is precisely
the state this repository was in before Phase 3 Part 1 — pointed at a live feed,
``run()`` would have blocked forever with no handler installed, recoverable only
by killing the process from outside.

So this test does the only thing that actually demonstrates the fix: it spawns a
separate process, waits until its feed is genuinely running, sends it a real
signal, and checks that it shut itself down — in order, on the right threads, and
without anyone reaching across to close a connection they do not own.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from common.persistence import Database

CHILD = Path(__file__).parent / "supervisor_signal_child.py"
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The child is given a 5s grace period; this bounds the whole run generously
#: enough to survive a loaded CI machine while still failing rather than hanging.
CHILD_TIMEOUT = 60.0
#: How long to wait for the child to report that its feed is live.
READY_TIMEOUT = 30.0


@pytest.fixture
def start_supervisor(tmp_path: Path):
    """Start the child supervisor with given arguments, and guarantee it is reaped."""
    started: list[subprocess.Popen[str]] = []

    def _start(*extra_args: str) -> subprocess.Popen[str]:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(REPO_ROOT)
        environment["PYTHONUNBUFFERED"] = "1"

        process = subprocess.Popen(
            [sys.executable, str(CHILD), str(tmp_path), *extra_args],
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


@pytest.fixture
def supervisor_process(start_supervisor):
    """The ordinary case: a feed that delivers, and can be stopped."""
    return start_supervisor()


def _wait_for_ready(process: subprocess.Popen[str]) -> None:
    """Block until the child says its feed is delivering, or fail loudly."""
    assert process.stdout is not None
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            break
        if line.strip() == "READY":
            return
        if line.strip() == "CROSS_THREAD_STOP":
            pytest.fail("the child closed its feed from a thread that does not own it")
    process.kill()
    pytest.fail(f"the child never reported a running feed within {READY_TIMEOUT:.0f}s")


def test_a_real_sigterm_shuts_the_supervisor_down_cleanly(supervisor_process):
    process = supervisor_process
    _wait_for_ready(process)

    signalled_at = time.monotonic()
    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=CHILD_TIMEOUT)
    elapsed = time.monotonic() - signalled_at

    assert process.returncode == 0, f"non-zero exit\nstdout:\n{stdout}\nstderr:\n{stderr}"
    assert "CROSS_THREAD_STOP" not in stdout, (
        "the connection was closed from a thread that does not own the feed loop"
    )

    result = _result_line(stdout, stderr)
    assert result["stopped_by_signal"] is True, "the run ended for some other reason"
    assert result["clean_feed_shutdown"] is True, "the feed did not finish within the grace period"
    assert result["closed_on_owner_thread"] is True, (
        "the feed was closed by a thread other than the one that opened it"
    )
    assert result["feed_still_running"] is False
    assert result["ticks_received"] > 0, "the feed was never actually live"

    # A shutdown that merely *eventually* completes is not much better than one
    # that hangs: an orderly stop has to be prompt enough for a process manager's
    # own patience, which is where SIGKILL waits.
    assert elapsed < 30.0, f"shutdown took {elapsed:.1f}s after the signal"


def test_the_workers_are_still_drained_when_a_signal_ends_the_run(supervisor_process):
    """Signalled or not, the run finishes the same way: no orphaned children."""
    process = supervisor_process
    _wait_for_ready(process)

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=CHILD_TIMEOUT)

    result = _result_line(stdout, stderr)
    assert result["workers_started"] == 1
    assert result["worker_exit_codes"] == {"skelfix": 0}, (
        "a worker was left behind or died badly during a signalled shutdown"
    )


def test_sigint_is_handled_the_same_way(supervisor_process):
    """A terminal Ctrl-C gets the orderly path too, not a traceback."""
    process = supervisor_process
    _wait_for_ready(process)

    process.send_signal(signal.SIGINT)
    stdout, stderr = process.communicate(timeout=CHILD_TIMEOUT)

    assert process.returncode == 0, f"non-zero exit\nstdout:\n{stdout}\nstderr:\n{stderr}"
    assert "KeyboardInterrupt" not in stderr, "SIGINT was not handled, it just escaped"
    result = _result_line(stdout, stderr)
    assert result["stopped_by_signal"] is True
    assert result["clean_feed_shutdown"] is True


def test_a_feed_that_cannot_be_closed_raises_an_alarm_an_operator_would_see(
    start_supervisor, tmp_path: Path
):
    """Runbook limitation 13, asserted on all three channels.

    A log line is not an alarm — nobody is tailing the file at 15:31. The
    condition has to reach the dashboard's health tile, the error it displays
    alongside it, and a human who is looking at neither. This drives the real
    thing: a connected feed that delivers nothing and never honours the stop
    request, in a real process, ended by a real signal.
    """
    process = start_supervisor("--silent", "--grace", "2.0")
    _wait_for_ready(process)

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=CHILD_TIMEOUT)

    # The process still exits cleanly even though its feed thread never returned:
    # that thread is a daemon, which is the whole reason giving up is survivable.
    assert process.returncode == 0, f"non-zero exit\nstdout:\n{stdout}\nstderr:\n{stderr}"
    assert "CROSS_THREAD_STOP" not in stdout, "escalated to the close that hangs"

    result = _result_line(stdout, stderr)
    assert result["clean_feed_shutdown"] is False, "an unclosable feed reported as clean"
    assert result["stopped_by_signal"] is True

    # Channel 3: the human.
    assert "NOTIFIED feed_shutdown_unclean" in stdout, "no notification was sent"

    connection = Database(tmp_path / "operational" / "intraday_options.db").connect()

    # Channel 1: the dashboard's health tile. DEGRADED must be the *last* word —
    # a tidy STOPPED written afterwards would erase the alarm from the tile.
    last_state = connection.execute(
        """
        SELECT health_state FROM runtime_heartbeats
        WHERE strategy_id IS NULL ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    assert last_state is not None, "the supervisor published no health at all"
    assert last_state["health_state"] == "DEGRADED", (
        "the group's final health state does not show the alarm"
    )

    # Channel 2: the message that tile displays.
    error = connection.execute(
        "SELECT severity, component, message FROM errors ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert error is not None, "no error row was recorded"
    assert error["severity"] == "CRITICAL"
    assert error["component"] == "feed"
    assert "delivering nothing" in error["message"]

    # And the session says why it ended, for anyone reading afterwards.
    session = connection.execute(
        """
        SELECT process_role, shutdown_reason FROM runtime_sessions
        WHERE process_role = 'supervisor' ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    assert session["shutdown_reason"] == "feed_did_not_stop"


def test_a_clean_run_publishes_group_health_and_ends_stopped(supervisor_process, tmp_path: Path):
    """The other half: the group is visible while healthy, not only when it breaks.

    Without this the dashboard cannot tell a running group from a dead one, since
    every other heartbeat in the table belongs to a worker.
    """
    process = supervisor_process
    _wait_for_ready(process)
    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=CHILD_TIMEOUT)

    assert process.returncode == 0, f"non-zero exit\nstdout:\n{stdout}\nstderr:\n{stderr}"
    # Phase 7 Part 2: a clean run now legitimately notifies on routine
    # lifecycle (runtime_started/runtime_stopped, and feed_recovered once the
    # tape's first tick lands) — "no NOTIFIED line at all" stopped being the
    # right check the moment those existed to fire. What must still never
    # appear on a clean run is an *alarm* — the three event types the group's
    # own alarm methods (_raise_silent_feed_alarm, _check_stuck_subscription,
    # add_worker's live-gate refusal) are the only things that ever send.
    alarm_event_types = {
        "feed_shutdown_unclean",
        "subscription_not_applied",
        "live_strategy_blocked",
    }
    notified_lines = [line for line in stdout.splitlines() if line.startswith("NOTIFIED")]
    notified = {line.removeprefix("NOTIFIED ") for line in notified_lines}
    alarms_fired = notified & alarm_event_types
    assert not alarms_fired, f"a clean shutdown must not raise an alarm: {alarms_fired}"
    assert "runtime_started" in notified
    assert "runtime_stopped" in notified

    connection = Database(tmp_path / "operational" / "intraday_options.db").connect()
    states = [
        row["health_state"]
        for row in connection.execute(
            """
            SELECT health_state FROM runtime_heartbeats
            WHERE strategy_id IS NULL ORDER BY id
            """
        ).fetchall()
    ]
    assert states, "the supervisor published no group health"
    assert states[0] == "STARTING"
    assert "RUNNING_PAPER" in states
    assert states[-1] == "STOPPED", f"a clean run should end STOPPED, got {states[-1]!r}"
    assert "DEGRADED" not in states, "a clean run must not report DEGRADED"

    session = connection.execute(
        """
        SELECT shutdown_reason FROM runtime_sessions
        WHERE process_role = 'supervisor' ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    assert session["shutdown_reason"] == "signal"


def _result_line(stdout: str, stderr: str) -> dict:
    for line in stdout.splitlines():
        if line.startswith("RESULT "):
            parsed: dict = json.loads(line.removeprefix("RESULT "))
            return parsed
    pytest.fail(f"the child reported no result\nstdout:\n{stdout}\nstderr:\n{stderr}")
