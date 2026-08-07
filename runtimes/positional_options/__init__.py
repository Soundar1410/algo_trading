"""Positional options strategy group — placeholder package.

Deliberately empty of a supervisor, worker or config adapter. This is not an
oversight; it is Phase 5's D56 decision, corrected on review to distinguish
two things that were originally deferred together for one reason, and
narrowed further at Phase 6 Part 5 once one of the two was actually closed:

**Inert scaffolding — done since Phase 5.** ``config/runtimes/
positional_options.yaml`` makes ``runtime_id: positional_options`` a real,
loadable ``RuntimeConfig``, ``enabled: false``, matching the exact precedent
``config/runtimes/intraday_options.yaml`` set in Phase 1 (disabled "until the
Phase 1 walking skeleton exists to be started"). Nothing scans
``config/runtimes/*.yaml`` on its own, so this file and this package are both
inert — present to be loaded, not to be discovered automatically.

**The exit-timing question is answered — Phase 6 Part 4.**
``SquareOffPolicy`` gained ``expiry_policy``/``square_off_before_expiry_days``:
a contract held past its own expiry (minus a configurable lead) force-closes
regardless of time of day, composed into the same ``SquareOffAuthority``
seam every strategy already goes through. A positional strategy built on
today's schema would not be exit-blind the way it was when this docstring
was first written.

**A real supervisor/worker/persistence layer — still genuinely deferred, on
one remaining structural question, not "no consumer yet."** Unlike
``intraday_options`` at the equivalent point in Phase 1, this was never just
"no consumer" (the reason Deviation D34 gave for not porting
``EquityScripMaster`` early, and the reason ``MultiLegEngine``/
``FixedStrikeEngine`` waited for their first consumer):

* **Persistence identity is date-scoped and a positional strategy is not.**
  ``positions``, ``strategy_state`` and ``order_intents`` all carry
  ``trading_date`` in their UNIQUE key (migration ``0001``). A position held
  across several sessions would fragment across that many unrelated rows
  under the current schema. **D69** (Phase 6 Part 5) records a candidate
  direction — an additional ``cycle_id`` column, ``trading_date`` left
  untouched — but deliberately stops at documenting it: the blast radius
  (every recovery function, 6+ ``ExecutionRepository`` methods, dozens of
  existing tests) and the unanswered question of what "a cycle" operationally
  means for a real positional strategy make building it now speculative
  rather than incremental. See runbook limitation 30.

Building a supervisor/worker here before that question has a real answer
would mean either reusing same-day-shaped persistence incorrectly (silently
wrong — a held position fragmenting across daily rows) or committing to a
persistence design no real strategy has yet validated.

Revisit once Phase 9 has a real positional strategy whose actual session/
rollover/adjustment shape can validate D69's candidate direction — or
replace it.
"""

from __future__ import annotations
