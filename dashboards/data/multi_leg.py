"""Read-model: the generic multi-leg basket/leg drill-down.

Not specific to ``straddle_920`` — every function here is filtered by
``strategy_id`` as an ordinary parameter, reads only the generic
``strategy_baskets``/``strategy_legs`` tables (migration ``0009``), and is
directly reusable by the next multi-leg strategy with zero changes here.
``straddle_920``-specific labels (leg role names, the original/combined-stop
basis figures) are exposed as typed data fields for the page to render —
never hardcoded SQL or a page branch naming this one strategy.

Same convention as :mod:`dashboards.data.intraday_options`: every function
takes an already-open ``connect_readonly`` connection, so the page owns
connection lifetime via :func:`dashboards._shared.run_bounded` and every
query here is directly unit-testable against a migrated fixture database.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class BasketRow:
    """One logical basket — e.g. one day's straddle."""

    basket_id: str
    trading_date: str
    lifecycle_state: str
    entries_consumed: bool
    day_blocked_reason: str | None
    adjustment_count: int
    pending_replacement_role: str | None
    pending_replacement_state: str | None
    original_combined_basis: float | None
    square_off_state: str
    updated_at: str


@dataclass(frozen=True)
class LegRow:
    """One leg instance — original or replacement — inside a basket."""

    leg_id: str
    leg_role: str
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
    #: Gross unrealised P&L at the last mark this leg was updated with — only
    #: meaningful while ``state == "OPEN"``; the caller supplies the mark
    #: (see :func:`load_legs_for_basket`'s docstring), since this read-model
    #: has no live tick source of its own.
    unrealised_gross_pnl: float | None
    #: Reporting-only, read from the append-only trade ledger's own charge
    #: columns when this leg's close is found there — never used to compute
    #: ``realized_gross_pnl`` itself, which is the strategy's gross risk
    #: figure and must stay charge-free.
    charges: float | None
    net_pnl: float | None


def load_baskets(
    conn: sqlite3.Connection,
    *,
    strategy_id: str,
    execution_mode: str,
    trading_date: str | None = None,
    limit: int = 200,
) -> tuple[BasketRow, ...]:
    """Baskets for one strategy, most recent trading date first."""
    if trading_date is not None:
        rows = conn.execute(
            """
            SELECT * FROM strategy_baskets
            WHERE strategy_id = ? AND execution_mode = ? AND trading_date = ?
            ORDER BY basket_id
            """,
            (strategy_id, execution_mode, trading_date),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM strategy_baskets
            WHERE strategy_id = ? AND execution_mode = ?
            ORDER BY trading_date DESC, basket_id
            LIMIT ?
            """,
            (strategy_id, execution_mode, limit),
        ).fetchall()
    return tuple(_row_to_basket(row) for row in rows)


def load_legs_for_basket(conn: sqlite3.Connection, *, basket_id: str) -> tuple[LegRow, ...]:
    """Every leg instance for one basket — original and replacement alike,
    ordered by role then sequence so a replacement always follows the leg it
    replaced.

    Charges/net P&L come from ``trade_ledger`` when a matching closed row
    exists (joined by ``entry_correlation_id``, the identifier
    ``strategy_legs`` and ``trade_ledger`` both carry for the same fill) —
    reporting-only, never fed back into the gross ``realized_gross_pnl`` the
    strategy's own risk formulas use.
    """
    rows = conn.execute(
        """
        SELECT * FROM strategy_legs
        WHERE basket_id = ?
        ORDER BY leg_role, leg_sequence
        """,
        (basket_id,),
    ).fetchall()
    return tuple(_row_to_leg(conn, row) for row in rows)


def _row_to_basket(row: sqlite3.Row) -> BasketRow:
    return BasketRow(
        basket_id=row["basket_id"],
        trading_date=row["trading_date"],
        lifecycle_state=row["lifecycle_state"],
        entries_consumed=bool(row["entries_consumed"]),
        day_blocked_reason=row["day_blocked_reason"],
        adjustment_count=int(row["adjustment_count"]),
        pending_replacement_role=row["pending_replacement_role"],
        pending_replacement_state=row["pending_replacement_state"],
        original_combined_basis=row["original_combined_basis"],
        square_off_state=row["square_off_state"],
        updated_at=row["updated_at"],
    )


def _row_to_leg(conn: sqlite3.Connection, row: sqlite3.Row) -> LegRow:
    charges: float | None = None
    net_pnl: float | None = None
    if row["state"] == "CLOSED" and row["entry_correlation_id"]:
        ledger = conn.execute(
            """
            SELECT entry_charges, exit_charges, gross_pnl FROM trade_ledger
            WHERE entry_correlation_id = ? AND security_id = ?
            ORDER BY closed_at DESC LIMIT 1
            """,
            (row["entry_correlation_id"], row["security_id"]),
        ).fetchone()
        if ledger is not None:
            charges = float(ledger["entry_charges"]) + float(ledger["exit_charges"])
            net_pnl = float(ledger["gross_pnl"]) - charges

    return LegRow(
        leg_id=row["leg_id"],
        leg_role=row["leg_role"],
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
        # No live mark source in this read-only layer (spec's own documented
        # gap — see dashboards/intraday_options.py's NOT_YET_AVAILABLE note
        # for the identical limitation on the single-leg page): an OPEN
        # leg's unrealised P&L is not shown here rather than shown stale.
        unrealised_gross_pnl=None,
        charges=charges,
        net_pnl=net_pnl,
    )
