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
from common.models import CURRENT_STATE_VERSION

if TYPE_CHECKING:  # typing only: keeps common.execution out of the import graph
    from common.execution.repository import ExecutionRepository

log = get_logger(__name__)


class UnsupportedStateVersion(RuntimeError):
    """``strategy_state.state_version`` names a version this build does not understand.

    Phase 6 Part 3. The one place :func:`read_payload`'s "never raises on bad
    data" rule is deliberately narrowed: a payload that merely fails to decode
    (bad JSON, wrong type) is cosmetic — this build has nothing to read, which
    every caller's own absence handling already covers correctly. A *version*
    mismatch is different in kind: the payload decodes fine and looks
    plausible, but its shape was written by a build this one has no guarantee
    of agreeing with, so silently reading it risks confidently misinterpreting
    a foreign shape rather than visibly finding nothing.
    """

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

    Never raises on bad *data*. A payload that will not decode is a state file
    this process did not write or a build that wrote a different shape;
    refusing to start over it would turn a cosmetic problem into an outage,
    while the caller's own absence handling (no recovered position, no prior
    summary) is already correct.

    **Raises :class:`UnsupportedStateVersion` on a bad *version*, deliberately —
    the one narrowing of the rule above.** ``state_version`` names the shape
    the payload was written against; a value other than
    :data:`common.models.CURRENT_STATE_VERSION` means this build has no
    guarantee it can read that shape correctly. Every existing caller already
    has a defined severity for "this key's data is unusable" — fail-closed for
    ``OPEN_POSITION_KEY`` via ``recover_position``, fail-open for
    ``EXIT_STATE_KEY`` via ``recover_exit_state`` (Part 2, D60) — so raising
    here lets each one inherit exactly that severity for free, rather than
    needing a special case per key.
    """
    row = repository.load_strategy_state(
        strategy_id=strategy_id,
        execution_mode=execution_mode,
        trading_date=trading_date,
    )
    if row is None:
        return {}
    stored_version = row["state_version"]
    if stored_version is not None and stored_version != CURRENT_STATE_VERSION:
        raise UnsupportedStateVersion(
            f"strategy_state.state_version={stored_version!r} for {strategy_id} on "
            f"{trading_date} is not {CURRENT_STATE_VERSION!r}, the only version this "
            "build understands; refusing to read the payload rather than risk "
            "misinterpreting its shape"
        )
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
    last_candle_end_at: str | None = None,
) -> dict[str, Any]:
    """Merge ``updates`` into the stored payload and write it back.

    A value of ``None`` **removes** its key rather than storing a null, so clearing
    is expressed the same way everywhere and an emptied payload still writes ``{}``
    — which the column's ``COALESCE`` accepts, where a bare ``None`` would not.

    ``last_candle_end_at`` (Phase 6 Part 3, optional) is threaded straight to
    :meth:`~common.execution.repository.ExecutionRepository.save_strategy_state`
    in the **same** write as the payload, rather than a second transaction at
    the same checkpoint — the caller (``TradingEngine._persist_exit_state``)
    already writes both from one per-candle checkpoint, and a second write
    would double the frequency limitation 24 already flags as unmeasured.

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
        last_candle_end_at=last_candle_end_at,
    )
    return payload
