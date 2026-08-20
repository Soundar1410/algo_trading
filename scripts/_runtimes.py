"""Runtime-id -> entrypoint registry: the one authoritative runtime table.

Before this module, ``start_runtime.py`` and ``start_strategy.py`` both
imported ``runtimes.intraday_options.__main__.main`` unconditionally and called
it with whatever ``--runtime-id`` the operator passed — harmless while
``intraday_options`` was the only real runtime, but wrong the moment a second
runtime with its own composition root existed: ``scripts.start_runtime
positional_options`` would drive ``positional_options``'s strategies through
*intraday's* worker/engine wiring, admitting them under the wrong supervisor
entirely.

The same registry now carries each runtime's **exit-code classification**,
because ``orchestration.process_control.supervised_launch`` had the identical
bug one level up: it imported intraday's ``__main__`` as the universal
supervisor *and* as the universal source of exit-code meanings. That second
half is the more dangerous one, because the two runtimes genuinely disagree on
the numbers:

===========================  ==================  ====================
meaning                      intraday_options    positional_options
===========================  ==================  ====================
``EXIT_RUNTIME_DISABLED``    3                   10
``EXIT_NO_CREDENTIALS``      2                   11
``EXIT_LEGACY_SYSTEM_ACTIVE``5                   12
``EXIT_STRATEGY_NOT_FOUND``  4                   13
===========================  ==================  ====================

Read through intraday's table, positional's ``10`` (disabled — a structural
refusal that must stop immediately) is not in any known set and positional's
``11`` (no credentials) would be classified as ``EXIT_STRATEGY_NOT_FOUND``.
A runtime that is deliberately disabled would be retried until the attempt
budget ran out. Each entry below therefore cites its **own** module's
constants, so adding a third runtime is one entry here and no branch anywhere.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import runtimes.intraday_options.__main__ as _intraday_main
import runtimes.positional_options.__main__ as _positional_main

# EXIT_OK is only re-exported by positional's __main__; take it from the module
# that actually defines it so this stays a real, checkable reference.
from runtimes.positional_options.supervisor import EXIT_OK as _POSITIONAL_EXIT_OK


@dataclass(frozen=True)
class RuntimeEntrypoint:
    """One runtime group's composition root and its own exit-code meanings."""

    main: Callable[[list[str] | None], int]
    #: Codes that must never be retried: a clean stop, a disabled runtime, a
    #: missing strategy, a detected legacy system, a deliberate safety
    #: shutdown. None of these are different on a second attempt.
    terminal_exit_codes: frozenset[int]
    #: Codes worth one more attempt — a transient auth/network failure, not a
    #: structural refusal to start.
    retryable_exit_codes: frozenset[int]

    def classify(self, exit_code: int) -> str:
        """``"terminal"``, ``"retryable"``, or ``"unknown"``.

        An unrecognised code is deliberately **not** folded into either set.
        Treating it as retryable would loop on a failure nobody has classified;
        treating it as terminal silently would hide it. Callers report it.
        """
        if exit_code in self.terminal_exit_codes:
            return "terminal"
        if exit_code in self.retryable_exit_codes:
            return "retryable"
        return "unknown"


RUNTIMES: dict[str, RuntimeEntrypoint] = {
    "intraday_options": RuntimeEntrypoint(
        main=_intraday_main.main,
        terminal_exit_codes=frozenset(
            {
                _intraday_main.EXIT_OK,
                _intraday_main.EXIT_RUNTIME_DISABLED,
                _intraday_main.EXIT_STRATEGY_NOT_FOUND,
                _intraday_main.EXIT_LEGACY_SYSTEM_ACTIVE,
                _intraday_main.EXIT_SAFETY_SHUTDOWN,
            }
        ),
        retryable_exit_codes=frozenset(
            {_intraday_main.EXIT_FAILED, _intraday_main.EXIT_NO_CREDENTIALS}
        ),
    ),
    "positional_options": RuntimeEntrypoint(
        main=_positional_main.main,
        terminal_exit_codes=frozenset(
            {
                _POSITIONAL_EXIT_OK,
                _positional_main.EXIT_RUNTIME_DISABLED,
                _positional_main.EXIT_STRATEGY_NOT_FOUND,
                _positional_main.EXIT_LEGACY_SYSTEM_ACTIVE,
            }
        ),
        retryable_exit_codes=frozenset(
            {_positional_main.EXIT_FAILED, _positional_main.EXIT_NO_CREDENTIALS}
        ),
    ),
}

#: There is deliberately no second ``ENTRYPOINTS`` mapping alongside
#: :data:`RUNTIMES`. A derived copy is a drift hazard of exactly the kind this
#: module exists to remove: a caller (or a test) mutating one would leave the
#: other stale and silently authoritative for whoever read it. One table.


def resolve_runtime(runtime_id: str) -> RuntimeEntrypoint:
    """This runtime's entrypoint *and* its exit-code classification.

    Raises :class:`KeyError` with every known id listed, rather than silently
    falling back to ``intraday_options``'s — the exact mistake this module
    exists to close.
    """
    try:
        return RUNTIMES[runtime_id]
    except KeyError:
        raise KeyError(
            f"Unknown runtime_id {runtime_id!r}. Known runtimes: {sorted(RUNTIMES)}."
        ) from None


def resolve_entrypoint(runtime_id: str) -> Callable[[list[str] | None], int]:
    """The ``main()`` this ``runtime_id`` actually runs under."""
    return resolve_runtime(runtime_id).main
