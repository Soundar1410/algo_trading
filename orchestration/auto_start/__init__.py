"""Unattended PAPER-mode daily startup.

One controller owns the whole ordered chain, in one process:

    system-timezone check
      -> trading-day / start-window gate      (no network on a weekend or holiday)
      -> project root and .venv availability  (the volume may mount late)
      -> paper-safety + legacy guard + per-runtime environment validation
      -> authentication, retried until the configured session deadline
      -> Dhan validation through the read-only profile endpoint
      -> exactly one Telegram success notification per trading date
      -> spawn and then *supervise* one process per enabled runtime group

The ordering is structural, not a race: steps 7 and 8 are reached from one
function, after step 6, or not at all. That is why this replaced the previous
``auth`` @08:45 + ``intraday_options`` @09:00 pair of LaunchAgents, which had
two independent triggers and no way to express "not before authentication".

Nothing here decides *what* runs. Runtime and strategy ``enabled:`` flags are
the only authority on that, and this package cannot override them — it reads
them through :func:`common.config.discover_enabled_strategies` and
:func:`common.config.load_runtime_config` like everything else.

PAPER only. :mod:`orchestration.auto_start.paper_safety` refuses to start
anything at all unless every live gate is off, and no code path here can
reroute a live-designated strategy into paper.
"""

from __future__ import annotations

from .gate import StartDecision, evaluate_start_window, system_timezone_matches
from .retry import DeadlineWaiter, Retryability, classify

__all__ = [
    "DeadlineWaiter",
    "Retryability",
    "StartDecision",
    "classify",
    "evaluate_start_window",
    "system_timezone_matches",
]
