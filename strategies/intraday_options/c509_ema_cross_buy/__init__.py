"""``c509_ema_cross_buy`` — the second real intraday-options strategy, a faithful
clone of ``c921_ema_cross_buy`` (Phase 9's first real strategy) with only the
EMA periods (5/9 instead of 9/21) and identity changed. See
``c509_ema_cross_buy_spec.md`` in this directory for the full functional/design
spec, and ``c921_ema_cross_buy_spec.md`` (the sibling strategy's directory)
for the shared behavioural source of truth this strategy clones.

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
