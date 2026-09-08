"""``st12_supertrend_buy`` — SuperTrend(1, 1.2) on NIFTY 5-minute underlying
candles, BUY-only ATM weekly CE/PE, intraday. Renamed from
``supertrend_buy_1_1p2`` on 8 September 2026 to fix a correlation-ID token
collision with the new ``st05_supertrend_buy`` variant (see the dated
addendum in ``docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md``); no behaviour
changed. See ``SUPERTREND_BUY_1_1P2_ALGO_TRADING_SPEC.md`` in this directory
for the full functional specification; it, and the legacy ``supertrend_fast``
code it was extracted from, are the authoritative sources — not this
docstring.

**Deliberately not imported by** :mod:`strategies.intraday_options` (the
parent package's ``__init__.py``), and this file deliberately does not import
:mod:`.strategy` either. That module pulls in :mod:`common.engine`,
:mod:`common.exit` and :mod:`common.indicators` — exactly the graph
``tests/unit/test_worker_import_boundary.py`` keeps out of every spawned
worker's module-level imports. The real engine path reaches this strategy the
same way it reaches ``c921_ema_cross_buy``: a dotted ``strategy_ref`` string
resolved by ``runtimes.intraday_options.engine_worker.load_strategy``, from
inside the deferred engine branch — never a package-level import.
"""

from __future__ import annotations
