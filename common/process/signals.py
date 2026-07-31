"""Shutdown-signal ownership: one installer per process, and it puts them back.

Extracted from ``runtimes/intraday_options/supervisor.py`` in Phase 3 Part 2b-i,
unchanged in behaviour. It was a private method there, which was fine while the
supervisor was the only thing that installed handlers. Part 2b introduces a second
process that needs them — the worker, which must tell its engine to square off —
and the rule that keeps both correct is worth being structural rather than
remembered:

    **A process has exactly one shutdown-signal installer. Handlers set a flag and
    return; the component being shut down is asked, on the thread that owns it.**

That rule is what the whole of Phase 3 is about. Part 1 established it for the feed
(``request_stop()``); Part 2b-i establishes it for the engine
(``request_square_off()``); this is the one place that installs the handlers those
two are driven from. Two installers in one process is not an error the interpreter
reports — the second silently wins delivery — so the fix is to have one function
and no reason for anyone to write another.
"""

from __future__ import annotations

import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType
from typing import Any

from common.logging import get_logger

_log = get_logger(__name__)

#: Signals that mean "shut down": SIGTERM from a process manager, SIGINT from a
#: terminal. Both get the same orderly path rather than dying where they land.
SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)


@contextmanager
def shutdown_signals(
    on_signal: Callable[[], None],
    *,
    signals: tuple[signal.Signals, ...] = SHUTDOWN_SIGNALS,
) -> Iterator[None]:
    """Install shutdown handlers for the duration of the block, then put them back.

    The handler does the least it possibly can — call ``on_signal``, which is
    expected to set an event and return — because it runs on the main thread,
    interrupting whatever it was doing. Anything that blocks, takes a lock, or
    touches the resource being shut down risks deadlocking against the very
    shutdown it is trying to start.

    Restoring the previous handlers matters: this runs inside other people's
    processes, including the test suite's, and leaving handlers installed would
    change how ``Ctrl-C`` behaves everywhere afterwards.

    Off the main thread, installing is impossible and that is **not** an error:
    Python only allows handler installation on the main thread. The block still
    runs, with the process's existing handlers still in force, which is the right
    degradation for a supervisor or worker driven from a worker thread or a test.
    """

    def _handle(signum: int, _frame: FrameType | None) -> None:
        _log.warning("received %s; beginning orderly shutdown", signal.Signals(signum).name)
        on_signal()

    # ``Any`` for the previous handler: it is whatever ``signal.signal`` returned,
    # and it is only ever handed straight back to it.
    installed: list[tuple[signal.Signals, Any]] = []
    try:
        for signum in signals:
            try:
                installed.append((signum, signal.signal(signum, _handle)))
            except ValueError:
                _log.debug("cannot install a %s handler off the main thread", signum)
        yield
    finally:
        for installed_signum, previous in installed:
            signal.signal(installed_signum, previous)
