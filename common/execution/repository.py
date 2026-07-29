"""Transactional persistence for the order lifecycle.

The spec's transaction boundaries (section 11.6) are the reason this is a
repository rather than scattered SQL:

* **Intent + correlation reservation + risk decision commit together, before the
  broker is called.** If the process dies during submission, the intent is on
  disk and recovery can ask the broker what happened using the correlation ID.
  Persisting after the call would leave an order in the market that the database
  has never heard of — the one failure mode that cannot be repaired.
* **Fill + order update + position update + strategy state commit together,
  after.** A fill that moved the position but did not update the order, or vice
  versa, is silent corruption that only surfaces as a wrong P&L.
* **No network call inside a transaction**, because an open write transaction
  holds a lock for the whole broker round trip.

Fill application is idempotent on ``(order_id, broker_fill_id)``: replaying the
same broker event must not move the position twice.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from common.config.models import ExecutionMode
from common.models import (
    Candle,
    Fill,
    Order,
    OrderIntent,
    OrderStatus,
    Position,
    PositionStatus,
    Side,
    Signal,
)
from common.persistence import Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class SessionRecord:
    """A runtime session row — the anchor for everything a process writes."""

    id: int
    runtime_id: str
    strategy_id: str | None
    execution_mode: ExecutionMode
    process_role: str
    pid: int
    started_at: str


class ExecutionRepository:
    """All operational writes for one runtime group's database."""

    def __init__(self, database: Database) -> None:
        self._db = database

    @property
    def database(self) -> Database:
        return self._db

    # ------------------------------------------------------------ sessions
    def open_session(
        self,
        *,
        runtime_id: str,
        strategy_id: str | None,
        execution_mode: ExecutionMode,
        process_role: str,
        pid: int,
        config_fingerprint: str | None = None,
    ) -> SessionRecord:
        started_at = _now()
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO runtime_sessions
                    (runtime_id, strategy_id, execution_mode, process_role, pid,
                     config_fingerprint, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    runtime_id,
                    strategy_id,
                    execution_mode.value,
                    process_role,
                    pid,
                    config_fingerprint,
                    started_at,
                ),
            )
            session_id = int(cursor.lastrowid or 0)
        return SessionRecord(
            id=session_id,
            runtime_id=runtime_id,
            strategy_id=strategy_id,
            execution_mode=execution_mode,
            process_role=process_role,
            pid=pid,
            started_at=started_at,
        )

    def close_session(self, session_id: int, *, reason: str = "clean_shutdown") -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE runtime_sessions SET ended_at = ?, shutdown_reason = ? WHERE id = ?",
                (_now(), reason, session_id),
            )

    def previous_incomplete_session(
        self, *, runtime_id: str, strategy_id: str, exclude_session_id: int | None = None
    ) -> sqlite3.Row | None:
        """The session that never shut down cleanly — what recovery reconciles."""
        sql = """
            SELECT * FROM runtime_sessions
            WHERE runtime_id = ? AND strategy_id = ? AND ended_at IS NULL
        """
        params: list[object] = [runtime_id, strategy_id]
        if exclude_session_id is not None:
            sql += " AND id != ?"
            params.append(exclude_session_id)
        sql += " ORDER BY id DESC LIMIT 1"
        row: sqlite3.Row | None = self._db.connect().execute(sql, params).fetchone()
        return row

    def record_heartbeat(
        self,
        *,
        session_id: int,
        runtime_id: str,
        strategy_id: str | None,
        health_state: str,
        last_tick_at: datetime | None = None,
        queue_depth: int | None = None,
        dropped_events: int = 0,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO runtime_heartbeats
                    (session_id, runtime_id, strategy_id, health_state, last_tick_at,
                     queue_depth, dropped_events, beat_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    runtime_id,
                    strategy_id,
                    health_state,
                    last_tick_at.isoformat() if last_tick_at else None,
                    queue_depth,
                    dropped_events,
                    _now(),
                ),
            )

    # ------------------------------------------------------------- signals
    def record_signal(
        self,
        *,
        session_id: int,
        runtime_id: str,
        signal: Signal,
        trading_date: str,
        config_fingerprint: str | None = None,
    ) -> int | None:
        """Persist a signal. Returns None when this candle already produced one.

        The UNIQUE constraint on (strategy, mode, instrument, candle_end_at) is
        what enforces "one completed candle creates one order". A duplicate
        candle delivery is absorbed here rather than becoming a second position.
        """
        candle: Candle = signal.candle
        try:
            with self._db.transaction() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO signals
                        (session_id, runtime_id, strategy_id, execution_mode, trading_date,
                         instrument, security_id, side, candle_open, candle_high, candle_low,
                         candle_close, candle_start_at, candle_end_at, reference_price,
                         tick_at, received_at, evaluated_at, reason, config_fingerprint)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        runtime_id,
                        signal.strategy_id,
                        signal.execution_mode.value,
                        trading_date,
                        signal.instrument,
                        signal.security_id,
                        signal.side.value,
                        candle.open,
                        candle.high,
                        candle.low,
                        candle.close,
                        candle.start_at.isoformat(),
                        candle.end_at.isoformat(),
                        signal.reference_price,
                        candle.last_tick_at.isoformat() if candle.last_tick_at else None,
                        None,
                        signal.evaluated_at.isoformat(),
                        signal.reason,
                        config_fingerprint,
                    ),
                )
                return int(cursor.lastrowid or 0)
        except sqlite3.IntegrityError:
            return None

    # ------------------------------------------------------- order intents
    def reserve_intent(
        self,
        *,
        session_id: int,
        intent: OrderIntent,
    ) -> int:
        """Persist the intent and reserve submission, in one transaction.

        Everything the spec requires before an external call happens here and
        commits together: the intent row, the unique correlation ID, the risk
        decision and the submission reservation.
        """
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO order_intents
                    (correlation_id, correlation_namespace, session_id, signal_id, runtime_id,
                     strategy_id, execution_mode, trading_date, sequence_number, instrument,
                     security_id, side, quantity, order_type, limit_price, trigger_price,
                     product_type, basket_id, leg_id, config_fingerprint, risk_decision,
                     risk_reason, submission_reserved, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    intent.correlation_id,
                    intent.execution_mode.value,
                    session_id,
                    intent.signal_id,
                    intent.runtime_id,
                    intent.strategy_id,
                    intent.execution_mode.value,
                    intent.trading_date,
                    intent.sequence_number,
                    intent.instrument,
                    intent.security_id,
                    intent.side.value,
                    intent.quantity,
                    intent.order_type.value,
                    intent.limit_price,
                    intent.trigger_price,
                    intent.product_type,
                    intent.basket_id,
                    intent.leg_id,
                    intent.config_fingerprint,
                    intent.risk_decision.value,
                    intent.risk_reason,
                    intent.created_at.isoformat(),
                ),
            )
            return int(cursor.lastrowid or 0)

    def next_sequence_number(
        self, *, strategy_id: str, execution_mode: ExecutionMode, trading_date: str
    ) -> int:
        """Next per-strategy, per-day sequence.

        Read from the table rather than an in-memory counter so a restart
        continues the day's numbering instead of colliding with it. The UNIQUE
        constraint is the real guarantee; this only picks a good candidate.
        """
        row = (
            self._db.connect()
            .execute(
                """
            SELECT COALESCE(MAX(sequence_number), 0) AS current
            FROM order_intents
            WHERE strategy_id = ? AND execution_mode = ? AND trading_date = ?
            """,
                (strategy_id, execution_mode.value, trading_date),
            )
            .fetchone()
        )
        return int(row["current"]) + 1

    # -------------------------------------------------------------- orders
    def record_submission(
        self,
        *,
        intent_id: int,
        order: Order,
        runtime_id: str,
    ) -> int:
        """Persist the broker response, in its own transaction after the call."""
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO orders
                    (intent_id, correlation_id, runtime_id, strategy_id, execution_mode,
                     broker_order_id, status, filled_quantity, average_fill_price,
                     rejection_reason, submitted_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (intent_id) DO UPDATE SET
                    status = excluded.status,
                    broker_order_id = excluded.broker_order_id,
                    filled_quantity = excluded.filled_quantity,
                    average_fill_price = excluded.average_fill_price,
                    rejection_reason = excluded.rejection_reason,
                    updated_at = excluded.updated_at
                """,
                (
                    intent_id,
                    order.correlation_id,
                    runtime_id,
                    order.strategy_id,
                    order.execution_mode.value,
                    order.broker_order_id,
                    order.status.value,
                    order.filled_quantity,
                    order.average_fill_price,
                    order.rejection_reason,
                    order.updated_at.isoformat(),
                    _now(),
                ),
            )
            order_id = int(cursor.lastrowid or 0)
            if order_id == 0:
                row = conn.execute(
                    "SELECT id FROM orders WHERE intent_id = ?", (intent_id,)
                ).fetchone()
                order_id = int(row["id"])
            return order_id

    # ------------------------------------------------- fill + position (one txn)
    def apply_fill(
        self,
        *,
        order_id: int,
        runtime_id: str,
        fill: Fill,
        order_status: OrderStatus,
        instrument: str,
        security_id: str,
        side: Side,
        trading_date: str,
        stop_price: float | None = None,
        target_price: float | None = None,
        last_candle_end_at: str | None = None,
    ) -> Position:
        """Insert the fill and move the position — atomically.

        Returns the resulting position. Idempotent: replaying a fill whose
        ``broker_fill_id`` is already recorded leaves everything unchanged and
        returns the current position.
        """
        with self._db.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM fills WHERE order_id = ? AND broker_fill_id = ?",
                (order_id, fill.broker_fill_id),
            ).fetchone()
            if existing is not None:
                position = self._read_position(
                    conn,
                    strategy_id=fill.strategy_id,
                    execution_mode=fill.execution_mode,
                    trading_date=trading_date,
                    security_id=security_id,
                )
                if position is None:  # pragma: no cover - fill without position
                    raise RuntimeError(
                        f"Fill {fill.broker_fill_id!r} is recorded but its position is missing"
                    )
                return position

            conn.execute(
                """
                INSERT INTO fills
                    (order_id, correlation_id, runtime_id, strategy_id, execution_mode,
                     broker_fill_id, quantity, price, reference_price, slippage_amount,
                     latency_ms, fill_method, charges, filled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    fill.correlation_id,
                    runtime_id,
                    fill.strategy_id,
                    fill.execution_mode.value,
                    fill.broker_fill_id,
                    fill.quantity,
                    fill.price,
                    fill.reference_price,
                    fill.slippage_amount,
                    fill.latency_ms,
                    fill.fill_method,
                    fill.charges,
                    fill.filled_at.isoformat(),
                ),
            )
            conn.execute(
                """
                UPDATE orders
                SET status = ?, filled_quantity = ?, average_fill_price = ?, updated_at = ?
                WHERE id = ?
                """,
                (order_status.value, fill.quantity, fill.price, _now(), order_id),
            )
            position = self._upsert_position(
                conn,
                runtime_id=runtime_id,
                fill=fill,
                instrument=instrument,
                security_id=security_id,
                side=side,
                trading_date=trading_date,
                stop_price=stop_price,
                target_price=target_price,
            )
            self._touch_strategy_state(
                conn,
                runtime_id=runtime_id,
                strategy_id=fill.strategy_id,
                execution_mode=fill.execution_mode,
                trading_date=trading_date,
                last_candle_end_at=last_candle_end_at,
                realised_delta=position.realised_pnl,
            )
            return position

    def _upsert_position(
        self,
        conn: sqlite3.Connection,
        *,
        runtime_id: str,
        fill: Fill,
        instrument: str,
        security_id: str,
        side: Side,
        trading_date: str,
        stop_price: float | None,
        target_price: float | None,
    ) -> Position:
        current = self._read_position(
            conn,
            strategy_id=fill.strategy_id,
            execution_mode=fill.execution_mode,
            trading_date=trading_date,
            security_id=security_id,
        )
        signed = side.sign * fill.quantity
        now = _now()

        if current is None:
            conn.execute(
                """
                INSERT INTO positions
                    (runtime_id, strategy_id, execution_mode, trading_date, instrument,
                     security_id, quantity, average_price, entry_correlation_id, stop_price,
                     target_price, realised_pnl, charges, status, opened_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, 'OPEN', ?, ?)
                """,
                (
                    runtime_id,
                    fill.strategy_id,
                    fill.execution_mode.value,
                    trading_date,
                    instrument,
                    security_id,
                    signed,
                    fill.price,
                    fill.correlation_id,
                    stop_price,
                    target_price,
                    fill.charges,
                    fill.filled_at.isoformat(),
                    now,
                ),
            )
        else:
            new_quantity = current.quantity + signed
            realised = current.realised_pnl
            average = current.average_price

            if current.quantity != 0 and (current.quantity > 0) != (signed > 0):
                # Reducing or closing: realise P&L on the closed portion.
                closed = min(abs(signed), abs(current.quantity))
                direction = 1 if current.quantity > 0 else -1
                realised += direction * closed * (fill.price - current.average_price)
            elif new_quantity != 0:
                # Adding to the position: weighted-average the entry price.
                total = abs(current.quantity) + abs(signed)
                average = (
                    current.average_price * abs(current.quantity) + fill.price * abs(signed)
                ) / total

            status = PositionStatus.CLOSED if new_quantity == 0 else PositionStatus.OPEN
            conn.execute(
                """
                UPDATE positions
                SET quantity = ?, average_price = ?, realised_pnl = ?, charges = ?,
                    stop_price = COALESCE(?, stop_price),
                    target_price = COALESCE(?, target_price),
                    status = ?, closed_at = ?, updated_at = ?
                WHERE strategy_id = ? AND execution_mode = ? AND trading_date = ?
                  AND security_id = ?
                """,
                (
                    new_quantity,
                    average,
                    realised,
                    current.charges + fill.charges,
                    stop_price,
                    target_price,
                    status.value,
                    now if status is PositionStatus.CLOSED else None,
                    now,
                    fill.strategy_id,
                    fill.execution_mode.value,
                    trading_date,
                    security_id,
                ),
            )

        result = self._read_position(
            conn,
            strategy_id=fill.strategy_id,
            execution_mode=fill.execution_mode,
            trading_date=trading_date,
            security_id=security_id,
        )
        assert result is not None
        return result

    def _touch_strategy_state(
        self,
        conn: sqlite3.Connection,
        *,
        runtime_id: str,
        strategy_id: str,
        execution_mode: ExecutionMode,
        trading_date: str,
        last_candle_end_at: str | None,
        realised_delta: float,
    ) -> None:
        conn.execute(
            """
            INSERT INTO strategy_state
                (runtime_id, strategy_id, execution_mode, trading_date,
                 last_candle_end_at, daily_realised_pnl, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (strategy_id, execution_mode, trading_date) DO UPDATE SET
                last_candle_end_at = COALESCE(excluded.last_candle_end_at, last_candle_end_at),
                daily_realised_pnl = excluded.daily_realised_pnl,
                updated_at = excluded.updated_at
            """,
            (
                runtime_id,
                strategy_id,
                execution_mode.value,
                trading_date,
                last_candle_end_at,
                realised_delta,
                _now(),
            ),
        )

    # ------------------------------------------------------------ recovery
    def open_positions(
        self, *, strategy_id: str, execution_mode: ExecutionMode, trading_date: str
    ) -> list[Position]:
        rows = (
            self._db.connect()
            .execute(
                """
            SELECT * FROM positions
            WHERE strategy_id = ? AND execution_mode = ? AND trading_date = ?
              AND status = 'OPEN' AND quantity != 0
            ORDER BY id
            """,
                (strategy_id, execution_mode.value, trading_date),
            )
            .fetchall()
        )
        return [_row_to_position(row) for row in rows]

    def open_orders(self, *, strategy_id: str, execution_mode: ExecutionMode) -> list[sqlite3.Row]:
        return list(
            self._db.connect().execute(
                """
                SELECT * FROM orders
                WHERE strategy_id = ? AND execution_mode = ?
                  AND status NOT IN ('FILLED', 'REJECTED', 'CANCELLED')
                ORDER BY id
                """,
                (strategy_id, execution_mode.value),
            )
        )

    def _read_position(
        self,
        conn: sqlite3.Connection,
        *,
        strategy_id: str,
        execution_mode: ExecutionMode,
        trading_date: str,
        security_id: str,
    ) -> Position | None:
        row = conn.execute(
            """
            SELECT * FROM positions
            WHERE strategy_id = ? AND execution_mode = ? AND trading_date = ? AND security_id = ?
            """,
            (strategy_id, execution_mode.value, trading_date, security_id),
        ).fetchone()
        return None if row is None else _row_to_position(row)

    def load_strategy_state(
        self, *, strategy_id: str, execution_mode: ExecutionMode, trading_date: str
    ) -> sqlite3.Row | None:
        row: sqlite3.Row | None = (
            self._db.connect()
            .execute(
                """
            SELECT * FROM strategy_state
            WHERE strategy_id = ? AND execution_mode = ? AND trading_date = ?
            """,
                (strategy_id, execution_mode.value, trading_date),
            )
            .fetchone()
        )
        return row

    def save_strategy_state(
        self,
        *,
        runtime_id: str,
        strategy_id: str,
        execution_mode: ExecutionMode,
        trading_date: str,
        last_candle_end_at: str | None = None,
        square_off_state: str | None = None,
        entries_blocked: bool | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO strategy_state
                    (runtime_id, strategy_id, execution_mode, trading_date, last_candle_end_at,
                     square_off_state, entries_blocked, payload, updated_at)
                VALUES (?, ?, ?, ?, ?, COALESCE(?, 'PENDING'), COALESCE(?, 0), ?, ?)
                ON CONFLICT (strategy_id, execution_mode, trading_date) DO UPDATE SET
                    last_candle_end_at = COALESCE(excluded.last_candle_end_at, last_candle_end_at),
                    square_off_state = COALESCE(?, square_off_state),
                    entries_blocked = COALESCE(?, entries_blocked),
                    payload = COALESCE(excluded.payload, payload),
                    updated_at = excluded.updated_at
                """,
                (
                    runtime_id,
                    strategy_id,
                    execution_mode.value,
                    trading_date,
                    last_candle_end_at,
                    square_off_state,
                    None if entries_blocked is None else int(entries_blocked),
                    json.dumps(payload) if payload is not None else None,
                    _now(),
                    square_off_state,
                    None if entries_blocked is None else int(entries_blocked),
                ),
            )

    # ------------------------------------------------- notifications/errors
    def record_notification(
        self,
        *,
        runtime_id: str,
        strategy_id: str | None,
        execution_mode: ExecutionMode | None,
        channel: str,
        event_type: str,
        message: str,
        delivered: bool,
        failure_reason: str | None = None,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO notifications
                    (runtime_id, strategy_id, execution_mode, channel, event_type,
                     message, delivered, failure_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    runtime_id,
                    strategy_id,
                    execution_mode.value if execution_mode else None,
                    channel,
                    event_type,
                    message,
                    int(delivered),
                    failure_reason,
                    _now(),
                ),
            )

    def record_error(
        self,
        *,
        runtime_id: str,
        strategy_id: str | None,
        execution_mode: ExecutionMode | None,
        severity: str,
        component: str,
        message: str,
        context: str | None = None,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO errors
                    (runtime_id, strategy_id, execution_mode, severity, component,
                     message, context, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    runtime_id,
                    strategy_id,
                    execution_mode.value if execution_mode else None,
                    severity,
                    component,
                    message,
                    context,
                    _now(),
                ),
            )


def _row_to_position(row: sqlite3.Row) -> Position:
    return Position(
        strategy_id=row["strategy_id"],
        execution_mode=ExecutionMode(row["execution_mode"]),
        trading_date=row["trading_date"],
        instrument=row["instrument"],
        security_id=row["security_id"],
        quantity=int(row["quantity"]),
        average_price=float(row["average_price"]),
        status=PositionStatus(row["status"]),
        opened_at=datetime.fromisoformat(row["opened_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        entry_correlation_id=row["entry_correlation_id"],
        stop_price=row["stop_price"],
        target_price=row["target_price"],
        highest_favourable=row["highest_favourable"],
        lowest_favourable=row["lowest_favourable"],
        realised_pnl=float(row["realised_pnl"]),
        charges=float(row["charges"]),
        closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
    )
