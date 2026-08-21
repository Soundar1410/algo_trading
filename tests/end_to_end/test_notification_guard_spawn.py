"""The notification guard across real process boundaries.

The incident this closes was never an in-process one. The messages were sent by
a ``multiprocessing`` ``spawn`` worker and by a ``subprocess`` child started
with ``cwd=REPO_ROOT`` — fresh interpreters that re-import everything, re-read
``.env``, and inherit exactly one thing from the parent: its environment. So
these tests reproduce both shapes for real, against a ``.env`` carrying
real-looking (fake) Telegram credentials, and prove nothing leaves the machine.

The credentials are deliberately *loadable* in every test here. A run where
``Settings`` found no token would prove nothing at all, so each assertion pairs
"the credentials were there" with "and nothing was sent anyway".
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).parent / "notification_guard_child.py"

GUARD_ENV = "ALGO_DISABLE_EXTERNAL_NOTIFICATIONS"
NETWORK_LOG_ENV = "ALGO_TEST_NETWORK_LOG"

FAKE_BOT_TOKEN = "7000000000:AAFfakefakefakefakefakefakefakefake1"
FAKE_CHAT_ID = "-1009999999999"

CHILD_TIMEOUT = 180.0

#: Imported by *every* interpreter that starts with this on ``PYTHONPATH`` —
#: including the ``spawn``ed grandchildren the supervisor creates, which is the
#: whole reason for going through ``sitecustomize`` rather than a monkeypatch.
#: Loopback stays open (``multiprocessing`` IPC, SQLite, nothing external);
#: anything else is recorded and refused, so "zero external requests" is a
#: measured fact rather than an inference.
_SITECUSTOMIZE = '''
import os
import socket

_log_path = os.environ.get("ALGO_TEST_NETWORK_LOG")
if _log_path:
    _real_connect = socket.socket.connect
    _real_connect_ex = socket.socket.connect_ex

    def _is_local(address):
        if not isinstance(address, tuple):
            return True  # AF_UNIX
        host = str(address[0])
        return host in ("", "localhost", "::1", "0.0.0.0") or host.startswith("127.")

    def _record(address):
        with open(_log_path, "a", encoding="utf-8") as handle:
            handle.write("%s %r\\n" % (os.getpid(), address))

    def connect(self, address):
        if not _is_local(address):
            _record(address)
            raise OSError("outbound network blocked by the test network sentinel")
        return _real_connect(self, address)

    def connect_ex(self, address):
        if not _is_local(address):
            _record(address)
            return 1
        return _real_connect_ex(self, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
'''


@pytest.fixture
def dotenv_dir(tmp_path: Path) -> Path:
    """A working directory holding a ``.env`` with real-looking credentials."""
    directory = tmp_path / "with_dotenv"
    directory.mkdir()
    (directory / ".env").write_text(
        f"TELEGRAM_BOT_TOKEN={FAKE_BOT_TOKEN}\n"
        f"TELEGRAM_CHAT_ID={FAKE_CHAT_ID}\n"
        f"PROJECT_ROOT={REPO_ROOT}\n",
        encoding="utf-8",
    )
    return directory


# ------------------------------------------------------------ multiprocessing
def _report_notifier(work_dir: str, result_queue: mp.Queue[dict[str, object]]) -> None:
    """Module-level child entrypoint — must be importable for ``spawn``.

    Runs the production resolution a spawned worker runs: chdir into the
    directory holding ``.env`` (what ``cwd=REPO_ROOT`` amounted to), load
    settings from scratch, build the notifier from them.
    """
    import os as child_os

    child_os.chdir(work_dir)

    from common.config import load_settings
    from common.notifications import build_notifier

    settings = load_settings()
    notifier = build_notifier(settings)
    result_queue.put(
        {
            "guard": child_os.environ.get("ALGO_DISABLE_EXTERNAL_NOTIFICATIONS"),
            "has_telegram_credentials": settings.has_telegram_credentials(),
            "channel": notifier.channel,
            "pid": child_os.getpid(),
        }
    )


def test_a_spawned_child_inherits_the_guard_and_builds_a_null_notifier(dotenv_dir: Path):
    """A fresh interpreter, a populated ``.env``, and still nothing to send with."""
    context = mp.get_context("spawn")
    queue: mp.Queue[dict[str, object]] = context.Queue()
    child = context.Process(target=_report_notifier, args=(str(dotenv_dir), queue))
    child.start()
    child.join(timeout=60)

    assert not child.is_alive()
    report = queue.get(timeout=10)

    assert report["pid"] != os.getpid(), "the check must happen in a real second process"
    assert report["guard"] == "1", "the guard did not cross the spawn boundary"
    assert report["has_telegram_credentials"] is True, (
        "the child did not load the .env, so this run proves nothing"
    )
    assert report["channel"] == "null"


# ---------------------------------------------------------- the whole lifecycle
def test_a_real_skelfix_lifecycle_makes_zero_external_requests(
    dotenv_dir: Path, tmp_path: Path, tick_tape_path: Path
):
    """The incident, re-run: real supervisor, real spawned worker, real fills.

    ``skelfix`` starts, fills twice and stops — the exact three events that
    reached the operator's phone — inside a process tree whose outbound
    sockets are instrumented. The assertions are the two halves that matter:
    the credentials were loadable, and the network log is empty.
    """
    sentinel_dir = tmp_path / "sentinel"
    sentinel_dir.mkdir()
    (sentinel_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    network_log = tmp_path / "network.log"

    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join([str(sentinel_dir), str(REPO_ROOT)])
    environment["PYTHONUNBUFFERED"] = "1"
    environment[NETWORK_LOG_ENV] = str(network_log)
    assert environment[GUARD_ENV] == "1", "the test session bootstrap did not set the guard"

    completed = subprocess.run(
        [sys.executable, str(CHILD), str(tmp_path / "run"), str(tick_tape_path)],
        cwd=str(dotenv_dir),
        env=environment,
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT,
        check=False,
    )
    assert completed.returncode == 0, (
        f"child failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    result = _result_line(completed)

    # The run really happened, and really traded.
    assert result["workers_started"] == 1
    assert result["worker_exit_codes"] == {"skelfix": 0}
    assert result["fills"] == 2, "the fill path did not run, so this proves nothing"

    # The credentials really were there for the taking.
    assert result["has_telegram_credentials"] is True

    # And the spawned worker's own factory took the guarded branch.
    assert result["worker_log_present"] is True
    assert result["worker_log_says_disabled"] is True
    assert result["worker_log_says_telegram_enabled"] is False

    # Nothing left the machine.
    recorded = network_log.read_text(encoding="utf-8") if network_log.exists() else ""
    assert recorded == "", f"outbound connections were attempted:\n{recorded}"

    # And no secret was printed along the way.
    for stream in (completed.stdout, completed.stderr):
        assert FAKE_BOT_TOKEN not in stream
        assert FAKE_BOT_TOKEN.split(":")[1] not in stream


def _result_line(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    for line in completed.stdout.splitlines():
        if line.startswith("RESULT "):
            parsed: dict[str, object] = json.loads(line[len("RESULT ") :])
            return parsed
    raise AssertionError(
        f"the child never reported a result\nstdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
