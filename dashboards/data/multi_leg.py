"""Read-model: the generic multi-leg basket/leg drill-down.

Not specific to ``straddle_920`` — every function here is filtered by
``strategy_id`` as an ordinary parameter, reads only the generic
``strategy_baskets``/``strategy_legs`` tables (migration ``0009``), and is
directly reusable by the next multi-leg strategy with zero changes here.
``straddle_920``-specific labels (leg role names, the original/combined-stop
basis figures) are exposed as typed data fields for the page to render —
never hardcoded SQL or a page branch naming this one strategy.

:class:`RollRow`/:class:`RollAnchorRow` (Phase 5,
``strategy-rolling-strangle-otm1``) extend this same generic read-model to
migration ``0013``'s ``strategy_basket_rolls``/``strategy_basket_roll_
anchor`` — durable repeated-roll history first needed by
``rolling_strangle_otm1``, but keyed only by ``basket_id`` exactly like
every other function here, so any multi-leg strategy that ever claims a
roll (``straddle_920``'s own single adjustment included, since Phase 2
normalises it into the same claim machinery) is covered with zero further
changes.

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
    exit_correlation_id: str | None
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
    exists — joined by ``exit_correlation_id`` (P0-3 correction), the
    authoritative, globally-unique identifier the closing order actually
    persisted under (``trade_ledger.exit_correlation_id`` is ``NOT NULL
    UNIQUE`` with ``exit_broker_fill_id`` — exactly one row per real
    closing fill), rather than the previous ``(entry_correlation_id,
    security_id)`` approximation, which could not distinguish two different
    closes of the same security on the same day and had to guess the most
    recent one. Reporting-only, never fed back into the gross
    ``realized_gross_pnl`` the strategy's own risk formulas use.
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
    exit_correlation_id = row["exit_correlation_id"]
    if row["state"] == "CLOSED" and exit_correlation_id:
        ledger = conn.execute(
            """
            SELECT entry_charges, exit_charges, gross_pnl FROM trade_ledger
            WHERE exit_correlation_id = ?
            """,
            (exit_correlation_id,),
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
        exit_correlation_id=exit_correlation_id,
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


@dataclass(frozen=True)
class RollRow:
    """One durable roll claim (migration ``0013``'s ``strategy_basket_
    rolls``) — generic to any multi-leg strategy's repeated-roll history.
    ``target_*``/``replacement_*`` are read from ``strategy_legs`` by leg
    id; a leg id with no matching row (not expected in production, but the
    read must not raise) comes back ``None``, which the page renders as
    ``—`` rather than guessing."""

    leg_role: str
    roll_sequence: int
    lifecycle_state: str
    target_leg_id: str
    target_strike: float | None
    target_symbol: str | None
    replacement_leg_id: str | None
    replacement_strike: float | None
    replacement_symbol: str | None
    reference_price_at_claim: float | None
    claim_candle_ts: str
    claimed_at: str
    close_correlation_id: str | None
    close_intent_id: int | None
    updated_at: str


@dataclass(frozen=True)
class RollAnchorRow:
    """The shared reference spot a basket's rolls re-anchor to (migration
    ``0013``'s ``strategy_basket_roll_anchor``) — one row per basket,
    generic to any multi-leg strategy."""

    reference_price: float | None
    anchor_candle_ts: str | None
    updated_at: str


def load_rolls_for_basket(conn: sqlite3.Connection, *, basket_id: str) -> tuple[RollRow, ...]:
    """Every roll claim for one basket, ordered by role then roll sequence
    so repeated rolls for one role read in chronological order."""
    rows = conn.execute(
        """
        SELECT * FROM strategy_basket_rolls
        WHERE basket_id = ?
        ORDER BY leg_role, roll_sequence
        """,
        (basket_id,),
    ).fetchall()
    return tuple(_row_to_roll(conn, row) for row in rows)


def load_roll_anchor(conn: sqlite3.Connection, *, basket_id: str) -> RollAnchorRow | None:
    """The basket's current shared reference spot, if it has ever rolled or
    otherwise durably anchored one — ``None`` for a basket with no anchor
    row at all (e.g. it never reached primary entry)."""
    row = conn.execute(
        "SELECT * FROM strategy_basket_roll_anchor WHERE basket_id = ?",
        (basket_id,),
    ).fetchone()
    if row is None:
        return None
    return RollAnchorRow(
        reference_price=row["reference_price"],
        anchor_candle_ts=row["anchor_candle_ts"],
        updated_at=row["updated_at"],
    )


def _leg_strike_and_symbol(
    conn: sqlite3.Connection, leg_id: str | None
) -> tuple[float | None, str | None]:
    if leg_id is None:
        return None, None
    row = conn.execute(
        "SELECT strike, symbol FROM strategy_legs WHERE leg_id = ?", (leg_id,)
    ).fetchone()
    if row is None:
        return None, None
    return row["strike"], row["symbol"]


def _row_to_roll(conn: sqlite3.Connection, row: sqlite3.Row) -> RollRow:
    target_strike, target_symbol = _leg_strike_and_symbol(conn, row["target_leg_id"])
    replacement_strike, replacement_symbol = _leg_strike_and_symbol(
        conn, row["replacement_leg_id"]
    )
    return RollRow(
        leg_role=row["leg_role"],
        roll_sequence=int(row["roll_sequence"]),
        lifecycle_state=row["lifecycle_state"],
        target_leg_id=row["target_leg_id"],
        target_strike=target_strike,
        target_symbol=target_symbol,
        replacement_leg_id=row["replacement_leg_id"],
        replacement_strike=replacement_strike,
        replacement_symbol=replacement_symbol,
        reference_price_at_claim=row["reference_price_at_claim"],
        claim_candle_ts=row["claim_candle_ts"],
        claimed_at=row["claimed_at"],
        close_correlation_id=row["close_correlation_id"],
        close_intent_id=row["close_intent_id"],
        updated_at=row["updated_at"],
    )
