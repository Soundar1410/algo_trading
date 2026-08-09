"""``ema_cross_9_21_buy`` — Phase 9's first (and, per CLAUDE.md, only) real
intraday-options strategy. See ``ema_cross_9_21_buy_spec.md`` in this directory
for the full functional/design spec.

**Deliberately not imported by** :mod:`strategies.intraday_options` (the
parent package's ``__init__.py``). This module's :mod:`.strategy` pulls in
:mod:`common.engine`, :mod:`common.exit` and :mod:`common.indicators` —
exactly the graph ``tests/unit/test_worker_import_boundary.py`` keeps out of
every spawned worker's module-level imports (see that test's module
docstring). The real engine path reaches this strategy the same way it
reaches every other engine-contract strategy: a dotted ``strategy_ref`` string
resolved by ``runtimes.intraday_options.engine_worker.load_strategy``, from
inside the deferred engine branch — never a package-level import.
"""

from __future__ import annotations
