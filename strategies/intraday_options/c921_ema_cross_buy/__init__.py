"""``c921_ema_cross_buy`` — Phase 9's first real intraday-options strategy. See
``c921_ema_cross_buy_spec.md`` in this directory for the full functional/design
spec. A second real strategy, ``c509_ema_cross_buy`` (a faithful clone with
EMA periods 5/9 instead of 9/21), lives alongside this one in
``strategies/intraday_options/c509_ema_cross_buy/``.

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
