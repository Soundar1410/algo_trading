"""Intraday options strategies. Still test-only fixtures — real strategies are Phase 9.

There are two, for the two seams this repository has, but only one is exported here:

* :class:`FixtureSignalStrategy` — the worker's seam (one completed candle in, at
  most one persisted-shape signal out). Phase 1's walking skeleton runs on it and
  keeps doing so, deliberately, so an engine regression cannot be masked by the
  test harness changing underneath it at the same time.
* ``EngineFixtureStrategy`` — the ported engine's richer contract (ticks, premium
  candles, a risk manager, a warm-up spec). Phase 3 Part 2b-i. Import it from
  :mod:`strategies.intraday_options.engine_fixture_strategy` directly.

The second is **deliberately not re-exported here**, and that is load-bearing
rather than tidy. ``runtimes/intraday_options/worker.py`` imports this package, and
every worker is a ``spawn``ed process that re-imports it from scratch. Re-exporting
the engine fixture drags ``common.engine`` — and through it the exit registry and
the indicators — into the import graph of every worker that will never use them,
measured at +0.22s per child. That is not merely waste: it was enough to push a
spawned child past the window in ``test_duplicate_worker_startup_is_refused``,
where the lock holder exits after 0.5s of an idle queue. See the runbook's Part 2b-i
notes for that finding.
"""

from __future__ import annotations

from .fixture_strategy import FixtureSignalStrategy, Strategy

__all__ = ["FixtureSignalStrategy", "Strategy"]
