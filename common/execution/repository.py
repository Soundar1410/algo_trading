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
from common.execution.health_events import AUTH_EVENTS, FEED_EVENTS
from common.models import (
    CURRENT_STATE_VERSION,
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
            # Accumulate, never overwrite. Until Phase 4 Part 5 this wrote
            # ``fill.quantity`` and ``fill.price`` directly, so an order with two
            # fills reported the *last* one's quantity and price as though they
            # were the order's. Invisible while nothing produced two fills, and
            # wrong the moment the partial-fill model does. The running totals are
            # read back from the ``fills`` rows — inside this transaction, and
            # after this fill's own INSERT — so they cannot disagree with them.
            totals = conn.execute(
                """
                SELECT COALESCE(SUM(quantity), 0)              AS filled,
                       COALESCE(SUM(price * quantity), 0.0)    AS notional
                FROM fills WHERE order_id = ?
                """,
                (order_id,),
            ).fetchone()
            filled_quantity = int(totals["filled"])
            average_price = float(totals["notional"]) / filled_quantity if filled_quantity else None
            conn.execute(
                """
                UPDATE orders
                SET status = ?, filled_quantity = ?, average_fill_price = ?, updated_at = ?
                WHERE id = ?
                """,
                (order_status.value, filled_quantity, average_price, _now(), order_id),
            )
            self._record_fill_quote(conn, order_id=order_id, fill=fill)
            position, realised_delta = self._upsert_position(
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
                realised_delta=realised_delta,
            )
            return position

    @staticmethod
    def _record_fill_quote(conn: sqlite3.Connection, *, order_id: int, fill: Fill) -> None:
        """Record the submission-time quote this fill was priced against.

        Spec section 6's record list asks for the submission-time quote alongside
        the slippage and latency the ``fills`` row already carries. It lives in a
        side table rather than in new ``fills`` columns because the migration
        runner requires every statement to be replay-safe and SQLite's
        ``ALTER TABLE ... ADD COLUMN`` is not — it errors on the second run.

        Writes nothing when the broker recorded no quote detail, so a live adapter
        that cannot supply one leaves no misleading all-``NULL`` row behind.
        """
        if fill.quote_bid is None and fill.quote_ask is None and fill.latency_applied is None:
            return
        conn.execute(
            """
            INSERT INTO paper_fill_quotes
                (order_id, broker_fill_id, quote_bid, quote_ask, quote_age_ms,
                 latency_applied, fill_method)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (order_id, broker_fill_id) DO NOTHING
            """,
            (
                order_id,
                fill.broker_fill_id,
                fill.quote_bid,
                fill.quote_ask,
                fill.quote_age_ms,
                None if fill.latency_applied is None else int(fill.latency_applied),
                fill.fill_method,
            ),
        )

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
    ) -> tuple[Position, float]:
        """Apply one fill to its position row. Returns ``(position, realised_delta)``.

        ``realised_delta`` is *this fill's* contribution to realised P&L — the
        change in ``realised_pnl`` this call makes, not the row's new total. It is
        what :meth:`_touch_strategy_state` accumulates into the strategy-day's
        running figure; passing the row's cumulative total there instead was the
        Phase 6 Part 1 bug (see that method's docstring).
        """
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
            realised_delta = 0.0
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

            realised_delta = realised - current.realised_pnl
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
        return result, realised_delta

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
        """Accumulate ``realised_delta`` into the strategy-day's running P&L.

        **Phase 6 Part 1 bug, found and fixed here.** Until now this UPSERT wrote
        ``daily_realised_pnl = excluded.daily_realised_pnl`` — an overwrite, not an
        accumulation — while its caller passed the *position's own* cumulative
        ``realised_pnl`` under the name ``realised_delta``. The two facts together
        meant the column silently held whichever position was fills-updated last,
        not the day's total: a strategy that closed one contract and opened a
        *different* one the same day (a different ``security_id``, hence a
        different ``positions`` row) lost the first contract's booked P&L from
        this column the moment the second contract's first fill landed. Invisible
        until now because nothing read this column back — Phase 6 Part 1's
        restart-recovery of :class:`~common.engine.daily_guard.DailyRiskGuard` is
        the first reader, and building recovery on a number that was already wrong
        would have been worse than not restoring it at all.

        Fixed at the source: :meth:`_upsert_position` now returns the true
        per-call delta (this fill's own contribution, not the row's new total),
        and the SQL below adds it to whatever is already stored rather than
        replacing it — the same "accumulate, never overwrite" fix already applied
        to ``fills`` -> ``orders`` a few lines above in :meth:`apply_fill`, missed
        here originally. A fresh row's ``INSERT`` needs no special case: the first
        delta for a new strategy-day is the whole total so far.
        """
        conn.execute(
            """
            INSERT INTO strategy_state
                (runtime_id, strategy_id, execution_mode, trading_date,
                 last_candle_end_at, daily_realised_pnl, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (strategy_id, execution_mode, trading_date) DO UPDATE SET
                last_candle_end_at = COALESCE(excluded.last_candle_end_at, last_candle_end_at),
                daily_realised_pnl = strategy_state.daily_realised_pnl
                                     + excluded.daily_realised_pnl,
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

    def closed_position_count(
        self, *, strategy_id: str, execution_mode: ExecutionMode, trading_date: str
    ) -> int:
        """How many round trips this strategy-day has already closed.

        Phase 6 Part 1 — restart recovery for :class:`~common.engine.daily_guard.
        DailyRiskGuard`'s trade-count limit. Deliberately a query against the
        authoritative ``positions`` table rather than a new counter column: the
        count this needs already exists as data, auditable in SQL, and adding a
        column would be a second, independently-writable copy of the same fact.
        """
        row = (
            self._db.connect()
            .execute(
                """
            SELECT COUNT(*) AS closed FROM positions
            WHERE strategy_id = ? AND execution_mode = ? AND trading_date = ?
              AND status = 'CLOSED'
            """,
                (strategy_id, execution_mode.value, trading_date),
            )
            .fetchone()
        )
        return int(row["closed"])

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
        increment_square_off_attempts: bool = False,
    ) -> None:
        """Upsert this strategy-day's row.

        ``state_version`` is stamped to :data:`common.models.CURRENT_STATE_VERSION`
        on **every** write, never left to the column's schema default (Phase 6
        Part 3). Relying on the default happened to be correct only because
        nothing had ever bumped it; a future migration that changes the default
        would then silently disagree with what this code believes it wrote.

        ``increment_square_off_attempts`` accumulates rather than overwrites —
        the same "add a delta in SQL" pattern the ``daily_realised_pnl`` fix
        (Part 1, D58) established — so this is race-free without a read first.
        ``PersistedSquareOffAuthority._save`` passes ``True`` on every call it
        makes (both the ``IN_PROGRESS`` and ``COMPLETED`` writes), so a normal
        day's attempts count is always >= 2 and a crash-forced retry raises it
        further — closing spec section 10's "persist square-off attempts".
        """
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO strategy_state
                    (runtime_id, strategy_id, execution_mode, trading_date, last_candle_end_at,
                     square_off_state, entries_blocked, payload, state_version,
                     square_off_attempts, updated_at)
                VALUES (?, ?, ?, ?, ?, COALESCE(?, 'PENDING'), COALESCE(?, 0), ?, ?, ?, ?)
                ON CONFLICT (strategy_id, execution_mode, trading_date) DO UPDATE SET
                    last_candle_end_at = COALESCE(excluded.last_candle_end_at, last_candle_end_at),
                    square_off_state = COALESCE(?, square_off_state),
                    entries_blocked = COALESCE(?, entries_blocked),
                    payload = COALESCE(excluded.payload, payload),
                    state_version = excluded.state_version,
                    square_off_attempts = strategy_state.square_off_attempts
                                           + excluded.square_off_attempts,
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
                    CURRENT_STATE_VERSION,
                    int(increment_square_off_attempts),
                    _now(),
                    square_off_state,
                    None if entries_blocked is None else int(entries_blocked),
                ),
            )

    def update_position_marks(
        self,
        *,
        strategy_id: str,
        execution_mode: ExecutionMode,
        trading_date: str,
        security_id: str,
        highest_favourable: float,
        lowest_favourable: float,
    ) -> None:
        """Persist the running MFE/MAE excursion for an open position.

        Phase 6 Part 3. Deliberately **outside** the fill path — ``_upsert_position``
        only ever runs from :meth:`apply_fill`, and MFE/MAE change on every tick
        while a position is open, not just at entry/exit. Called from the same
        per-candle-while-open checkpoint :class:`~common.engine.engine.TradingEngine`
        already uses for ``_persist_exit_state`` (Part 2), not a new one — so this
        adds no write frequency beyond what limitation 24 already scopes.

        A no-op (0 rows affected) if the position is not OPEN or does not exist —
        callers only invoke this while they hold an open position in memory, so
        that should never happen, but the write itself makes no assumption about it.
        """
        with self._db.transaction() as conn:
            conn.execute(
                """
                UPDATE positions
                SET highest_favourable = ?, lowest_favourable = ?
                WHERE strategy_id = ? AND execution_mode = ? AND trading_date = ?
                  AND security_id = ? AND status = 'OPEN'
                """,
                (
                    highest_favourable,
                    lowest_favourable,
                    strategy_id,
                    execution_mode.value,
                    trading_date,
                    security_id,
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

    # ---------------------------------------------------------- diagnostics
    def record_auth_event(
        self,
        *,
        runtime_id: str,
        event: str,
        token_source: str | None = None,
        token_expiry: str | None = None,
        requests_made: int = 0,
        detail: str | None = None,
    ) -> None:
        """Write one row to ``auth_events`` (migration 0002).

        Written on state changes and failures only, never per tick or per
        candle — the migration's own rule (see its module docstring).
        """
        if event not in AUTH_EVENTS:
            raise ValueError(f"unknown auth event {event!r}; must be one of {sorted(AUTH_EVENTS)}")
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO auth_events
                    (runtime_id, event, token_source, token_expiry, requests_made,
                     detail, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (runtime_id, event, token_source, token_expiry, requests_made, detail, _now()),
            )

    def record_feed_event(
        self,
        *,
        runtime_id: str,
        event: str,
        reason_code: int | None = None,
        reason: str | None = None,
        attempt: int | None = None,
        downtime_seconds: float | None = None,
        expected_subscriptions: int | None = None,
        active_subscriptions: int | None = None,
        gap_candles_discarded: int = 0,
        security_id: str | None = None,
    ) -> None:
        """Write one row to ``feed_events`` (migration 0002).

        Written on state changes and failures only, never per tick or per
        candle — the migration's own rule (see its module docstring).
        """
        if event not in FEED_EVENTS:
            raise ValueError(f"unknown feed event {event!r}; must be one of {sorted(FEED_EVENTS)}")
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO feed_events
                    (runtime_id, event, reason_code, reason, attempt, downtime_seconds,
                     expected_subscriptions, active_subscriptions, gap_candles_discarded,
                     security_id, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    runtime_id,
                    event,
                    reason_code,
                    reason,
                    attempt,
                    downtime_seconds,
                    expected_subscriptions,
                    active_subscriptions,
                    gap_candles_discarded,
                    security_id,
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
