"""Vocabulary for the operator-action audit trail migration 0004 shipped.

Mirrors :mod:`common.execution.health_events`'s own reasoning exactly: a
Python-side mirror of ``audit_events.action``'s CHECK constraint, so a caller
gets a clear :class:`ValueError` before ever reaching SQLite, and the
vocabulary lives in exactly one place rather than being reinvented at each
operator script that writes one.

Deliberately has no dependency on :mod:`common.execution.repository` — a leaf
module either that module or a script may import without risking a cycle.
"""

from __future__ import annotations

AUDIT_ACTIONS: frozenset[str] = frozenset(
    {
        "stop_runtime",
        "stop_strategy",
        "square_off_requested",
        "square_off_completed",
    }
)
