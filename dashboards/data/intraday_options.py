"""Read-model: everything the Intraday Options page's eight tabs show.

Every function here takes an already-open ``connect_readonly`` connection —
same convention as :mod:`common.health.snapshot` — so the page module owns
connection lifetime (via :func:`dashboards._shared.run_bounded`) and every
query here is directly unit-testable against a migrated fixture database.

**What is deliberately not here, and why.** No column here is derived from
strike/expiry/option-type: no table in this schema persists a structured
option contract (see ``dashboards/intraday_options.py``'s own
``NOT_YET_AVAILABLE`` note — unchanged) and the one strategy that runs today,
``ema_cross_9_21_buy``, trades the NIFTY index directly
(``parameters.security_id: "13"``), not an option contract — so those fields
are not merely unpersisted, they are not applicable to what actually runs.
No current price / unrealised MTM is shown for paper positions either: no
mark table exists for paper fills (only the live-only, shared-account
``live_position_mtm``), and inventing one from the entry fill price would be
exactly the "stale value shown as current" the spec forbids. Both gaps are
documented in the plan's data-availability matrix and the final report, not
silently patched over.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from .calendar_stats import DailyOutcome

#: Below this many closed trades, every ratio-style statistic (win rate,
#: profit factor, expectancy, drawdown) is still computed but flagged
#: unreliable — never hidden, never presented as though five trades and five
#: hundred trades carry the same weight. Mirrors the reference dashboard's
#: own ``MIN_TRADES_FOR_SCORE`` threshold (see the exploration report).
MIN_SAMPLE_SIZE = 5


# ============================================================== Overview
@dataclass(frozen=True)
class LatestError:
    message: str
    severity: str
    occurred_at: str


@dataclass(frozen=True)
class OverviewRow:
    """One strategy's Overview-tab row."""

    strategy_id: str
    execution_mode: str | None
    health_state: str
    heartbeat_age_seconds: float | None
    pid: int | None
    entries_blocked: bool | None
    square_off_state: str | None
    open_positions: int
    current_position_instrument: str | None
    current_position_quantity: int | None
    current_position_side: str | None
    today_trade_count: int
    today_net_pnl: float
    latest_error: LatestError | None


def load_overview(
    conn: sqlite3.Connection, runtime_id: str, trading_date: str
) -> tuple[OverviewRow, ...]:
    strategy_ids = [
        row["strategy_id"]
        for row in conn.execute(
            "SELECT DISTINCT strategy_id FROM runtime_heartbeats "
            "WHERE runtime_id = ? AND strategy_id IS NOT NULL ORDER BY strategy_id",
            (runtime_id,),
        )
    ]
    rows: list[OverviewRow] = []
    for strategy_id in strategy_ids:
        heartbeat = conn.execute(
            """
            SELECT health_state,
                   (julianday('now') - julianday(beat_at)) * 86400.0 AS age_seconds
            FROM runtime_heartbeats WHERE runtime_id = ? AND strategy_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (runtime_id, strategy_id),
        ).fetchone()
        session = conn.execute(
            "SELECT execution_mode, pid FROM runtime_sessions "
            "WHERE runtime_id = ? AND strategy_id = ? ORDER BY id DESC LIMIT 1",
            (runtime_id, strategy_id),
        ).fetchone()
        execution_mode = session["execution_mode"] if session else None
        state = (
            conn.execute(
                "SELECT square_off_state, entries_blocked FROM strategy_state "
                "WHERE runtime_id = ? AND strategy_id = ? AND execution_mode = ? "
                "AND trading_date = ?",
                (runtime_id, strategy_id, execution_mode, trading_date),
            ).fetchone()
            if execution_mode is not None
            else None
        )
        position = conn.execute(
            "SELECT instrument, quantity FROM positions WHERE runtime_id = ? "
            "AND strategy_id = ? AND trading_date = ? AND status = 'OPEN' AND quantity != 0 "
            "ORDER BY id DESC LIMIT 1",
            (runtime_id, strategy_id, trading_date),
        ).fetchone()
        open_count = conn.execute(
            "SELECT COUNT(*) AS c FROM positions WHERE runtime_id = ? AND strategy_id = ? "
            "AND trading_date = ? AND status = 'OPEN' AND quantity != 0",
            (runtime_id, strategy_id, trading_date),
        ).fetchone()
        today = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(realised_pnl - charges), 0.0) AS net "
            "FROM positions WHERE runtime_id = ? AND strategy_id = ? AND trading_date = ? "
            "AND status = 'CLOSED'",
            (runtime_id, strategy_id, trading_date),
        ).fetchone()
        error = conn.execute(
            "SELECT message, severity, occurred_at FROM errors WHERE runtime_id = ? "
            "AND strategy_id = ? ORDER BY id DESC LIMIT 1",
            (runtime_id, strategy_id),
        ).fetchone()
        rows.append(
            OverviewRow(
                strategy_id=strategy_id,
                execution_mode=execution_mode,
                health_state=heartbeat["health_state"] if heartbeat else "STOPPED",
                heartbeat_age_seconds=(
                    max(0.0, float(heartbeat["age_seconds"])) if heartbeat else None
                ),
                pid=int(session["pid"]) if session else None,
                entries_blocked=(bool(state["entries_blocked"]) if state else None),
                square_off_state=state["square_off_state"] if state else None,
                open_positions=int(open_count["c"]) if open_count else 0,
                current_position_instrument=position["instrument"] if position else None,
                current_position_quantity=(
                    int(position["quantity"]) if position is not None else None
                ),
                current_position_side=(
                    ("BUY" if position["quantity"] > 0 else "SELL") if position else None
                ),
                today_trade_count=int(today["n"]) if today else 0,
                today_net_pnl=float(today["net"]) if today else 0.0,
                latest_error=(
                    LatestError(
                        message=error["message"],
                        severity=error["severity"],
                        occurred_at=error["occurred_at"],
                    )
                    if error
                    else None
                ),
            )
        )
    return tuple(rows)


# =========================================================== Live positions
@dataclass(frozen=True)
class LivePositionRow:
    strategy_id: str
    execution_mode: str
    instrument: str
    security_id: str
    side: str
    quantity: int
    entry_time: str
    entry_price: float
    stop_price: float | None
    target_price: float | None
    highest_favourable: float | None
    lowest_favourable: float | None
    duration_seconds: float


def load_live_positions(
    conn: sqlite3.Connection, runtime_id: str, trading_date: str
) -> tuple[LivePositionRow, ...]:
    rows = conn.execute(
        """
        SELECT strategy_id, execution_mode, instrument, security_id, quantity,
               average_price, stop_price, target_price, highest_favourable,
               lowest_favourable, opened_at,
               (julianday('now') - julianday(opened_at)) * 86400.0 AS duration_seconds
        FROM positions
        WHERE runtime_id = ? AND trading_date = ? AND status = 'OPEN' AND quantity != 0
        ORDER BY opened_at DESC
        """,
        (runtime_id, trading_date),
    ).fetchall()
    return tuple(
        LivePositionRow(
            strategy_id=row["strategy_id"],
            execution_mode=row["execution_mode"],
            instrument=row["instrument"],
            security_id=row["security_id"],
            side="BUY" if row["quantity"] > 0 else "SELL",
            quantity=abs(int(row["quantity"])),
            entry_time=row["opened_at"],
            entry_price=float(row["average_price"]),
            stop_price=row["stop_price"],
            target_price=row["target_price"],
            highest_favourable=row["highest_favourable"],
            lowest_favourable=row["lowest_favourable"],
            duration_seconds=max(0.0, float(row["duration_seconds"])),
        )
        for row in rows
    )


# ============================================================ Orders & fills
@dataclass(frozen=True)
class FillRow:
    broker_fill_id: str
    quantity: int
    price: float
    reference_price: float | None
    slippage_amount: float | None
    latency_ms: int | None
    fill_method: str | None
    charges: float
    filled_at: str


@dataclass(frozen=True)
class OrderRow:
    correlation_id: str
    strategy_id: str
    execution_mode: str
    intent_time: str
    instrument: str
    security_id: str
    side: str
    quantity: int
    order_type: str
    status: str | None
    broker_order_id: str | None
    filled_quantity: int
    average_fill_price: float | None
    rejection_reason: str | None
    fills: tuple[FillRow, ...]

    @property
    def total_charges(self) -> float:
        return sum(f.charges for f in self.fills)


def load_orders(
    conn: sqlite3.Connection, runtime_id: str, trading_date: str
) -> tuple[OrderRow, ...]:
    intent_rows = conn.execute(
        """
        SELECT oi.id AS intent_id, oi.correlation_id, oi.strategy_id, oi.execution_mode,
               oi.created_at, oi.instrument, oi.security_id, oi.side, oi.quantity,
               oi.order_type, o.status, o.broker_order_id, o.filled_quantity,
               o.average_fill_price, o.rejection_reason
        FROM order_intents oi
        LEFT JOIN orders o ON o.intent_id = oi.id
        WHERE oi.runtime_id = ? AND oi.trading_date = ?
        ORDER BY oi.created_at DESC
        """,
        (runtime_id, trading_date),
    ).fetchall()
    if not intent_rows:
        return ()

    correlation_ids = tuple(row["correlation_id"] for row in intent_rows)
    placeholders = ",".join("?" * len(correlation_ids))
    fill_rows = conn.execute(
        f"""
        SELECT correlation_id, broker_fill_id, quantity, price, reference_price,
               slippage_amount, latency_ms, fill_method, charges, filled_at
        FROM fills WHERE correlation_id IN ({placeholders})
        ORDER BY filled_at ASC
        """,
        correlation_ids,
    ).fetchall()
    fills_by_correlation: dict[str, list[FillRow]] = defaultdict(list)
    for row in fill_rows:
        fills_by_correlation[row["correlation_id"]].append(
            FillRow(
                broker_fill_id=row["broker_fill_id"],
                quantity=int(row["quantity"]),
                price=float(row["price"]),
                reference_price=row["reference_price"],
                slippage_amount=row["slippage_amount"],
                latency_ms=row["latency_ms"],
                fill_method=row["fill_method"],
                charges=float(row["charges"]),
                filled_at=row["filled_at"],
            )
        )

    return tuple(
        OrderRow(
            correlation_id=row["correlation_id"],
            strategy_id=row["strategy_id"],
            execution_mode=row["execution_mode"],
            intent_time=row["created_at"],
            instrument=row["instrument"],
            security_id=row["security_id"],
            side=row["side"],
            quantity=int(row["quantity"]),
            order_type=row["order_type"],
            status=row["status"],
            broker_order_id=row["broker_order_id"],
            filled_quantity=int(row["filled_quantity"] or 0),
            average_fill_price=row["average_fill_price"],
            rejection_reason=row["rejection_reason"],
            fills=tuple(fills_by_correlation.get(row["correlation_id"], ())),
        )
        for row in intent_rows
    )


# ============================================================= Closed trades
@dataclass(frozen=True)
class ClosedTradeRow:
    strategy_id: str
    execution_mode: str
    instrument: str
    security_id: str
    trading_date: str
    side: str | None
    quantity: int
    entry_price: float
    exit_price: float | None
    points: float | None
    gross_pnl: float
    charges: float
    net_pnl: float
    entry_time: str
    exit_time: str | None


def load_closed_trades(
    conn: sqlite3.Connection,
    runtime_id: str,
    *,
    strategy_id: str | None = None,
    execution_mode: str | None = None,
    start_date: str,
    end_date: str,
) -> tuple[ClosedTradeRow, ...]:
    """Every closed position in ``[start_date, end_date]``, with entry/exit
    price and side derived from the fills that opened and closed each one.

    Two queries total regardless of trade count (positions, then every fill
    in the date range in one shot, grouped in Python) — not one fills query
    per position, per the spec's "aggregate through indexed queries" rule.
    Exit side is identified structurally: the fill matching
    ``positions.entry_correlation_id`` fixes the entry side; every other
    fill against that same (strategy, mode, security, day) is the exit
    side — never a string-parsed guess.
    """
    query = (
        "SELECT strategy_id, execution_mode, instrument, security_id, quantity, "
        "average_price, entry_correlation_id, realised_pnl, charges, opened_at, "
        "closed_at, trading_date FROM positions "
        "WHERE runtime_id = ? AND status = 'CLOSED' AND trading_date BETWEEN ? AND ?"
    )
    params: list[object] = [runtime_id, start_date, end_date]
    if strategy_id is not None:
        query += " AND strategy_id = ?"
        params.append(strategy_id)
    if execution_mode is not None:
        query += " AND execution_mode = ?"
        params.append(execution_mode)
    query += " ORDER BY closed_at DESC"
    position_rows = conn.execute(query, params).fetchall()
    if not position_rows:
        return ()

    fill_rows = conn.execute(
        """
        SELECT f.strategy_id, f.execution_mode, oi.security_id, oi.trading_date,
               f.correlation_id, f.price, f.quantity, oi.side
        FROM fills f
        JOIN order_intents oi ON oi.correlation_id = f.correlation_id
        WHERE f.runtime_id = ? AND oi.trading_date BETWEEN ? AND ?
        """,
        (runtime_id, start_date, end_date),
    ).fetchall()
    fills_by_position: dict[tuple[str, str, str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in fill_rows:
        key = (row["strategy_id"], row["execution_mode"], row["security_id"], row["trading_date"])
        fills_by_position[key].append(row)

    trades: list[ClosedTradeRow] = []
    for p in position_rows:
        key = (p["strategy_id"], p["execution_mode"], p["security_id"], p["trading_date"])
        position_fills = fills_by_position.get(key, [])
        entry_side: str | None = next(
            (f["side"] for f in position_fills if f["correlation_id"] == p["entry_correlation_id"]),
            None,
        )
        entry_quantity = 0
        exit_notional = 0.0
        exit_quantity = 0
        for f in position_fills:
            if f["side"] == entry_side:
                entry_quantity += int(f["quantity"])
            else:
                exit_notional += float(f["price"]) * int(f["quantity"])
                exit_quantity += int(f["quantity"])
        exit_price = (exit_notional / exit_quantity) if exit_quantity else None
        points = None
        if exit_price is not None:
            points = (
                exit_price - p["average_price"]
                if entry_side == "BUY"
                else p["average_price"] - exit_price
            )
        gross = float(p["realised_pnl"])
        charges = float(p["charges"])
        trades.append(
            ClosedTradeRow(
                strategy_id=p["strategy_id"],
                execution_mode=p["execution_mode"],
                instrument=p["instrument"],
                security_id=p["security_id"],
                trading_date=p["trading_date"],
                side=entry_side,
                quantity=entry_quantity or abs(int(p["quantity"])),
                entry_price=float(p["average_price"]),
                exit_price=exit_price,
                points=points,
                gross_pnl=gross,
                charges=charges,
                net_pnl=gross - charges,
                entry_time=p["opened_at"],
                exit_time=p["closed_at"],
            )
        )
    return tuple(trades)


def load_daily_outcomes(
    conn: sqlite3.Connection,
    runtime_id: str,
    *,
    strategy_id: str,
    execution_mode: str | None,
    start_date: str,
    end_date: str,
) -> tuple[DailyOutcome, ...]:
    """One row per trading date with either recorded runtime activity or a
    closed trade — the input to :mod:`dashboards.data.calendar_stats`.

    ``ran`` is derived from ``runtime_sessions.started_at`` (UTC). Every NSE
    session (09:15-15:30 IST) falls inside the same UTC calendar date, so
    the UTC date always matches the IST trading date in practice — no
    separate IST-tagged "session ran on this date" column exists to read
    instead.
    """
    ran_dates = {
        row["d"]
        for row in conn.execute(
            "SELECT DISTINCT date(started_at) AS d FROM runtime_sessions "
            "WHERE runtime_id = ? AND strategy_id = ? AND date(started_at) BETWEEN ? AND ?",
            (runtime_id, strategy_id, start_date, end_date),
        )
    }
    trades = load_closed_trades(
        conn,
        runtime_id,
        strategy_id=strategy_id,
        execution_mode=execution_mode,
        start_date=start_date,
        end_date=end_date,
    )
    by_date: dict[str, list[ClosedTradeRow]] = defaultdict(list)
    for t in trades:
        by_date[t.trading_date].append(t)

    all_dates = ran_dates | set(by_date.keys())
    outcomes = []
    for d in sorted(all_dates):
        day_trades = by_date.get(d, [])
        outcomes.append(
            DailyOutcome(
                trading_date=date.fromisoformat(d),
                ran=d in ran_dates,
                trade_count=len(day_trades),
                net_pnl=sum(t.net_pnl for t in day_trades),
            )
        )
    return tuple(outcomes)


def load_inception_date(conn: sqlite3.Connection, runtime_id: str) -> date | None:
    """The earliest recorded session for this runtime group — the start of
    the 30-day paper-forward-testing clock. ``None`` if it has never run."""
    row = conn.execute(
        "SELECT MIN(date(started_at)) AS d FROM runtime_sessions WHERE runtime_id = ?",
        (runtime_id,),
    ).fetchone()
    if row is None or row["d"] is None:
        return None
    return date.fromisoformat(row["d"])


# ================================================================ Signals
@dataclass(frozen=True)
class SignalRow:
    strategy_id: str
    execution_mode: str
    instrument: str
    side: str
    candle_open: float
    candle_high: float
    candle_low: float
    candle_close: float
    candle_start_at: str
    candle_end_at: str
    reference_price: float
    evaluated_at: str
    reason: str | None
    order_correlation_id: str | None


def load_signals(
    conn: sqlite3.Connection, runtime_id: str, trading_date: str, *, limit: int = 200
) -> tuple[SignalRow, ...]:
    rows = conn.execute(
        """
        SELECT s.strategy_id, s.execution_mode, s.instrument, s.side, s.candle_open,
               s.candle_high, s.candle_low, s.candle_close, s.candle_start_at,
               s.candle_end_at, s.reference_price, s.evaluated_at, s.reason,
               oi.correlation_id AS order_correlation_id
        FROM signals s
        LEFT JOIN order_intents oi ON oi.signal_id = s.id
        WHERE s.runtime_id = ? AND s.trading_date = ?
        ORDER BY s.evaluated_at DESC LIMIT ?
        """,
        (runtime_id, trading_date, limit),
    ).fetchall()
    return tuple(
        SignalRow(
            strategy_id=row["strategy_id"],
            execution_mode=row["execution_mode"],
            instrument=row["instrument"],
            side=row["side"],
            candle_open=row["candle_open"],
            candle_high=row["candle_high"],
            candle_low=row["candle_low"],
            candle_close=row["candle_close"],
            candle_start_at=row["candle_start_at"],
            candle_end_at=row["candle_end_at"],
            reference_price=row["reference_price"],
            evaluated_at=row["evaluated_at"],
            reason=row["reason"],
            order_correlation_id=row["order_correlation_id"],
        )
        for row in rows
    )


@dataclass(frozen=True)
class NotificationRow:
    strategy_id: str | None
    execution_mode: str | None
    channel: str
    event_type: str
    message: str
    delivered: bool
    failure_reason: str | None
    created_at: str


def load_notifications(
    conn: sqlite3.Connection, runtime_id: str, *, limit: int = 100
) -> tuple[NotificationRow, ...]:
    rows = conn.execute(
        "SELECT strategy_id, execution_mode, channel, event_type, message, delivered, "
        "failure_reason, created_at FROM notifications WHERE runtime_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (runtime_id, limit),
    ).fetchall()
    return tuple(
        NotificationRow(
            strategy_id=row["strategy_id"],
            execution_mode=row["execution_mode"],
            channel=row["channel"],
            event_type=row["event_type"],
            message=row["message"],
            delivered=bool(row["delivered"]),
            failure_reason=row["failure_reason"],
            created_at=row["created_at"],
        )
        for row in rows
    )


@dataclass(frozen=True)
class EventErrorRow:
    strategy_id: str | None
    execution_mode: str | None
    severity: str
    component: str
    message: str
    occurred_at: str


def load_errors(
    conn: sqlite3.Connection, runtime_id: str, *, limit: int = 100
) -> tuple[EventErrorRow, ...]:
    rows = conn.execute(
        "SELECT strategy_id, execution_mode, severity, component, message, occurred_at "
        "FROM errors WHERE runtime_id = ? ORDER BY id DESC LIMIT ?",
        (runtime_id, limit),
    ).fetchall()
    return tuple(
        EventErrorRow(
            strategy_id=row["strategy_id"],
            execution_mode=row["execution_mode"],
            severity=row["severity"],
            component=row["component"],
            message=row["message"],
            occurred_at=row["occurred_at"],
        )
        for row in rows
    )


# ============================================================= Performance
@dataclass(frozen=True)
class PerformanceMetrics:
    """Every ratio here is computed regardless of sample size — never
    hidden — but :attr:`reliable` must gate how a page presents it (spec:
    "do not present ... as reliable when there is insufficient data")."""

    sample_size: int
    wins: int
    losses: int
    win_rate: float | None
    gross_profit: float
    gross_loss: float
    net_profit: float
    avg_win: float | None
    avg_loss: float | None
    profit_factor: float | None
    expectancy: float | None
    total_charges: float
    max_drawdown: float | None

    @property
    def reliable(self) -> bool:
        return self.sample_size >= MIN_SAMPLE_SIZE


_EMPTY_METRICS = PerformanceMetrics(
    sample_size=0,
    wins=0,
    losses=0,
    win_rate=None,
    gross_profit=0.0,
    gross_loss=0.0,
    net_profit=0.0,
    avg_win=None,
    avg_loss=None,
    profit_factor=None,
    expectancy=None,
    total_charges=0.0,
    max_drawdown=None,
)


def equity_curve(trades: tuple[ClosedTradeRow, ...]) -> tuple[tuple[str, float], ...]:
    """Cumulative net P&L ordered by exit time. Trades with no ``exit_time``
    (should not exist for a closed position, kept defensive) are skipped."""
    ordered = sorted((t for t in trades if t.exit_time), key=lambda t: t.exit_time or "")
    points = []
    running = 0.0
    for t in ordered:
        running += t.net_pnl
        points.append((t.exit_time or "", running))
    return tuple(points)


def drawdown_curve(equity: tuple[tuple[str, float], ...]) -> tuple[tuple[str, float], ...]:
    """Equity minus running peak, at every equity point — always ≤ 0."""
    peak = float("-inf")
    points = []
    for ts, value in equity:
        peak = max(peak, value)
        points.append((ts, value - peak))
    return tuple(points)


def pnl_by_day(trades: tuple[ClosedTradeRow, ...]) -> tuple[tuple[str, float], ...]:
    totals: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.exit_time:
            totals[t.exit_time[:10]] += t.net_pnl
    return tuple(sorted(totals.items()))


def pnl_by_month(trades: tuple[ClosedTradeRow, ...]) -> tuple[tuple[str, float], ...]:
    totals: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.exit_time:
            totals[t.exit_time[:7]] += t.net_pnl
    return tuple(sorted(totals.items()))


def compute_metrics(trades: tuple[ClosedTradeRow, ...]) -> PerformanceMetrics:
    n = len(trades)
    if n == 0:
        return _EMPTY_METRICS
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    gross_profit = sum(t.net_pnl for t in wins)
    gross_loss = -sum(t.net_pnl for t in losses)
    net_profit = sum(t.net_pnl for t in trades)
    total_charges = sum(t.charges for t in trades)
    win_rate = len(wins) / n * 100.0
    avg_win = (gross_profit / len(wins)) if wins else None
    avg_loss = (gross_loss / len(losses)) if losses else None
    if gross_loss == 0:
        profit_factor = float("inf") if gross_profit > 0 else None
    else:
        profit_factor = gross_profit / gross_loss
    p_win = len(wins) / n
    expectancy = (p_win * (avg_win or 0.0)) - ((1 - p_win) * (avg_loss or 0.0))
    drawdown = drawdown_curve(equity_curve(trades))
    max_dd = min((d for _, d in drawdown), default=None)
    return PerformanceMetrics(
        sample_size=n,
        wins=len(wins),
        losses=len(losses),
        win_rate=win_rate,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        net_profit=net_profit,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        expectancy=expectancy,
        total_charges=total_charges,
        max_drawdown=max_dd,
    )


# ========================================================= Strategy comparison
@dataclass(frozen=True)
class ComparisonRow:
    strategy_id: str
    metrics: PerformanceMetrics
    execution_days: int
    eligible_days: int
    roi_pct: float | None


def build_strategy_comparison(
    conn: sqlite3.Connection,
    runtime_id: str,
    config_root: object,
    strategy_ids: tuple[str, ...],
    *,
    start_date: str,
    end_date: str,
) -> tuple[ComparisonRow, ...]:
    """One leaderboard row per strategy — computed read-only, on demand,
    from closed trades in ``[start_date, end_date]``. Never writes a
    snapshot (the reference dashboard's "save today's snapshot" button is
    deliberately not ported)."""
    rows = []
    for strategy_id in strategy_ids:
        trades = load_closed_trades(
            conn,
            runtime_id,
            strategy_id=strategy_id,
            start_date=start_date,
            end_date=end_date,
        )
        metrics = compute_metrics(trades)
        outcomes = load_daily_outcomes(
            conn,
            runtime_id,
            strategy_id=strategy_id,
            execution_mode=None,
            start_date=start_date,
            end_date=end_date,
        )
        from .calendar_stats import execution_day_stats

        day_stats = execution_day_stats(
            outcomes,
            window_start=date.fromisoformat(start_date),
            window_end=date.fromisoformat(end_date),
        )
        capital_base = load_capital_base(config_root, strategy_id)
        roi_pct = (
            (metrics.net_profit / capital_base * 100.0)
            if capital_base and capital_base > 0
            else None
        )
        rows.append(
            ComparisonRow(
                strategy_id=strategy_id,
                metrics=metrics,
                execution_days=day_stats.executed_days,
                eligible_days=day_stats.eligible_trading_days,
                roi_pct=roi_pct,
            )
        )
    return tuple(rows)


def load_capital_base(config_root: object, strategy_id: str) -> float | None:
    """``parameters.capital_base`` from ``config/strategies/<id>.yaml``, if
    present — a per-strategy, untyped config value (``StrategyConfig.
    parameters`` is a free-form dict), so ROI is only ever computed when a
    strategy has actually declared its own capital base. A config-only
    read, same exception class as ``dashboards/app.py``'s live-gate read.
    """
    from pathlib import Path

    import yaml

    path = Path(str(config_root)) / "strategies" / f"{strategy_id}.yaml"
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    parameters = data.get("parameters")
    if not isinstance(parameters, dict):
        return None
    capital = parameters.get("capital_base")
    return float(capital) if isinstance(capital, (int, float)) else None
