"""Positional options strategy group — placeholder package.

Deliberately empty of a supervisor, worker or config adapter. This is not an
oversight; it is Phase 5's D56 decision, corrected on review to distinguish
two things that were originally deferred together for one reason:

**Inert scaffolding — done now.** ``config/runtimes/positional_options.yaml``
makes ``runtime_id: positional_options`` a real, loadable ``RuntimeConfig``,
``enabled: false``, matching the exact precedent
``config/runtimes/intraday_options.yaml`` set in Phase 1 (disabled "until the
Phase 1 walking skeleton exists to be started"). Nothing scans
``config/runtimes/*.yaml`` on its own, so this file and this package are both
inert — present to be loaded, not to be discovered automatically.

**A real supervisor/worker/persistence layer — genuinely deferred, not
merely unwired.** Unlike ``intraday_options`` at the equivalent point in
Phase 1, this is not just "no consumer yet" (the reason Deviation D34 gave
for not porting ``EquityScripMaster`` early, and the reason
``MultiLegEngine``/``FixedStrikeEngine`` waited for their first consumer).
Two design questions block it structurally, and both are explicitly Phase 6's
job, not this package's to invent ahead of order:

* **Persistence identity is date-scoped and a positional strategy is not.**
  ``positions``, ``strategy_state`` and ``order_intents`` all carry
  ``trading_date`` in their UNIQUE key (migration ``0001``). A position held
  across several sessions would fragment across that many unrelated rows
  under the current schema — Phase 6's "restore open paper positions and
  strategy/risk state by strategy and mode" (notably not "...by strategy,
  mode, and date") is where that gets a real answer.
* **``SquareOffPolicy`` is same-day wall-clock exit, full stop.**
  ``entry_cutoff``/``square_off_at`` have no "hold and re-evaluate tomorrow"
  concept. Phase 6's ``force_square_off_before_expiry`` and its
  exchange-settlement simulation are what a positional strategy's exit
  actually needs, and neither exists yet.

Building a supervisor/worker here before Phase 6 answers those two questions
would mean either reusing same-day-shaped persistence incorrectly (silently
wrong — a held position fragmenting across daily rows) or inventing new
persistence/recovery semantics ungoverned by any spec section but Phase 6's
own not-yet-reached bullets, which is doing Phase 6's work out of order.

Revisit once Phase 6 has answered both questions and Phase 9 has a real
positional strategy to build against.
"""

from __future__ import annotations
