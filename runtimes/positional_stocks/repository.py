"""Persistence for the ``wsr1_weekly_stochrsi`` paper book (spec section 9).

The ``stock_`` tables of ``positional_stocks.db`` (own migration set, v1.2i),
read into — and written from — the rules core's own types. Nothing here decides
anything: :mod:`.accounting` runs the week, the rules decide, this module
stores what they said.

* **Cash is derived, never stored as a balance:** capital plus the sum of every
  fill's ``cash_delta``, so it cannot drift from the fills that made it.
* **A position is rebuilt from its fills** (tranches and sales) plus the row
  holding what fills cannot: sizing, fixed levels and the rules' bookkeeping
  (touch memory, T3 disabled, ``half_sold_week``). ``Position.__post_init__``
  re-validates every rebuilt position.
* **Order ids are deterministic** — strategy, decided week, symbol, action — so
  re-deciding the same week produces the same ids, and ``stock_fills``'
  ``UNIQUE(order_id)`` makes each fill exactly-once.
* Every write takes the caller's connection, inside the caller's single
  transaction; see :mod:`.accounting` for the boundaries.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal

from common.persistence import Database
from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import WeekKey
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    Book,
    BrakeState,
    BuyFill,
    ClosedTrade,
    FunnelEntry,
    OnExit,
    OrderAction,
    PendingOrder,
    Position,
    PositionReview,
    PositionState,
    SaleFill,
    Sizing,
    UniverseRow,
    WeekDecision,
)

#: Timestamps excluded from :meth:`StockRepository.dump`, which compares state.
_VOLATILE_COLUMNS = frozenset({"started_at", "finished_at"})


# ------------------------------------------------------------- encoding
def week_text(week: WeekKey) -> str:
    return f"{week[0]}-W{week[1]:02d}"


def week_key(text: str) -> WeekKey:
    year, week = text.split("-W")
    return (int(year), int(week))


def _opt_week(text: str | None) -> WeekKey | None:
    return None if text is None else week_key(text)


def _opt_date(text: str | None) -> date | None:
    return None if text is None else date.fromisoformat(text)


def order_id(strategy_id: str, order: PendingOrder) -> str:
    return f"{strategy_id}:{week_text(order.decided_week)}:{order.symbol}:{order.action.value}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class StockRepository:
    """Reads and writes one strategy's paper book."""

    def __init__(self, database: Database, strategy_id: str, capital: Decimal) -> None:
        self._db = database
        self._strategy_id = strategy_id
        self._capital = capital

    @property
    def database(self) -> Database:
        return self._db

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    # ------------------------------------------------------------- runs
    def run_status(self, week_ending: date) -> str | None:
        row = (
            self._db.connect()
            .execute(
                "SELECT status FROM stock_weekly_runs WHERE strategy_id = ? AND week_ending = ?",
                (self._strategy_id, week_ending.isoformat()),
            )
            .fetchone()
        )
        return None if row is None else str(row["status"])

    def latest_run(self) -> tuple[date, str] | None:
        """The most recent run's week-ending date and status."""
        row = (
            self._db.connect()
            .execute(
                "SELECT week_ending, status FROM stock_weekly_runs WHERE strategy_id = ? "
                "ORDER BY week_ending DESC LIMIT 1",
                (self._strategy_id,),
            )
            .fetchone()
        )
        return None if row is None else (date.fromisoformat(row["week_ending"]), row["status"])

    def mark_started(self, week_ending: date, week: WeekKey, fingerprint: str) -> None:
        """Record STARTED in its own transaction, before the week's work."""
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO stock_weekly_runs "
                "(strategy_id, week_ending, iso_week, status, fingerprint, started_at) "
                "VALUES (?, ?, ?, 'STARTED', ?, ?) "
                "ON CONFLICT (strategy_id, week_ending) DO UPDATE SET "
                "fingerprint = excluded.fingerprint, started_at = excluded.started_at",
                (self._strategy_id, week_ending.isoformat(), week_text(week), fingerprint, _now()),
            )

    def mark_completed(self, conn: sqlite3.Connection, week_ending: date) -> None:
        conn.execute(
            "UPDATE stock_weekly_runs SET status = 'COMPLETED', finished_at = ? "
            "WHERE strategy_id = ? AND week_ending = ?",
            (_now(), self._strategy_id, week_ending.isoformat()),
        )

    # ------------------------------------------------------------- reads
    def cash(self, conn: sqlite3.Connection | None = None) -> Decimal:
        """Capital plus every fill's cash change."""
        rows = (conn or self._db.connect()).execute(
            "SELECT cash_delta FROM stock_fills WHERE strategy_id = ?", (self._strategy_id,)
        )
        return self._capital + sum((Decimal(row["cash_delta"]) for row in rows), Decimal("0"))

    def positions(self, conn: sqlite3.Connection | None = None) -> dict[str, Position]:
        """Every position ever opened, by id (CLOSED ones included)."""
        connection = conn or self._db.connect()
        fills: dict[str, list[sqlite3.Row]] = {}
        for row in connection.execute(
            "SELECT * FROM stock_fills WHERE strategy_id = ? ORDER BY fill_id",
            (self._strategy_id,),
        ):
            fills.setdefault(row["position_id"], []).append(row)
        out: dict[str, Position] = {}
        for row in connection.execute(
            "SELECT * FROM stock_positions WHERE strategy_id = ?", (self._strategy_id,)
        ):
            out[row["position_id"]] = _position(row, fills.get(row["position_id"], []))
        return out

    def pending_orders(self, conn: sqlite3.Connection | None = None) -> list[PendingOrder]:
        rows = (conn or self._db.connect()).execute(
            "SELECT * FROM stock_pending_orders WHERE strategy_id = ? AND state = 'PENDING' "
            "ORDER BY order_id",
            (self._strategy_id,),
        )
        return [_pending(row) for row in rows]

    def brakes(self, conn: sqlite3.Connection | None = None) -> BrakeState:
        row = (
            (conn or self._db.connect())
            .execute(
                "SELECT * FROM stock_equity WHERE strategy_id = ? "
                "ORDER BY week_ending DESC LIMIT 1",
                (self._strategy_id,),
            )
            .fetchone()
        )
        if row is None:
            return BrakeState()
        return BrakeState(
            peak=Decimal(row["peak"]),
            brake1_until=_opt_week(row["brake1_until"]),
            brake1_can_fire=bool(row["brake1_can_fire"]),
            brake2_fired_on=_opt_date(row["brake2_fired_on"]),
        )

    def book(self, conn: sqlite3.Connection | None = None) -> Book:
        """The book the rules decide on: held positions, closed trades,
        still-PENDING orders, cash and brakes."""
        positions = self.positions(conn)
        held = tuple(p for p in positions.values() if p.state is not PositionState.CLOSED)
        closed = tuple(
            ClosedTrade(p.symbol, p.position_id, p.exit_week, p.net_pnl)  # type: ignore[arg-type]
            for p in positions.values()
            if p.state is PositionState.CLOSED
        )
        return Book(
            cash=self.cash(conn),
            positions=tuple(sorted(held, key=lambda p: p.symbol)),
            closed=closed,
            pending=tuple(self.pending_orders(conn)),
            brakes=self.brakes(conn),
        )

    def last_seen_rows(self, conn: sqlite3.Connection | None = None) -> dict[str, UniverseRow]:
        rows = (conn or self._db.connect()).execute(
            "SELECT * FROM stock_universe_seen WHERE strategy_id = ?", (self._strategy_id,)
        )
        return {
            row["symbol"]: UniverseRow(
                symbol=row["symbol"],
                isin=row["isin"],
                company=row["company"],
                industry=row["industry"],
                nifty100=bool(row["nifty100"]),
                group=row["grp"],
                on_exit=OnExit(row["on_exit"]),
                as_of=_opt_date(row["as_of"]),
            )
            for row in rows
        }

    # ------------------------------------------------------------ writes
    def save_position(self, conn: sqlite3.Connection, position: Position, week: WeekKey) -> None:
        sizing = position.sizing
        conn.execute(
            "INSERT INTO stock_positions (position_id, strategy_id, symbol, sector, grp, state, "
            "spacing, allocation, t1_amount, t2_amount, t3_amount, event_risk, p1, l1, l2, stop, "
            "touch_week, t3_disabled, half_sold_week, exit_week, net_pnl, updated_week) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (position_id) DO UPDATE SET state = excluded.state, "
            "touch_week = excluded.touch_week, t3_disabled = excluded.t3_disabled, "
            "half_sold_week = excluded.half_sold_week, exit_week = excluded.exit_week, "
            "net_pnl = excluded.net_pnl, updated_week = excluded.updated_week",
            (
                position.position_id,
                self._strategy_id,
                position.symbol,
                position.sector,
                position.group,
                position.state.value,
                str(sizing.spacing),
                str(sizing.allocation),
                *(str(a) for a in sizing.tranche_amounts),
                int(sizing.event_risk),
                str(position.p1),
                str(position.l1),
                str(position.l2),
                str(position.stop),
                None if position.touch_week is None else week_text(position.touch_week),
                int(position.t3_disabled),
                None if position.half_sold_week is None else week_text(position.half_sold_week),
                None if position.exit_week is None else week_text(position.exit_week),
                str(position.net_pnl) if position.state is PositionState.CLOSED else None,
                week_text(week),
            ),
        )

    def save_order(self, conn: sqlite3.Connection, order: PendingOrder) -> str:
        sizing = order.sizing
        identifier = order_id(self._strategy_id, order)
        conn.execute(
            "INSERT INTO stock_pending_orders (order_id, strategy_id, symbol, action, "
            "decided_week, execute_on_or_after, reason, position_id, amount, quantity, spacing, "
            "allocation, t1_amount, t2_amount, t3_amount, event_risk, sector, grp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identifier,
                self._strategy_id,
                order.symbol,
                order.action.value,
                week_text(order.decided_week),
                order.execute_on_or_after.isoformat(),
                order.reason,
                order.position_id,
                None if order.amount is None else str(order.amount),
                order.quantity,
                None if sizing is None else str(sizing.spacing),
                None if sizing is None else str(sizing.allocation),
                *(
                    (None, None, None)
                    if sizing is None
                    else (str(a) for a in sizing.tranche_amounts)
                ),
                None if sizing is None else int(sizing.event_risk),
                order.sector,
                order.group,
            ),
        )
        return identifier

    def resolve_order(
        self,
        conn: sqlite3.Connection,
        order: PendingOrder,
        *,
        state: str,
        week: WeekKey,
        resolution: str,
    ) -> None:
        """PENDING -> FILLED or SKIPPED, exactly once."""
        cursor = conn.execute(
            "UPDATE stock_pending_orders SET state = ?, resolved_week = ?, resolution = ? "
            "WHERE order_id = ? AND state = 'PENDING'",
            (state, week_text(week), resolution, order_id(self._strategy_id, order)),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"order {order_id(self._strategy_id, order)} was not PENDING")

    def save_fill(
        self,
        conn: sqlite3.Connection,
        order: PendingOrder,
        position: Position,
        *,
        cash_delta: Decimal,
        not_traded_on_execution_session: bool,
        week: WeekKey,
    ) -> None:
        """The fill this order just made — the position's latest buy or sale."""
        if order.action.is_buy:
            buy = position.buys[order.action.tranche - 1]
            session, price, shares, fees = buy.session, buy.price, buy.shares, buy.fees
            at_open: int | None = int(buy.at_week_open)
        else:
            sale = position.sales[-1]
            session, price, shares, fees = sale.session, sale.price, sale.shares, sale.fees
            at_open = None
        conn.execute(
            "INSERT INTO stock_fills (strategy_id, order_id, position_id, symbol, action, "
            "session, price, shares, fees, at_week_open, cash_delta, "
            "not_traded_on_execution_session, recorded_week) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._strategy_id,
                order_id(self._strategy_id, order),
                position.position_id,
                order.symbol,
                order.action.value,
                session.isoformat(),
                str(price),
                shares,
                str(fees),
                at_open,
                str(cash_delta),
                int(not_traded_on_execution_session),
                week_text(week),
            ),
        )

    def save_cooling_off(
        self, conn: sqlite3.Connection, trade: ClosedTrade, until_week: WeekKey
    ) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO stock_cooling_off "
            "(strategy_id, position_id, symbol, exit_week, until_week, net_pnl) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                self._strategy_id,
                trade.position_id,
                trade.symbol,
                week_text(trade.exit_week),
                week_text(until_week),
                str(trade.net_pnl),
            ),
        )

    def save_universe_seen(
        self, conn: sqlite3.Connection, rows: Iterable[UniverseRow], week: WeekKey
    ) -> None:
        for row in rows:
            conn.execute(
                "INSERT INTO stock_universe_seen (strategy_id, symbol, isin, company, industry, "
                "nifty100, grp, on_exit, as_of, last_seen_week) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (strategy_id, symbol) DO UPDATE SET isin = excluded.isin, "
                "company = excluded.company, industry = excluded.industry, "
                "nifty100 = excluded.nifty100, grp = excluded.grp, on_exit = excluded.on_exit, "
                "as_of = excluded.as_of, last_seen_week = excluded.last_seen_week",
                (
                    self._strategy_id,
                    row.symbol,
                    row.isin,
                    row.company,
                    row.industry,
                    int(row.nifty100),
                    row.group,
                    row.on_exit.value,
                    None if row.as_of is None else row.as_of.isoformat(),
                    week_text(week),
                ),
            )

    def save_decision(
        self,
        conn: sqlite3.Connection,
        decision: WeekDecision,
        week_ending: date,
        *,
        cash: Decimal,
        triggered: dict[str, bool],
    ) -> None:
        """The week's equity/brake row, its funnel with trigger history, and
        each held position's review."""
        brakes = decision.brakes
        assert brakes.peak is not None
        conn.execute(
            "INSERT INTO stock_equity (strategy_id, week_ending, iso_week, cash, "
            "positions_value, equity, peak, regime, brake1_until, brake1_can_fire, "
            "brake2_fired_on, entries_blocked, warnings) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._strategy_id,
                week_ending.isoformat(),
                week_text(decision.week),
                str(cash),
                str(decision.equity - cash),
                str(decision.equity),
                str(brakes.peak),
                decision.regime.value,
                None if brakes.brake1_until is None else week_text(brakes.brake1_until),
                int(brakes.brake1_can_fire),
                None if brakes.brake2_fired_on is None else brakes.brake2_fired_on.isoformat(),
                decision.entries_blocked,
                json.dumps(list(decision.warnings)),
            ),
        )
        funnel: dict[str, FunnelEntry] = {entry.symbol: entry for entry in decision.funnel}
        for symbol in sorted(set(funnel) | set(triggered)):
            entry = funnel.get(symbol)
            conn.execute(
                "INSERT INTO stock_signals (strategy_id, week_ending, symbol, stage, reason, "
                "rs, flags, triggered) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._strategy_id,
                    week_ending.isoformat(),
                    symbol,
                    "held" if entry is None else entry.stage.value,
                    "position reviewed" if entry is None else entry.reason,
                    None if entry is None else entry.rs,
                    json.dumps([] if entry is None else list(entry.flags)),
                    int(triggered.get(symbol, False)),
                ),
            )
        for review in decision.reviews:
            self._save_review(conn, review, week_ending)

    def _save_review(
        self, conn: sqlite3.Connection, review: PositionReview, week_ending: date
    ) -> None:
        conn.execute(
            "INSERT INTO stock_position_reviews (strategy_id, week_ending, position_id, symbol, "
            "reason, order_id, flags) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self._strategy_id,
                week_ending.isoformat(),
                review.position_id,
                review.symbol,
                review.reason,
                None if review.order is None else order_id(self._strategy_id, review.order),
                json.dumps(list(review.flags)),
            ),
        )

    # ----------------------------------------------------------- testing
    def dump(self) -> dict[str, list[tuple[object, ...]]]:
        """Every ``stock_`` row, sorted, without timestamps — for comparing
        two states of the book (idempotency and crash-resume tests)."""
        conn = self._db.connect()
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'stock_%' "
                "ORDER BY name"
            )
        ]
        out: dict[str, list[tuple[object, ...]]] = {}
        for table in tables:
            columns = [
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table})")
                if row["name"] not in _VOLATILE_COLUMNS and row["name"] != "fill_id"
            ]
            rows = conn.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
            out[table] = sorted(tuple(row) for row in rows)
        return out


# ------------------------------------------------------------ decoding
def _sizing(row: sqlite3.Row) -> Sizing:
    return Sizing(
        spacing=Decimal(row["spacing"]),
        allocation=Decimal(row["allocation"]),
        tranche_amounts=(
            Decimal(row["t1_amount"]),
            Decimal(row["t2_amount"]),
            Decimal(row["t3_amount"]),
        ),
        event_risk=bool(row["event_risk"]),
    )


def _position(row: sqlite3.Row, fills: list[sqlite3.Row]) -> Position:
    buys = sorted(
        (
            BuyFill(
                tranche=OrderAction(fill["action"]).tranche,
                session=date.fromisoformat(fill["session"]),
                price=Decimal(fill["price"]),
                shares=int(fill["shares"]),
                at_week_open=bool(fill["at_week_open"]),
                fees=Decimal(fill["fees"]),
            )
            for fill in fills
            if OrderAction(fill["action"]).is_buy
        ),
        key=lambda buy: buy.tranche,
    )
    sales = tuple(
        SaleFill(
            action=OrderAction(fill["action"]),
            session=date.fromisoformat(fill["session"]),
            price=Decimal(fill["price"]),
            shares=int(fill["shares"]),
            fees=Decimal(fill["fees"]),
        )
        for fill in fills
        if not OrderAction(fill["action"]).is_buy
    )
    return Position(
        position_id=row["position_id"],
        symbol=row["symbol"],
        sector=row["sector"],
        group=row["grp"],
        sizing=_sizing(row),
        p1=Decimal(row["p1"]),
        l1=Decimal(row["l1"]),
        l2=Decimal(row["l2"]),
        stop=Decimal(row["stop"]),
        buys=tuple(buys),
        sales=sales,
        state=PositionState(row["state"]),
        touch_week=_opt_week(row["touch_week"]),
        t3_disabled=bool(row["t3_disabled"]),
        half_sold_week=_opt_week(row["half_sold_week"]),
    )


def _pending(row: sqlite3.Row) -> PendingOrder:
    has_sizing = row["spacing"] is not None
    return PendingOrder(
        action=OrderAction(row["action"]),
        symbol=row["symbol"],
        decided_week=week_key(row["decided_week"]),
        execute_on_or_after=date.fromisoformat(row["execute_on_or_after"]),
        reason=row["reason"],
        position_id=row["position_id"],
        amount=None if row["amount"] is None else Decimal(row["amount"]),
        quantity=row["quantity"],
        sizing=_sizing(row) if has_sizing else None,
        sector=row["sector"],
        group=row["grp"],
    )
