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
before touching the feed at all) and then **that runtime's own composition
root**, resolved through :func:`scripts._runtimes.resolve_runtime`, up to
``--max-attempts`` times. Exit codes split into two groups:

* **Terminal** — stop immediately: a clean stop, a disabled runtime, a missing
  strategy, a detected legacy system, a deliberate safety shutdown. None of
  these mean "try again": a disabled runtime is still disabled next attempt,
  and a deliberate shutdown must never loop — spec section 12's own words.
* **Retryable, bounded** — a transient auth/network failure, not a structural
  refusal to start. Retried up to ``--max-attempts`` times with a fixed
  backoff between attempts.

The *numbers* behind those groups are per runtime and deliberately not
hardcoded here: ``intraday_options`` and ``positional_options`` disagree on
almost all of them (positional's disabled is ``10``, intraday's is ``3``), so
each runtime's own constants are read from the registry. Until this was fixed,
this module imported intraday's ``__main__`` as both the universal entrypoint
*and* the universal exit-code vocabulary, which meant ``--runtime-id
positional_options`` ran intraday's supervisor and then misread its own
runtime's codes — a deliberately disabled positional runtime would have been
retried until the attempt budget ran out. An unrecognised code is reported as
unknown and treated as terminal rather than looped on.

**Exhaustion is final for the day.** Every runtime LaunchAgent this project
generates sets ``KeepAlive=false``, so ``launchd`` does *not* restart this
process when it exits, and no ``ThrottleInterval`` is set. Recovery from an
outage that outlasts every in-process retry comes from the next scheduled
``orchestration.auto_start`` trigger, not from ``launchd`` restarting this.
An earlier version of this docstring claimed the opposite; it was wrong.

An **unexpected exception** raised out of the runtime's ``main`` itself
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
from scripts import validate_environment
from scripts._operator_common import open_audit_repository
from scripts._runtimes import RuntimeEntrypoint, resolve_runtime

_log = get_logger(__name__)

#: A generic failure code used when this wrapper never reached the runtime at
#: all (an unknown runtime id, say). Deliberately *not* one runtime's own
#: ``EXIT_FAILED``: the two real runtimes happen to agree that 1 means failure,
#: but nothing in the registry requires a third one to.
EXIT_CODE_UNREACHED = 1

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


def _resolve(runtime_id: str) -> RuntimeEntrypoint | None:
    """This runtime's entrypoint, or ``None`` after logging an unknown id.

    An unsupported runtime id is a terminal configuration error, never
    something to retry — and never, as it once was, a silent fallback to
    ``intraday_options``'s composition root.
    """
    try:
        return resolve_runtime(runtime_id)
    except KeyError as exc:
        _log.error("refusing to start: %s", exc)
        return None


def run(
    *,
    runtime_id: str,
    config_root: Path,
    max_attempts: int,
    backoff_seconds: float,
) -> int:
    runtime = _resolve(runtime_id)
    if runtime is None:
        return EXIT_PREFLIGHT_FAILED

    if not _run_preflight(runtime_id):
        _log.error("preflight failed; refusing to start %s", runtime_id)
        return EXIT_PREFLIGHT_FAILED

    exit_code = EXIT_CODE_UNREACHED
    last_exception: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        _log.info("supervised launch attempt %d/%d for %s", attempt, max_attempts, runtime_id)
        last_exception = None
        try:
            exit_code = runtime.main(
                ["--runtime-id", runtime_id, "--config-root", str(config_root)]
            )
        except Exception as exc:  # intentionally broad — see module docstring
            # Deliberately not `except BaseException`: SystemExit/KeyboardInterrupt
            # must keep propagating untouched, never be folded into this retry path.
            last_exception = exc
            exit_code = EXIT_CODE_UNREACHED
            _log.exception(
                "supervised launch attempt %d/%d for %s raised an unexpected exception",
                attempt,
                max_attempts,
                runtime_id,
            )

        classification = runtime.classify(exit_code)
        if last_exception is None and classification == "unknown":
            # Not in either set. Looping on a code nobody has classified would
            # hide it behind the attempt budget, so stop and say so.
            _log.error(
                "%s returned unclassified exit_code=%d; treating it as terminal",
                runtime_id,
                exit_code,
            )
        if last_exception is None and classification != "retryable":
            severity = "INFO" if classification == "terminal" and exit_code == 0 else "WARNING"
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
