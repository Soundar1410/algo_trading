"""Driver for ``test_bounded_worker_queue_start_method.py``. Not a test module.

Run as a fresh interpreter, because the one thing it must do —
``multiprocessing.set_start_method("fork", force=True)`` — is process-global and
irreversible. Doing it inside the pytest process would change the start method
for every test that ran afterwards.

Forcing ``fork`` here is what makes this macOS run reproduce the Linux failure:
``fork`` is Linux's default and ``spawn`` is macOS's, so a queue built from the
*default* context is a fork-context queue on Linux and a spawn-context one here.
Pinned to ``fork`` explicitly, both platforms exercise the same hazard.

Prints ``OK`` and exits 0 only if a queue built by
:class:`~common.feed.queues.BoundedWorkerQueue` can actually carry a value into a
``spawn``-context child — which is the only kind of child the supervisors create
(``mp.get_context("spawn")`` in both ``runtimes/*/supervisor.py``).
"""

from __future__ import annotations

import multiprocessing as mp
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SENTINEL = "a candle that crossed the process boundary"

#: Bounded so a wedged child cannot hang the suite; generous enough that a
#: loaded machine spawning a fresh interpreter is not mistaken for a failure.
CHILD_JOIN_TIMEOUT = 60.0
GET_TIMEOUT = 30.0


def put_one(raw_queue: Any, value: str) -> None:
    """The ``spawn``ed child's entry point.

    Module level, not a closure, because ``spawn`` pickles the target by
    qualified name and re-imports this module (as ``__mp_main__``) to find it.
    That re-import is also why ``set_start_method`` sits under the ``__main__``
    guard below rather than at module scope — the re-imported copy must not
    re-run it.
    """
    raw_queue.put(value)


def main() -> int:
    mp.set_start_method("fork", force=True)

    from common.feed.queues import BoundedWorkerQueue

    # No ``_queue`` injected: this is the production construction path, the one
    # the supervisor takes when it builds a real worker's inbound channel.
    bounded = BoundedWorkerQueue("s1", max_depth=4)

    child = mp.get_context("spawn").Process(target=put_one, args=(bounded._queue, SENTINEL))
    child.start()
    child.join(CHILD_JOIN_TIMEOUT)

    if child.is_alive():  # pragma: no cover - only on a wedged machine
        child.kill()
        child.join(5)
        print("child never exited", file=sys.stderr)
        return 2
    if child.exitcode != 0:
        print(f"child exited {child.exitcode}", file=sys.stderr)
        return 3

    received = bounded.get(timeout=GET_TIMEOUT)
    if received != SENTINEL:
        print(f"child put nothing usable: {received!r}", file=sys.stderr)
        return 4

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
