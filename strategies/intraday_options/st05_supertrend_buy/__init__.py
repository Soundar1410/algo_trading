"""``st05_supertrend_buy`` — SuperTrend(1, 0.5) on NIFTY 5-minute underlying
candles, BUY-only ATM weekly CE/PE, intraday. A faithful clone of
``st12_supertrend_buy`` (formerly ``supertrend_buy_1_1p2``), changing only the
SuperTrend multiplier (1.2 -> 0.5) and identity — see
``SUPERTREND_BUY_1_0P5_ALGO_TRADING_SPEC.md`` in this directory for the full
functional specification and ``st12_supertrend_buy``'s own spec for the
authoritative behaviour both strategies share.

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
