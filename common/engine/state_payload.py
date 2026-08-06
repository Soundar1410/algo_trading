"""The free-form ``strategy_state.payload`` column, read and written in one place.

Phase 3 Part 2b-ii-B-2. Restart recovery for the engine needs something the
``positions`` row cannot express. That row carries ``instrument``, ``security_id``,
``quantity`` and ``average_price`` — but not the option type, strike, expiry or lot
size, so an :class:`~common.engine.models.OptionContract` cannot be rebuilt from it
and a restarted engine could not manage the position it already holds.

``strategy_state.payload`` already exists for exactly this kind of state (migration
``0001_walking_skeleton.sql``), so nothing is added to the schema. **No migration:**
SQLite's ``ALTER TABLE ADD COLUMN`` is not ``IF NOT EXISTS``-able and would break
D6 replay-safety.

Two traps this module exists to handle
--------------------------------------
Both are properties of
:meth:`~common.execution.repository.ExecutionRepository.save_strategy_state`, and
both are silent rather than loud, which is why the handling is centralised here
rather than repeated at each call site.

**1. ``payload=None`` does not clear the column.** The upsert reads
``payload = COALESCE(excluded.payload, payload)``, so passing ``None`` *preserves*
whatever was there. Clearing a key therefore means writing a payload that no longer
contains it — never passing ``None`` for the column. :func:`merge_payload` treats a
``None`` value as "remove this key", and an emptied payload is written as ``{}``,
which is a non-NULL string and so does replace.

**2. A write replaces the whole column.** Two writers with different keys —
:class:`~common.engine.gateway.LifecycleGateway` writing ``open_position`` and
:class:`~common.engine.reporting.RepositoryReportWriter` writing ``day_summary`` —
would clobber each other if either wrote a bare dict. Every write here is
read-modify-write, so the keys coexist.

The payload is namespaced by key rather than being a bare record, so a future
writer can add one without either of the above becoming a bug again.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from common.config.models import ExecutionMode
from common.logging import get_logger

if TYPE_CHECKING:  # typing only: keeps common.execution out of the import graph
    from common.execution.repository import ExecutionRepository

log = get_logger(__name__)

#: The open position's contract record, written by ``LifecycleGateway`` on open and
#: removed on close. Read by the worker to rebuild an ``OptionContract`` at startup.
OPEN_POSITION_KEY = "open_position"

#: The day's closed-trade summary, written by ``RepositoryReportWriter`` at
#: ``_end_day``. Deliberately *not* a second copy of the trades themselves — see
#: that class for why.
DAY_SUMMARY_KEY = "day_summary"

#: The open position's exit-policy state (a trailing peak, a reversal streak),
#: written after every candle close while a position is open and cleared on
#: close — Phase 6 Part 2. Shape: ``{"security_id": <open contract's>, "state":
#: <the strategy's own exit_state_snapshot()>}``. The ``security_id`` wrapper
#: reuses exactly the mismatch check ``OPEN_POSITION_KEY`` already needs (a
#: snapshot from a different contract must be refused), rather than inventing a
#: second comparison for this key.
EXIT_STATE_KEY = "exit_state"


def read_payload(
    repository: ExecutionRepository,
    *,
    strategy_id: str,
    execution_mode: ExecutionMode,
    trading_date: str,
) -> dict[str, Any]:
    """The decoded payload for one strategy-day, or ``{}`` if there is none.

    Never raises on bad data. A payload that will not decode is a state file this
    process did not write or a build that wrote a different shape; refusing to start
    over it would turn a cosmetic problem into an outage, while the caller's own
    absence handling (no recovered position, no prior summary) is already correct.
    """
    row = repository.load_strategy_state(
        strategy_id=strategy_id,
        execution_mode=execution_mode,
        trading_date=trading_date,
    )
    if row is None:
        return {}
    try:
        raw = row["payload"]
    except (IndexError, KeyError):  # pragma: no cover - the column exists
        return {}
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        log.warning(
            "unreadable strategy_state.payload for %s on %s; treating it as empty",
            strategy_id,
            trading_date,
        )
        return {}
    if not isinstance(decoded, dict):
        log.warning(
            "strategy_state.payload for %s on %s is a %s, not an object; ignoring it",
            strategy_id,
            trading_date,
            type(decoded).__name__,
        )
        return {}
    return decoded


def merge_payload(
    repository: ExecutionRepository,
    updates: dict[str, Any],
    *,
    runtime_id: str,
    strategy_id: str,
    execution_mode: ExecutionMode,
    trading_date: str,
) -> dict[str, Any]:
    """Merge ``updates`` into the stored payload and write it back.

    A value of ``None`` **removes** its key rather than storing a null, so clearing
    is expressed the same way everywhere and an emptied payload still writes ``{}``
    — which the column's ``COALESCE`` accepts, where a bare ``None`` would not.

    Returns the payload as written, so a caller can assert on it without a re-read.
    """
    payload = read_payload(
        repository,
        strategy_id=strategy_id,
        execution_mode=execution_mode,
        trading_date=trading_date,
    )
    for key, value in updates.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value

    repository.save_strategy_state(
        runtime_id=runtime_id,
        strategy_id=strategy_id,
        execution_mode=execution_mode,
        trading_date=trading_date,
        payload=payload,
    )
    return payload
