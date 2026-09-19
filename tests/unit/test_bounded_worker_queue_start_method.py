"""``BoundedWorkerQueue``'s multiprocessing queue must come from the *spawn*
context, not from whatever the platform default happens to be.

The defect this pins was real and platform-hidden. ``common/feed/queues.py``
once built its queue with a bare ``multiprocessing.Queue(...)``, which uses the
default context — ``spawn`` on macOS, ``fork`` on Linux. Both supervisors create
their workers with ``mp.get_context("spawn")``
(``runtimes/intraday_options/supervisor.py``,
``runtimes/positional_options/supervisor.py``), so on Linux the queue's
semaphore belonged to one context and the worker to another, and handing it over
raised::

    RuntimeError: A SemLock created in a fork context is being shared with a
    process in a spawn context.

22 end-to-end tests failed that way on Linux while every one of them passed on
macOS, where the default already *is* spawn. A single-line revert would
re-open it and nothing on a developer's Mac would notice — which is the whole
reason this test exists, and why it forces ``fork`` rather than trusting the
host's default.

``set_start_method`` is process-global and cannot be undone, so the work happens
in a fresh interpreter (``_bounded_worker_queue_start_method_child.py``), the
same isolation ``tests/end_to_end/test_notification_guard_spawn.py`` uses for
its own start-method-sensitive cases.

See deviation D86 in the runbook for the fail-first transcript.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).parent / "_bounded_worker_queue_start_method_child.py"

#: The child spawns a grandchild interpreter of its own, so it needs more room
#: than a plain unit test; still bounded, so a wedge fails rather than hangs.
CHILD_TIMEOUT = 180.0


def test_a_worker_queue_survives_a_fork_default_handed_to_a_spawn_worker():
    """The production construction path, under Linux's default start method.

    Fails with ``SemLock created in a fork context`` against a
    ``BoundedWorkerQueue`` that builds its queue from the default context.
    """
    result = subprocess.run(
        [sys.executable, str(CHILD)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT,
        check=False,
    )

    assert result.returncode == 0, (
        "a BoundedWorkerQueue built under a fork default could not be handed to a "
        f"spawn-context worker.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout, f"child did not confirm delivery:\n{result.stdout}"
    assert "SemLock" not in result.stderr, result.stderr
