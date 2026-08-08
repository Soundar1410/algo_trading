"""Bounded-restart wrapper around a runtime supervisor entrypoint.

    .venv/bin/python -m orchestration.process_control.supervised_launch \\
        [--runtime-id intraday_options] [--config-root config] \\
        [--max-attempts 3] [--backoff-seconds 30]

``launchd``'s own ``KeepAlive`` is unbounded — ``ThrottleInterval`` only
paces retries, it never caps them. Spec section 12's "bounded restart policy"
needs something that actually stops retrying, so this is what every runtime
LaunchAgent's ``ProgramArguments`` points at instead of
``runtimes.intraday_options.__main__`` directly.

Each attempt runs :mod:`scripts.validate_environment` once up front (fail
before touching the feed at all) and then
:func:`runtimes.intraday_options.__main__.main` up to ``--max-attempts``
times. Exit codes split into two groups:

* **Terminal** — stop immediately, whatever the code:
  ``EXIT_OK``, ``EXIT_RUNTIME_DISABLED``, ``EXIT_STRATEGY_NOT_FOUND``,
  ``EXIT_LEGACY_SYSTEM_ACTIVE``, ``EXIT_SAFETY_SHUTDOWN``. None of these mean
  "try again": a disabled runtime is still disabled next attempt, and a
  deliberate shutdown must never loop — spec section 12's own words.
* **Retryable, bounded** — ``EXIT_FAILED``, ``EXIT_NO_CREDENTIALS``: a
  transient auth/network failure, not a structural refusal to start. Retried
  up to ``--max-attempts`` times with a fixed backoff between attempts; once
  exhausted, this process itself exits non-zero and stops — ``launchd``'s own
  restart of *this* process, bounded by whatever ``ThrottleInterval`` the
  plist sets, is what finally recovers from a transient outage that outlasts
  every in-process retry.

An **unexpected exception** raised out of :func:`supervisor_main.main` itself
(a bug, not a classified exit code) is caught and folded into the same
retryable path rather than left to escape ``run()`` — the whole reason this
module exists is to bound restarts in-process instead of leaning on
``launchd``'s own uncapped ``KeepAlive``, and an uncaught exception would
silently defeat exactly that. Only :class:`Exception` is caught, never
:class:`BaseException` — a deliberate ``SystemExit`` or ``KeyboardInterrupt``
propagates untouched, exactly like today, and is never miscategorized as a
transient failure worth retrying.

Every attempt and its classification is written to the ``errors`` table
through ``ExecutionRepository.record_error`` — not ``record_audit_event``:
that table's ``action`` column is a closed vocabulary enforced by a ``CHECK``
baked into its ``CREATE TABLE`` (migration 0004), and the additive-only
migration runner cannot widen it without a real schema migration (it rejects
``DROP``, which any SQLite CHECK-widening rebuild needs). A supervised-launch
attempt is also not the kind of event that table is for — it is an automated
lifecycle event, not an operator issuing a live-impacting command — which is
exactly what ``errors`` (unconstrained ``component``/``severity``) already
models; ``IntradayOptionsSupervisor`` uses the same table the same way for
its own lifecycle events (``component="supervisor.correlation_token_collision"``
in ``runtimes/intraday_options/supervisor.py``). Severity follows spec section
14's own table: ``INFO`` for a clean stop, ``WARNING`` for a retryable
attempt or a deliberate refusal, ``ERROR`` once every attempt is exhausted —
so a restart loop is visible after the fact even though nobody was watching
the terminal.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from common.config import load_paths, load_settings
from common.logging import get_logger
from runtimes.intraday_options import __main__ as supervisor_main
from scripts import validate_environment
from scripts._operator_common import open_audit_repository

_log = get_logger(__name__)

#: Codes that must never be retried — see the module docstring.
TERMINAL_EXIT_CODES = frozenset(
    {
        supervisor_main.EXIT_OK,
        supervisor_main.EXIT_RUNTIME_DISABLED,
        supervisor_main.EXIT_STRATEGY_NOT_FOUND,
        supervisor_main.EXIT_LEGACY_SYSTEM_ACTIVE,
        supervisor_main.EXIT_SAFETY_SHUTDOWN,
    }
)

#: Codes worth one more attempt — a transient auth/network failure, not a
#: structural refusal to start.
RETRYABLE_EXIT_CODES = frozenset({supervisor_main.EXIT_FAILED, supervisor_main.EXIT_NO_CREDENTIALS})

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 30.0

#: This wrapper's own exit codes — distinct from the supervisor's, so a
#: caller (``launchd``, or a human reading a log) can tell "gave up after N
#: attempts" apart from "refused before even one attempt" and from a clean
#: pass-through of the supervisor's own terminal code.
EXIT_OK = 0
EXIT_GAVE_UP = 1
EXIT_PREFLIGHT_FAILED = 3


def _run_preflight(runtime_id: str) -> bool:
    """True if :mod:`scripts.validate_environment` reports no problems."""
    return validate_environment.main(["--runtime-id", runtime_id]) == validate_environment.EXIT_OK


def _record_attempt(
    *,
    runtime_id: str,
    attempt: int,
    max_attempts: int,
    exit_code: int,
    severity: str,
    terminal: bool,
    exception: Exception | None = None,
) -> None:
    """One ``errors`` row per attempt — the only write this module performs.

    ``exception`` is set only when this attempt raised instead of returning a
    classified exit code — its type and message are folded into ``message``
    so the exception is visible from the audit trail itself, not just the
    application log's traceback (see ``_log.exception`` at the call site).
    """
    settings = load_settings()
    paths = load_paths(settings=settings)
    repository = open_audit_repository(paths.database_path(runtime_id))
    if exception is not None:
        message = (
            f"attempt {attempt}/{max_attempts}: exception={type(exception).__name__}: "
            f"{exception} (retryable)"
        )
    else:
        message = (
            f"attempt {attempt}/{max_attempts}: exit_code={exit_code} "
            f"({'terminal' if terminal else 'retryable'})"
        )
    repository.record_error(
        runtime_id=runtime_id,
        strategy_id=None,
        execution_mode=None,
        severity=severity,
        component="supervised_launch",
        message=message,
    )


def run(
    *,
    runtime_id: str,
    config_root: Path,
    max_attempts: int,
    backoff_seconds: float,
) -> int:
    if not _run_preflight(runtime_id):
        _log.error("preflight failed; refusing to start %s", runtime_id)
        return EXIT_PREFLIGHT_FAILED

    exit_code = supervisor_main.EXIT_FAILED
    last_exception: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        _log.info("supervised launch attempt %d/%d for %s", attempt, max_attempts, runtime_id)
        last_exception = None
        try:
            exit_code = supervisor_main.main(
                ["--runtime-id", runtime_id, "--config-root", str(config_root)]
            )
        except Exception as exc:  # intentionally broad — see module docstring
            # Deliberately not `except BaseException`: SystemExit/KeyboardInterrupt
            # must keep propagating untouched, never be folded into this retry path.
            last_exception = exc
            exit_code = supervisor_main.EXIT_FAILED
            _log.exception(
                "supervised launch attempt %d/%d for %s raised an unexpected exception",
                attempt,
                max_attempts,
                runtime_id,
            )

        if last_exception is None and exit_code in TERMINAL_EXIT_CODES:
            severity = "INFO" if exit_code == supervisor_main.EXIT_OK else "WARNING"
            _record_attempt(
                runtime_id=runtime_id,
                attempt=attempt,
                max_attempts=max_attempts,
                exit_code=exit_code,
                severity=severity,
                terminal=True,
            )
            return exit_code

        _record_attempt(
            runtime_id=runtime_id,
            attempt=attempt,
            max_attempts=max_attempts,
            exit_code=exit_code,
            severity="WARNING",
            terminal=False,
            exception=last_exception,
        )
        if attempt < max_attempts:
            _log.warning(
                "attempt %d failed with exit_code=%d; retrying in %.0fs",
                attempt,
                exit_code,
                backoff_seconds,
            )
            time.sleep(backoff_seconds)

    _log.error(
        "gave up on %s after %d attempts (last exit_code=%d)", runtime_id, max_attempts, exit_code
    )
    _record_attempt(
        runtime_id=runtime_id,
        attempt=max_attempts,
        max_attempts=max_attempts,
        exit_code=exit_code,
        severity="ERROR",
        terminal=True,
        exception=last_exception,
    )
    return EXIT_GAVE_UP


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--runtime-id", default="intraday_options", help="Which runtime group to start."
    )
    parser.add_argument(
        "--config-root", type=Path, default=Path("config"), help="Root of the config/ tree."
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help="Retryable-failure attempts before giving up (default 3).",
    )
    parser.add_argument(
        "--backoff-seconds",
        type=float,
        default=DEFAULT_BACKOFF_SECONDS,
        help="Delay between retryable attempts, in seconds (default 30).",
    )
    args = parser.parse_args(argv)

    return run(
        runtime_id=args.runtime_id,
        config_root=args.config_root,
        max_attempts=args.max_attempts,
        backoff_seconds=args.backoff_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
