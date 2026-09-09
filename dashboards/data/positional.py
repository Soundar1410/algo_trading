"""Read-model: the positional multi-leg cycle drill-down.

Not specific to ``weekly_delta_neutral`` — every function here is filtered
by ``strategy_id`` as an ordinary parameter and reads only migration
``0010``'s generic cycle tables (``strategy_cycles``,
``strategy_cycle_legs``, ``strategy_cycle_adjustments``,
``strategy_cycle_events``, ``cycle_position_bindings``), plus the
``order_intents``/``orders`` history already keyed by ``basket_id`` =
``cycle_id`` (migration 0009's own reuse note in ``0010``'s header). Any
future positional strategy reuses every function here unchanged.

Same convention as :mod:`dashboards.data.multi_leg`: every function takes an
already-open ``connect_readonly`` connection, so the page owns connection
lifetime via :func:`dashboards._shared.run_bounded` and every query here is
directly unit-testable against a migrated fixture database. No mark-to-
market is shown for an OPEN leg — same documented gap
:mod:`dashboards.data.multi_leg` already carries: this read-only layer has
no live tick source of its own.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

#: The Home page's category-tile message (``dashboards/Home.py``) for when no
#: strategy is enabled under this runtime group at all — distinct from, but
#: worded consistently with, ``dashboards.positional_options.NOT_CONFIGURED``
#: (the page-internal per-tab message), which imports this same string
#: rather than duplicating it.
NOT_CONFIGURED = (
    "No strategy is enabled under config/strategies/*.yaml with "
    "runtime_id: positional_options, and/or the runtime has never been "
    "started (data/operational/positional_options.db does not exist yet)."
)


@dataclass(frozen=True)
class CycleRow:
    """One weekly/positional cycle — the unit legs and adjustments attach
    to, and the durable identity that survives across trading dates."""

    cycle_id: str
    underlying: str
    resolved_expiry_date: str
    state: str
    entries_consumed: bool
    day_blocked_reason: str | None
    original_net_credit: float | None
    original_max_loss: float | None
    original_wing_width: float | None
    adjustments_today: int
    adjustments_this_cycle: int
    last_adjustment_at: str | None
    pending_adjustment_role: str | None
    pending_adjustment_state: str | None
    square_off_state: str
    opened_trading_date: str
    updated_at: str


@dataclass(frozen=True)
class LegRow:
    """One leg instance — original or a roll replacement — inside a cycle."""

    leg_id: str
    leg_role: str
    option_type: str | None
    leg_sequence: int
    is_replacement: bool
    replaces_leg_id: str | None
    security_id: str | None
    symbol: str | None
    strike: float | None
    expiry: str | None
    lot_size: int | None
    side: str | None
    quantity: int | None
    entry_price: float | None
    entry_time: str | None
    exit_price: float | None
    exit_time: str | None
    exit_reason: str | None
    realized_gross_pnl: float | None
    state: str
    #: Live mark, from ``cycle_leg_marks`` (migration 0015). ``None`` means no
    #: mark has ever been written for this leg — a leg that never opened, or a
    #: cycle predating the migration. Never conflated with a mark of zero.
    last_price: float | None = None
    unrealised_pnl: float | None = None
    mark_as_of: str | None = None
    #: Real charges from ``fills``, correlated by this leg's own entry/exit
    #: correlation ids. ``None`` means no fill exists yet, which is a
    #: different statement from "this leg cost nothing".
    entry_charges: float | None = None
    exit_charges: float | None = None

    @property
    def total_charges(self) -> float | None:
        if self.entry_charges is None and self.exit_charges is None:
            return None
        return (self.entry_charges or 0.0) + (self.exit_charges or 0.0)

    @property
    def net_pnl(self) -> float | None:
        """Gross (realised if closed, else the live mark) net of charges.

        ``None`` when there is no gross figure to net against — an open leg
        with no mark yet reports nothing rather than reporting its charges as
        a loss.
        """
        gross = self.realized_gross_pnl if self.state != "OPEN" else self.unrealised_pnl
        if gross is None:
            return None
        return gross - (self.total_charges or 0.0)


@dataclass(frozen=True)
class AdjustmentRow:
    """One completed adjustment attempt, whatever its outcome — the
    append-only ledger, never the in-flight claim (that lives on
    :class:`CycleRow`'s own ``pending_adjustment_role``/``_state``)."""

    adjustment_sequence: int
    trigger_reason: str
    target_leg_id: str
    replacement_leg_id: str | None
    claimed_at: str
    pre_adjustment_net_delta: float | None
    post_adjustment_net_delta: float | None
    realized_pnl: float | None
    charges: float | None
    lifecycle_state: str


@dataclass(frozen=True)
class CycleOrderRow:
    """One order this cycle placed, across every leg — the Orders & Fills
    tab's row shape."""

    leg_id: str | None
    correlation_id: str
    side: str
    quantity: int
    sequence_number: int
    order_status: str | None
    filled_quantity: int | None
    filled_price: float | None
    rejection_reason: str | None


def load_cycles(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    execution_mode: str,
    limit: int = 200,
) -> tuple[CycleRow, ...]:
    """Cycles for one strategy, most recently updated first."""
    rows = conn.execute(
        """
        SELECT * FROM strategy_cycles
        WHERE strategy_id = ? AND execution_mode = ?
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (strategy_id, execution_mode, limit),
    ).fetchall()
    return tuple(_row_to_cycle(row) for row in rows)


def load_open_cycle(
    conn: sqlite3.Connection, *, strategy_id: str, execution_mode: str
) -> CycleRow | None:
    """The one non-terminal cycle for this strategy/mode, if any — the
    Overview tab's primary subject."""
    row = conn.execute(
        """
        SELECT * FROM strategy_cycles
        WHERE strategy_id = ? AND execution_mode = ?
          AND state NOT IN ('COMPLETED', 'FAILED', 'ABANDONED')
        ORDER BY id DESC LIMIT 1
        """,
        (strategy_id, execution_mode),
    ).fetchone()
    return _row_to_cycle(row) if row is not None else None


def load_legs_for_cycle(conn: sqlite3.Connection, *, cycle_id: str) -> tuple[LegRow, ...]:
    """Every leg instance for one cycle — original and roll replacement
    alike, ordered by role then sequence so a replacement always follows the
    leg it replaced."""
    rows = conn.execute(
        """
        SELECT
            l.*,
            m.last_price     AS mark_last_price,
            m.unrealised_pnl AS mark_unrealised_pnl,
            m.as_of          AS mark_as_of,
            (SELECT SUM(f.charges) FROM fills f
              WHERE f.correlation_id = l.entry_correlation_id) AS entry_charges,
            (SELECT SUM(f.charges) FROM fills f
              WHERE f.correlation_id = l.exit_correlation_id)  AS exit_charges
        FROM strategy_cycle_legs l
        LEFT JOIN cycle_leg_marks m
               ON m.runtime_id = l.runtime_id
              AND m.strategy_id = l.strategy_id
              AND m.execution_mode = l.execution_mode
              AND m.cycle_id = l.cycle_id
              AND m.leg_id = l.leg_id
        WHERE l.cycle_id = ?
        ORDER BY l.leg_role, l.leg_sequence
        """,
        (cycle_id,),
    ).fetchall()
    return tuple(_row_to_leg(row) for row in rows)


def load_adjustments_for_cycle(
    conn: sqlite3.Connection, *, cycle_id: str
) -> tuple[AdjustmentRow, ...]:
    """The completed-adjustment ledger for one cycle, oldest first."""
    rows = conn.execute(
        """
        SELECT * FROM strategy_cycle_adjustments
        WHERE cycle_id = ?
        ORDER BY adjustment_sequence
        """,
        (cycle_id,),
    ).fetchall()
    return tuple(_row_to_adjustment(row) for row in rows)


def load_orders_for_cycle(conn: sqlite3.Connection, *, cycle_id: str) -> tuple[CycleOrderRow, ...]:
    """Every order this cycle has ever placed, across every leg —
    ``order_intents``/``orders`` correlated through ``basket_id = cycle_id``
    (migration 0010's own reuse of the straddle_920 columns), the same shape
    ``ExecutionRepository.cycle_order_history`` reads, most recent first."""
    rows = conn.execute(
        """
        SELECT
            oi.leg_id, oi.correlation_id, oi.side, oi.quantity, oi.sequence_number,
            o.status AS order_status,
            o.filled_quantity AS order_filled_quantity,
            o.average_fill_price AS order_average_fill_price,
            o.rejection_reason AS order_rejection_reason
        FROM order_intents oi
        LEFT JOIN orders o ON o.intent_id = oi.id
        WHERE oi.basket_id = ?
        ORDER BY oi.sequence_number DESC
        """,
        (cycle_id,),
    ).fetchall()
    return tuple(_row_to_order(row) for row in rows)


def _row_to_cycle(row: sqlite3.Row) -> CycleRow:
    return CycleRow(
        cycle_id=row["cycle_id"],
        underlying=row["underlying"],
        resolved_expiry_date=row["resolved_expiry_date"],
        state=row["state"],
        entries_consumed=bool(row["entries_consumed"]),
        day_blocked_reason=row["day_blocked_reason"],
        original_net_credit=row["original_net_credit"],
        original_max_loss=row["original_max_loss"],
        original_wing_width=row["original_wing_width"],
        adjustments_today=int(row["adjustments_today"]),
        adjustments_this_cycle=int(row["adjustments_this_cycle"]),
        last_adjustment_at=row["last_adjustment_at"],
        pending_adjustment_role=row["pending_adjustment_role"],
        pending_adjustment_state=row["pending_adjustment_state"],
        square_off_state=row["square_off_state"],
        opened_trading_date=row["opened_trading_date"],
        updated_at=row["updated_at"],
    )


def _optional(row: sqlite3.Row, column: str) -> Any:
    """A column that only the joined leg query selects.

    ``load_legs_for_cycle`` joins the marks and charges in, but ``LegRow`` is
    also built from plain ``strategy_cycle_legs`` rows elsewhere; those simply
    have no such key. Missing and NULL both mean "not known", which is
    deliberately not the same as zero.
    """
    try:
        return row[column]
    except (IndexError, KeyError):
        return None


def _row_to_leg(row: sqlite3.Row) -> LegRow:
    return LegRow(
        leg_id=row["leg_id"],
        leg_role=row["leg_role"],
        option_type=row["option_type"],
        leg_sequence=int(row["leg_sequence"]),
        is_replacement=bool(row["is_replacement"]),
        replaces_leg_id=row["replaces_leg_id"],
        security_id=row["security_id"],
        symbol=row["symbol"],
        strike=row["strike"],
        expiry=row["expiry"],
        lot_size=row["lot_size"],
        side=row["side"],
        quantity=row["quantity"],
        entry_price=row["entry_price"],
        entry_time=row["entry_time"],
        exit_price=row["exit_price"],
        exit_time=row["exit_time"],
        exit_reason=row["exit_reason"],
        realized_gross_pnl=row["realized_gross_pnl"],
        state=row["state"],
        last_price=_optional(row, "mark_last_price"),
        unrealised_pnl=_optional(row, "mark_unrealised_pnl"),
        mark_as_of=_optional(row, "mark_as_of"),
        entry_charges=_optional(row, "entry_charges"),
        exit_charges=_optional(row, "exit_charges"),
    )


def _row_to_adjustment(row: sqlite3.Row) -> AdjustmentRow:
    return AdjustmentRow(
        adjustment_sequence=int(row["adjustment_sequence"]),
        trigger_reason=row["trigger_reason"],
        target_leg_id=row["target_leg_id"],
        replacement_leg_id=row["replacement_leg_id"],
        claimed_at=row["claimed_at"],
        pre_adjustment_net_delta=row["pre_adjustment_net_delta"],
        post_adjustment_net_delta=row["post_adjustment_net_delta"],
        realized_pnl=row["realized_pnl"],
        charges=row["charges"],
        lifecycle_state=row["lifecycle_state"],
    )


def _row_to_order(row: sqlite3.Row) -> CycleOrderRow:
    return CycleOrderRow(
        leg_id=row["leg_id"],
        correlation_id=row["correlation_id"],
        side=row["side"],
        quantity=int(row["quantity"]),
        sequence_number=int(row["sequence_number"]),
        order_status=row["order_status"],
        filled_quantity=row["order_filled_quantity"],
        filled_price=row["order_average_fill_price"],
        rejection_reason=row["order_rejection_reason"],
    )
