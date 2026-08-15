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
from common.execution.audit_events import AUDIT_ACTIONS
from common.execution.health_events import AUTH_EVENTS, FEED_EVENTS
from common.logging import redact_for_persistence
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
            # SQLite leaves ``lastrowid`` unchanged when the UPSERT takes its
            # UPDATE arm. Reusing it can therefore return the ID of some
            # unrelated prior insert on this connection and attach a recovered
            # fill to the wrong/non-existent order. Read back through the
            # unique intent identity on both insert and update paths.
            del cursor
            row = conn.execute("SELECT id FROM orders WHERE intent_id = ?", (intent_id,)).fetchone()
            if row is None:  # pragma: no cover - the UPSERT above must create it
                raise RuntimeError(f"order upsert for intent_id={intent_id} produced no row")
            return int(row["id"])

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
        cycle_id: str | None = None,
    ) -> Position:
        """Insert the fill and move the position — atomically.

        Returns the resulting position. Idempotent: replaying a fill whose
        ``broker_fill_id`` is already recorded leaves everything unchanged and
        returns the current position.

        ``cycle_id``, when given (a positional strategy's durable cycle
        identity — spec review correction 4), changes how the *position row*
        is resolved: through ``cycle_position_bindings`` rather than through
        ``(trading_date, security_id)`` — see :meth:`_upsert_position`. Every
        existing intraday caller passes ``None`` and this method is
        byte-identical to before that parameter existed.
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
                    cycle_id=cycle_id,
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
                cycle_id=cycle_id,
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
        cycle_id: str | None = None,
    ) -> tuple[Position, float]:
        """Apply one fill to its position row. Returns ``(position, realised_delta)``.

        ``realised_delta`` is *this fill's* contribution to realised P&L — the
        change in ``realised_pnl`` this call makes, not the row's new total. It is
        what :meth:`_touch_strategy_state` accumulates into the strategy-day's
        running figure; passing the row's cumulative total there instead was the
        Phase 6 Part 1 bug (see that method's docstring).

        **``cycle_id`` (spec review correction 4).** ``positions.trading_date``
        is written once, on the row's own creation, and is never rewritten by a
        later-day fill — a Friday fill against a Wednesday-opened leg updates
        this same row's ``quantity``/``average_price``/... in place and leaves
        ``trading_date`` at Wednesday's date; Friday's own event date is
        recorded where the event actually happened (the caller's
        ``order_intents``/``orders``/``fills``/``trade_ledger`` rows, all
        still keyed on the ``trading_date`` this call was given). What changes
        with ``cycle_id`` is purely *which row* a fill lands on: resolved
        through ``cycle_position_bindings`` (keyed ``(cycle_id, security_id)``)
        instead of ``(strategy_id, execution_mode, trading_date, security_id)``,
        so a security's position row is the *same* row across every trading
        date this cycle spans, including a close-and-reopen (the "reopening a
        fully-closed identity from flat" branch below fires against that same
        row rather than creating a new one).

        The binding write happens on the *same* ``conn``, inside this call's
        own already-open transaction (never a second one) — so a binding
        failure (e.g. a UNIQUE violation from a genuinely contradictory
        concurrent write) rolls back the position mutation this call just made,
        and a position mutation can never commit without its binding. No
        binding is ever rewritten to point at a different ``position_id``
        once created; :func:`~runtimes.positional_options.
        positional_multi_leg_engine_worker.recover_cycle`'s reconciliation is
        what detects a missing, duplicate, or contradictory binding at
        restart and fails closed.
        """
        current = self._read_position(
            conn,
            strategy_id=fill.strategy_id,
            execution_mode=fill.execution_mode,
            trading_date=trading_date,
            security_id=security_id,
            cycle_id=cycle_id,
        )
        signed = side.sign * fill.quantity
        now = _now()

        if current is None:
            cursor = conn.execute(
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
            if cycle_id is not None:
                new_position_id = int(cursor.lastrowid or 0)
                conn.execute(
                    """
                    INSERT INTO cycle_position_bindings
                        (cycle_id, security_id, position_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (cycle_id, security_id, new_position_id, now),
                )
        else:
            new_quantity = current.quantity + signed
            realised = current.realised_pnl
            average = current.average_price
            # Which entry this identity currently represents — reset only
            # on a genuine reopen from flat (see below), otherwise carried
            # forward unchanged, same as every other untouched column.
            opened_at = current.opened_at.isoformat()
            entry_correlation_id = current.entry_correlation_id
            ledger_entry: tuple[object, ...] | None = None

            if current.quantity != 0 and (current.quantity > 0) != (signed > 0):
                # Reducing or closing: realise P&L on the closed portion.
                closed = min(abs(signed), abs(current.quantity))
                direction = 1 if current.quantity > 0 else -1
                realised_delta_amount = direction * closed * (fill.price - current.average_price)
                realised += realised_delta_amount
                ledger_entry = self._build_ledger_entry(
                    conn,
                    runtime_id=runtime_id,
                    fill=fill,
                    instrument=instrument,
                    security_id=security_id,
                    trading_date=trading_date,
                    entry_side=(Side.BUY if current.quantity > 0 else Side.SELL),
                    quantity=closed,
                    entry_price=current.average_price,
                    gross_pnl=realised_delta_amount,
                    entry_correlation_id=entry_correlation_id,
                    opened_at=opened_at,
                )
            elif new_quantity != 0:
                # Adding to the position: weighted-average the entry price.
                total = abs(current.quantity) + abs(signed)
                average = (
                    current.average_price * abs(current.quantity) + fill.price * abs(signed)
                ) / total
                if current.quantity == 0:
                    # Reopening a fully-closed identity from flat, not
                    # scaling into an already-open one — this fill is a new
                    # entry. Until this fix, opened_at/entry_correlation_id
                    # were never touched outside the initial INSERT, so a
                    # second round trip on the same (strategy, mode, day,
                    # security) identity silently kept reporting the
                    # *first* entry's timestamp and correlation id forever.
                    # Bookkeeping only — no entry/exit/risk decision reads
                    # either column.
                    opened_at = fill.filled_at.isoformat()
                    entry_correlation_id = fill.correlation_id

            realised_delta = realised - current.realised_pnl
            status = PositionStatus.CLOSED if new_quantity == 0 else PositionStatus.OPEN
            update_values = (
                new_quantity,
                average,
                realised,
                current.charges + fill.charges,
                stop_price,
                target_price,
                status.value,
                now if status is PositionStatus.CLOSED else None,
                now,
                opened_at,
                entry_correlation_id,
            )
            if cycle_id is not None:
                # Located through the binding, never through trading_date —
                # this is what lets a later-day fill reach a row opened on an
                # earlier trading_date (spec review correction 4).
                conn.execute(
                    """
                    UPDATE positions
                    SET quantity = ?, average_price = ?, realised_pnl = ?, charges = ?,
                        stop_price = COALESCE(?, stop_price),
                        target_price = COALESCE(?, target_price),
                        status = ?, closed_at = ?, updated_at = ?,
                        opened_at = ?, entry_correlation_id = ?
                    WHERE id = (
                        SELECT position_id FROM cycle_position_bindings
                        WHERE cycle_id = ? AND security_id = ?
                    )
                    """,
                    (*update_values, cycle_id, security_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE positions
                    SET quantity = ?, average_price = ?, realised_pnl = ?, charges = ?,
                        stop_price = COALESCE(?, stop_price),
                        target_price = COALESCE(?, target_price),
                        status = ?, closed_at = ?, updated_at = ?,
                        opened_at = ?, entry_correlation_id = ?
                    WHERE strategy_id = ? AND execution_mode = ? AND trading_date = ?
                      AND security_id = ?
                    """,
                    (
                        *update_values,
                        fill.strategy_id,
                        fill.execution_mode.value,
                        trading_date,
                        security_id,
                    ),
                )
            if ledger_entry is not None:
                conn.execute(
                    """
                    INSERT INTO trade_ledger
                        (runtime_id, strategy_id, execution_mode, trading_date, instrument,
                         security_id, entry_side, quantity, entry_price, exit_price, gross_pnl,
                         entry_charges, exit_charges, entry_correlation_id, exit_correlation_id,
                         exit_broker_fill_id, opened_at, closed_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (exit_correlation_id, exit_broker_fill_id) DO NOTHING
                    """,
                    ledger_entry,
                )

        result = self._read_position(
            conn,
            strategy_id=fill.strategy_id,
            execution_mode=fill.execution_mode,
            trading_date=trading_date,
            security_id=security_id,
            cycle_id=cycle_id,
        )
        assert result is not None
        return result, realised_delta

    @staticmethod
    def _build_ledger_entry(
        conn: sqlite3.Connection,
        *,
        runtime_id: str,
        fill: Fill,
        instrument: str,
        security_id: str,
        trading_date: str,
        entry_side: Side,
        quantity: int,
        entry_price: float,
        gross_pnl: float,
        entry_correlation_id: str | None,
        opened_at: str,
    ) -> tuple[object, ...]:
        """The durable ``trade_ledger`` row for one realising fill.

        ``entry_charges`` sums every fill recorded under
        ``entry_correlation_id`` — exact for a strategy that never scales
        in (the only kind this project runs today, see the migration's own
        comment); a scaling strategy would attribute all of it to the
        first partial close, a documented precision limit rather than a
        schema gap. ``exit_charges`` is this fill's own charges — no
        apportionment needed, it is a single fill.
        """
        entry_charges = 0.0
        if entry_correlation_id is not None:
            entry_charges = float(
                conn.execute(
                    "SELECT COALESCE(SUM(charges), 0.0) AS total FROM fills "
                    "WHERE correlation_id = ?",
                    (entry_correlation_id,),
                ).fetchone()["total"]
            )
        return (
            runtime_id,
            fill.strategy_id,
            fill.execution_mode.value,
            trading_date,
            instrument,
            security_id,
            entry_side.value,
            quantity,
            entry_price,
            fill.price,
            gross_pnl,
            entry_charges,
            fill.charges,
            entry_correlation_id,
            fill.correlation_id,
            fill.broker_fill_id,
            opened_at,
            fill.filled_at.isoformat(),
            _now(),
        )

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

    def open_positions_all_dates(
        self, *, strategy_id: str, execution_mode: ExecutionMode
    ) -> list[Position]:
        """Every open position for this strategy/mode, across every
        trading_date ever recorded — not scoped to "today" like
        :meth:`open_positions`.

        Phase 10's mode-transition safety check needs this: a position
        opened on a prior trading date and never closed (e.g. from a bug or
        a crash) must still block a mode change today, not only an open
        position dated exactly today.
        """
        rows = (
            self._db.connect()
            .execute(
                """
            SELECT * FROM positions
            WHERE strategy_id = ? AND execution_mode = ?
              AND status = 'OPEN' AND quantity != 0
            ORDER BY trading_date, id
            """,
                (strategy_id, execution_mode.value),
            )
            .fetchall()
        )
        return [_row_to_position(row) for row in rows]

    def open_positions_for_cycle(self, *, cycle_id: str) -> list[Position]:
        """Every open position bound to one positional cycle — the
        cycle-scoped sibling of :meth:`open_positions`, joined through
        ``cycle_position_bindings`` rather than ``trading_date``.
        """
        rows = (
            self._db.connect()
            .execute(
                """
                SELECT p.* FROM positions p
                JOIN cycle_position_bindings b ON b.position_id = p.id
                WHERE b.cycle_id = ? AND p.status = 'OPEN' AND p.quantity != 0
                ORDER BY p.id
                """,
                (cycle_id,),
            )
            .fetchall()
        )
        return [_row_to_position(row) for row in rows]

    def positions_all_dates(
        self, *, strategy_id: str, execution_mode: ExecutionMode
    ) -> list[Position]:
        """Every position row for reconciliation, including CLOSED history.

        Broker-authoritative reconciliation must distinguish "the broker has
        exposure we have never seen" from "the broker still has exposure that
        local state says was closed".  Supplying only open rows makes the latter
        classification unreachable and silently downgrades it to BROKER_ONLY.
        """
        rows = (
            self._db.connect()
            .execute(
                """
            SELECT * FROM positions
            WHERE strategy_id = ? AND execution_mode = ?
            ORDER BY trading_date, id
            """,
                (strategy_id, execution_mode.value),
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
                  AND status NOT IN ('FILLED', 'REJECTED', 'CANCELLED', 'EXPIRED')
                ORDER BY id
                """,
                (strategy_id, execution_mode.value),
            )
        )

    def all_orders(self, *, strategy_id: str, execution_mode: ExecutionMode) -> list[sqlite3.Row]:
        """Every order for this strategy/mode, terminal states included —
        unlike :meth:`open_orders`. Broker-vs-local reconciliation
        (``common.reconciliation.compare_orders``) needs this: a locally
        ``FILLED`` order is correctly absent from ``open_orders`` but the
        broker's own order-book report still names it, and without the
        terminal rows here that match would be misread as ``BROKER_ONLY``.
        """
        return list(
            self._db.connect().execute(
                """
                SELECT * FROM orders
                WHERE strategy_id = ? AND execution_mode = ?
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
        cycle_id: str | None = None,
    ) -> Position | None:
        """The current position row for this identity.

        ``cycle_id``, when given, resolves exclusively through
        ``cycle_position_bindings`` — never falls back to the
        ``(trading_date, security_id)`` lookup, so a cycle-scoped caller can
        never accidentally adopt an unrelated row that happens to share
        today's date and security id. No binding yet means no position yet
        (``None``), which is exactly right for a leg's very first fill: the
        caller's INSERT branch creates both the row and its binding together.
        """
        if cycle_id is not None:
            binding = conn.execute(
                "SELECT position_id FROM cycle_position_bindings "
                "WHERE cycle_id = ? AND security_id = ?",
                (cycle_id, security_id),
            ).fetchone()
            if binding is None:
                return None
            row = conn.execute(
                "SELECT * FROM positions WHERE id = ?",
                (int(binding["position_id"]),),
            ).fetchone()
            return None if row is None else _row_to_position(row)
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

    def record_audit_event(
        self,
        *,
        runtime_id: str,
        action: str,
        actor: str,
        strategy_id: str | None = None,
        execution_mode: ExecutionMode | None = None,
        detail: str | None = None,
    ) -> None:
        """Write one row to ``audit_events`` (migration 0004).

        Spec section 11: "Live-impacting commands must require explicit
        confirmation and log an audit event." This is that sink — every
        control-tier script (``stop_runtime``, ``stop_strategy``,
        ``square_off``) calls it once per action, never per tick or candle.
        """
        if action not in AUDIT_ACTIONS:
            raise ValueError(
                f"unknown audit action {action!r}; must be one of {sorted(AUDIT_ACTIONS)}"
            )
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO audit_events
                    (runtime_id, strategy_id, execution_mode, action, actor, detail, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    runtime_id,
                    strategy_id,
                    execution_mode.value if execution_mode else None,
                    action,
                    actor,
                    # Free text a logging handler never sees — see
                    # common.logging.redact_for_persistence's own docstring.
                    redact_for_persistence(detail),
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

    # ------------------------------------------------------ multi-leg baskets
    # Migration 0009. Generic to any multi-leg strategy — see that migration's
    # own module docstring for why these two tables exist alongside (never
    # competing with) `positions`/`trade_ledger`. Returns raw rows rather than
    # a `common.engine` dataclass: this module must not import `common.engine`
    # (the same layering rule `gateway.py`/`square_off.py` keep via
    # `TYPE_CHECKING`-only imports) — the row <-> `Basket`/`LegInstance`
    # conversion lives in `common.engine.multi_leg_state`, the caller of these
    # methods.
    def upsert_strategy_basket(
        self,
        *,
        runtime_id: str,
        strategy_id: str,
        execution_mode: ExecutionMode,
        trading_date: str,
        basket_id: str,
        lifecycle_state: str,
        entries_consumed: bool,
        day_blocked_reason: str | None,
        adjustment_count: int,
        pending_replacement_role: str | None,
        pending_replacement_state: str | None,
        original_combined_basis: float | None,
        square_off_state: str,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO strategy_baskets
                    (runtime_id, strategy_id, execution_mode, trading_date, basket_id,
                     lifecycle_state, entries_consumed, day_blocked_reason, adjustment_count,
                     pending_replacement_role, pending_replacement_state,
                     original_combined_basis, square_off_state, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT (strategy_id, execution_mode, trading_date, basket_id) DO UPDATE SET
                    lifecycle_state = excluded.lifecycle_state,
                    entries_consumed = excluded.entries_consumed,
                    day_blocked_reason = excluded.day_blocked_reason,
                    adjustment_count = excluded.adjustment_count,
                    pending_replacement_role = excluded.pending_replacement_role,
                    pending_replacement_state = excluded.pending_replacement_state,
                    original_combined_basis = excluded.original_combined_basis,
                    square_off_state = excluded.square_off_state,
                    version = strategy_baskets.version + 1,
                    updated_at = excluded.updated_at
                """,
                (
                    runtime_id,
                    strategy_id,
                    execution_mode.value,
                    trading_date,
                    basket_id,
                    lifecycle_state,
                    int(entries_consumed),
                    day_blocked_reason,
                    adjustment_count,
                    pending_replacement_role,
                    pending_replacement_state,
                    original_combined_basis,
                    square_off_state,
                    _now(),
                    _now(),
                ),
            )

    def load_strategy_basket(
        self, *, strategy_id: str, execution_mode: ExecutionMode, trading_date: str
    ) -> sqlite3.Row | None:
        row: sqlite3.Row | None = (
            self._db.connect()
            .execute(
                """
                SELECT * FROM strategy_baskets
                WHERE strategy_id = ? AND execution_mode = ? AND trading_date = ?
                """,
                (strategy_id, execution_mode.value, trading_date),
            )
            .fetchone()
        )
        return row

    def upsert_strategy_leg(
        self,
        *,
        runtime_id: str,
        strategy_id: str,
        execution_mode: ExecutionMode,
        trading_date: str,
        basket_id: str,
        leg_id: str,
        leg_role: str,
        leg_sequence: int,
        is_replacement: bool,
        replaces_leg_id: str | None,
        security_id: str | None,
        symbol: str | None,
        strike: float | None,
        expiry: str | None,
        lot_size: int | None,
        side: str | None,
        quantity: int | None,
        entry_price: float | None,
        entry_time: str | None,
        entry_correlation_id: str | None,
        exit_price: float | None,
        exit_time: str | None,
        exit_reason: str | None,
        exit_correlation_id: str | None,
        realized_gross_pnl: float | None,
        state: str,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO strategy_legs
                    (runtime_id, strategy_id, execution_mode, trading_date, basket_id, leg_id,
                     leg_role, leg_sequence, is_replacement, replaces_leg_id, security_id,
                     symbol, strike, expiry, lot_size, side, quantity, entry_price, entry_time,
                     entry_correlation_id, exit_price, exit_time, exit_reason, exit_correlation_id,
                     realized_gross_pnl, state, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, 1, ?, ?)
                ON CONFLICT (strategy_id, execution_mode, trading_date, leg_id) DO UPDATE SET
                    leg_role = excluded.leg_role,
                    leg_sequence = excluded.leg_sequence,
                    is_replacement = excluded.is_replacement,
                    replaces_leg_id = excluded.replaces_leg_id,
                    security_id = excluded.security_id,
                    symbol = excluded.symbol,
                    strike = excluded.strike,
                    expiry = excluded.expiry,
                    lot_size = excluded.lot_size,
                    side = excluded.side,
                    quantity = excluded.quantity,
                    entry_price = excluded.entry_price,
                    entry_time = excluded.entry_time,
                    entry_correlation_id = excluded.entry_correlation_id,
                    exit_price = excluded.exit_price,
                    exit_time = excluded.exit_time,
                    exit_reason = excluded.exit_reason,
                    exit_correlation_id = excluded.exit_correlation_id,
                    realized_gross_pnl = excluded.realized_gross_pnl,
                    state = excluded.state,
                    version = strategy_legs.version + 1,
                    updated_at = excluded.updated_at
                """,
                (
                    runtime_id,
                    strategy_id,
                    execution_mode.value,
                    trading_date,
                    basket_id,
                    leg_id,
                    leg_role,
                    leg_sequence,
                    int(is_replacement),
                    replaces_leg_id,
                    security_id,
                    symbol,
                    strike,
                    expiry,
                    lot_size,
                    side,
                    quantity,
                    entry_price,
                    entry_time,
                    entry_correlation_id,
                    exit_price,
                    exit_time,
                    exit_reason,
                    exit_correlation_id,
                    realized_gross_pnl,
                    state,
                    _now(),
                    _now(),
                ),
            )

    def load_strategy_legs(
        self, *, strategy_id: str, execution_mode: ExecutionMode, trading_date: str
    ) -> list[sqlite3.Row]:
        return list(
            self._db.connect()
            .execute(
                """
                SELECT * FROM strategy_legs
                WHERE strategy_id = ? AND execution_mode = ? AND trading_date = ?
                ORDER BY leg_role, leg_sequence
                """,
                (strategy_id, execution_mode.value, trading_date),
            )
            .fetchall()
        )

    def load_strategy_legs_for_basket(self, *, basket_id: str) -> list[sqlite3.Row]:
        """Every leg instance for one basket, across strategy/mode/date scope
        boundaries — used by the dashboard's basket drill-down, which already
        has ``basket_id`` from a prior query and does not need to re-supply
        the scope that produced it."""
        return list(
            self._db.connect()
            .execute(
                "SELECT * FROM strategy_legs WHERE basket_id = ? ORDER BY leg_role, leg_sequence",
                (basket_id,),
            )
            .fetchall()
        )

    def leg_order_history(self, *, leg_id: str) -> list[sqlite3.Row]:
        """Every ``order_intents`` row ever reserved for ``leg_id``, each
        joined with its ``orders`` row if the broker call was ever recorded
        (``LEFT JOIN`` — a row with ``order_status IS NULL`` means the intent
        was reserved but no submission outcome was ever persisted, the P0-1/
        P0-4 "crashed between reserve and record" case).

        This — never the mutable ``strategy_legs`` projection alone — is what
        restart reconciliation (:mod:`runtimes.intraday_options.
        multi_leg_engine_worker`'s ``recover_basket``) cross-checks a leg's
        projected state against: the authoritative execution history for
        *this exact leg instance*, ordered by ``sequence_number`` so the
        entry attempt (if any) sorts before a later exit attempt.
        """
        return list(
            self._db.connect()
            .execute(
                """
                SELECT
                    oi.correlation_id, oi.side, oi.quantity, oi.risk_decision,
                    oi.risk_reason, oi.sequence_number, oi.submission_reserved,
                    o.status AS order_status,
                    o.filled_quantity AS order_filled_quantity,
                    o.average_fill_price AS order_average_fill_price,
                    o.broker_order_id AS order_broker_order_id,
                    o.rejection_reason AS order_rejection_reason
                FROM order_intents oi
                LEFT JOIN orders o ON o.intent_id = oi.id
                WHERE oi.leg_id = ?
                ORDER BY oi.sequence_number
                """,
                (leg_id,),
            )
            .fetchall()
        )

    def load_strategy_baskets(
        self,
        *,
        strategy_id: str,
        execution_mode: ExecutionMode,
        trading_date: str | None = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        """Baskets for a strategy, most recent trading date first — the
        dashboard's basket-list query. ``trading_date=None`` lists every date
        on record (bounded by ``limit``); a caller wanting "today only" passes
        it explicitly, same as every other scoped query in this class."""
        if trading_date is not None:
            return list(
                self._db.connect()
                .execute(
                    """
                    SELECT * FROM strategy_baskets
                    WHERE strategy_id = ? AND execution_mode = ? AND trading_date = ?
                    ORDER BY basket_id
                    """,
                    (strategy_id, execution_mode.value, trading_date),
                )
                .fetchall()
            )
        return list(
            self._db.connect()
            .execute(
                """
                SELECT * FROM strategy_baskets
                WHERE strategy_id = ? AND execution_mode = ?
                ORDER BY trading_date DESC, basket_id
                LIMIT ?
                """,
                (strategy_id, execution_mode.value, limit),
            )
            .fetchall()
        )

    # ------------------------------------------------------ positional cycles
    # Migration 0010 (strategy-weekly-delta-neutral). Generic to any
    # positional multi-leg strategy — see that migration's own docstring for
    # why cycle_id exists alongside (never replacing) trading_date. Returns
    # raw rows, same discipline as the multi-leg basket methods above: this
    # module must not import common.engine; the row <-> Cycle/CycleLeg
    # conversion lives in common.engine.positional.positional_state.
    def upsert_cycle(
        self,
        *,
        runtime_id: str,
        strategy_id: str,
        execution_mode: ExecutionMode,
        cycle_id: str,
        underlying: str,
        resolved_expiry_date: str,
        state: str,
        entries_consumed: bool,
        day_blocked_reason: str | None,
        original_net_credit: float | None,
        original_max_loss: float | None,
        original_wing_width: float | None,
        adjustments_today: int,
        adjustments_today_date: str | None,
        adjustments_this_cycle: int,
        last_adjustment_at: str | None,
        pending_adjustment_role: str | None,
        pending_adjustment_state: str | None,
        square_off_state: str,
        opened_trading_date: str,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO strategy_cycles
                    (runtime_id, strategy_id, execution_mode, cycle_id, underlying,
                     resolved_expiry_date, state, entries_consumed, day_blocked_reason,
                     original_net_credit, original_max_loss, original_wing_width,
                     adjustments_today, adjustments_today_date, adjustments_this_cycle,
                     last_adjustment_at, pending_adjustment_role, pending_adjustment_state,
                     square_off_state, opened_trading_date, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT (cycle_id) DO UPDATE SET
                    state = excluded.state,
                    entries_consumed = excluded.entries_consumed,
                    day_blocked_reason = excluded.day_blocked_reason,
                    original_net_credit = excluded.original_net_credit,
                    original_max_loss = excluded.original_max_loss,
                    original_wing_width = excluded.original_wing_width,
                    adjustments_today = excluded.adjustments_today,
                    adjustments_today_date = excluded.adjustments_today_date,
                    adjustments_this_cycle = excluded.adjustments_this_cycle,
                    last_adjustment_at = excluded.last_adjustment_at,
                    pending_adjustment_role = excluded.pending_adjustment_role,
                    pending_adjustment_state = excluded.pending_adjustment_state,
                    square_off_state = excluded.square_off_state,
                    version = strategy_cycles.version + 1,
                    updated_at = excluded.updated_at
                """,
                (
                    runtime_id,
                    strategy_id,
                    execution_mode.value,
                    cycle_id,
                    underlying,
                    resolved_expiry_date,
                    state,
                    int(entries_consumed),
                    day_blocked_reason,
                    original_net_credit,
                    original_max_loss,
                    original_wing_width,
                    adjustments_today,
                    adjustments_today_date,
                    adjustments_this_cycle,
                    last_adjustment_at,
                    pending_adjustment_role,
                    pending_adjustment_state,
                    square_off_state,
                    opened_trading_date,
                    _now(),
                    _now(),
                ),
            )

    def load_cycle(self, *, cycle_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = (
            self._db.connect()
            .execute("SELECT * FROM strategy_cycles WHERE cycle_id = ?", (cycle_id,))
            .fetchone()
        )
        return row

    def load_open_cycle(
        self, *, runtime_id: str, strategy_id: str, execution_mode: ExecutionMode
    ) -> sqlite3.Row | None:
        """The one non-terminal cycle for this strategy/mode, if any — the
        restart-recovery entrypoint. Relies on the same terminal-state
        vocabulary ``idx_one_open_cycle`` (migration 0010) enforces in the
        database itself, so this can never disagree with what the schema
        actually permits to coexist."""
        row: sqlite3.Row | None = (
            self._db.connect()
            .execute(
                """
                SELECT * FROM strategy_cycles
                WHERE runtime_id = ? AND strategy_id = ? AND execution_mode = ?
                  AND state NOT IN ('COMPLETED', 'FAILED', 'ABANDONED')
                """,
                (runtime_id, strategy_id, execution_mode.value),
            )
            .fetchone()
        )
        return row

    def load_cycles(
        self,
        *,
        runtime_id: str,
        strategy_id: str,
        execution_mode: ExecutionMode,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        """Cycles for a strategy, most recently opened first — the
        dashboard's cycle-history query."""
        return list(
            self._db.connect()
            .execute(
                """
                SELECT * FROM strategy_cycles
                WHERE runtime_id = ? AND strategy_id = ? AND execution_mode = ?
                ORDER BY opened_trading_date DESC, id DESC
                LIMIT ?
                """,
                (runtime_id, strategy_id, execution_mode.value, limit),
            )
            .fetchall()
        )

    def upsert_cycle_leg(
        self,
        *,
        runtime_id: str,
        strategy_id: str,
        execution_mode: ExecutionMode,
        cycle_id: str,
        leg_id: str,
        leg_role: str,
        option_type: str | None,
        leg_sequence: int,
        is_replacement: bool,
        replaces_leg_id: str | None,
        security_id: str | None,
        symbol: str | None,
        strike: float | None,
        expiry: str | None,
        lot_size: int | None,
        side: str | None,
        quantity: int | None,
        entry_price: float | None,
        entry_time: str | None,
        entry_correlation_id: str | None,
        exit_price: float | None,
        exit_time: str | None,
        exit_reason: str | None,
        exit_correlation_id: str | None,
        realized_gross_pnl: float | None,
        state: str,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO strategy_cycle_legs
                    (runtime_id, strategy_id, execution_mode, cycle_id, leg_id, leg_role,
                     option_type, leg_sequence, is_replacement, replaces_leg_id, security_id,
                     symbol, strike, expiry, lot_size, side, quantity, entry_price, entry_time,
                     entry_correlation_id, exit_price, exit_time, exit_reason, exit_correlation_id,
                     realized_gross_pnl, state, version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, 1, ?, ?)
                ON CONFLICT (cycle_id, leg_id) DO UPDATE SET
                    leg_role = excluded.leg_role,
                    option_type = excluded.option_type,
                    leg_sequence = excluded.leg_sequence,
                    is_replacement = excluded.is_replacement,
                    replaces_leg_id = excluded.replaces_leg_id,
                    security_id = excluded.security_id,
                    symbol = excluded.symbol,
                    strike = excluded.strike,
                    expiry = excluded.expiry,
                    lot_size = excluded.lot_size,
                    side = excluded.side,
                    quantity = excluded.quantity,
                    entry_price = excluded.entry_price,
                    entry_time = excluded.entry_time,
                    entry_correlation_id = excluded.entry_correlation_id,
                    exit_price = excluded.exit_price,
                    exit_time = excluded.exit_time,
                    exit_reason = excluded.exit_reason,
                    exit_correlation_id = excluded.exit_correlation_id,
                    realized_gross_pnl = excluded.realized_gross_pnl,
                    state = excluded.state,
                    version = strategy_cycle_legs.version + 1,
                    updated_at = excluded.updated_at
                """,
                (
                    runtime_id,
                    strategy_id,
                    execution_mode.value,
                    cycle_id,
                    leg_id,
                    leg_role,
                    option_type,
                    leg_sequence,
                    int(is_replacement),
                    replaces_leg_id,
                    security_id,
                    symbol,
                    strike,
                    expiry,
                    lot_size,
                    side,
                    quantity,
                    entry_price,
                    entry_time,
                    entry_correlation_id,
                    exit_price,
                    exit_time,
                    exit_reason,
                    exit_correlation_id,
                    realized_gross_pnl,
                    state,
                    _now(),
                    _now(),
                ),
            )

    def load_cycle_legs(self, *, cycle_id: str) -> list[sqlite3.Row]:
        return list(
            self._db.connect()
            .execute(
                "SELECT * FROM strategy_cycle_legs WHERE cycle_id = ? "
                "ORDER BY leg_role, leg_sequence",
                (cycle_id,),
            )
            .fetchall()
        )

    def cycle_order_history(self, *, cycle_id: str) -> list[sqlite3.Row]:
        """Every ``order_intents`` row ever reserved for one cycle, across
        every leg — the ``basket_id``-keyed sibling of :meth:`leg_order_history`
        (``order_intents.basket_id`` is set to the cycle's own ``cycle_id`` by
        the positional engine's gateway calls, reusing the identity space
        migration 0009's own docstring already established rather than adding
        a dedicated binding table for it)."""
        return list(
            self._db.connect()
            .execute(
                """
                SELECT
                    oi.leg_id, oi.correlation_id, oi.side, oi.quantity, oi.risk_decision,
                    oi.risk_reason, oi.sequence_number, oi.submission_reserved,
                    o.status AS order_status,
                    o.filled_quantity AS order_filled_quantity,
                    o.average_fill_price AS order_average_fill_price,
                    o.broker_order_id AS order_broker_order_id,
                    o.rejection_reason AS order_rejection_reason
                FROM order_intents oi
                LEFT JOIN orders o ON o.intent_id = oi.id
                WHERE oi.basket_id = ?
                ORDER BY oi.sequence_number
                """,
                (cycle_id,),
            )
            .fetchall()
        )

    def append_cycle_adjustment(
        self,
        *,
        runtime_id: str,
        strategy_id: str,
        execution_mode: ExecutionMode,
        cycle_id: str,
        adjustment_sequence: int,
        trigger_reason: str,
        target_leg_id: str,
        replacement_leg_id: str | None,
        claimed_at: str,
        pre_adjustment_net_delta: float | None,
        post_adjustment_net_delta: float | None,
        realized_pnl: float | None,
        charges: float | None,
        lifecycle_state: str,
    ) -> None:
        """Append-only: one row per adjustment attempt, written once it
        reaches a terminal outcome. ``ON CONFLICT ... DO NOTHING`` on
        ``(cycle_id, adjustment_sequence)`` makes a restart replaying the
        same durable claim idempotent rather than raising."""
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO strategy_cycle_adjustments
                    (runtime_id, strategy_id, execution_mode, cycle_id, adjustment_sequence,
                     trigger_reason, target_leg_id, replacement_leg_id, claimed_at,
                     pre_adjustment_net_delta, post_adjustment_net_delta, realized_pnl,
                     charges, lifecycle_state, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (cycle_id, adjustment_sequence) DO UPDATE SET
                    replacement_leg_id = excluded.replacement_leg_id,
                    post_adjustment_net_delta = excluded.post_adjustment_net_delta,
                    realized_pnl = excluded.realized_pnl,
                    charges = excluded.charges,
                    lifecycle_state = excluded.lifecycle_state
                """,
                (
                    runtime_id,
                    strategy_id,
                    execution_mode.value,
                    cycle_id,
                    adjustment_sequence,
                    trigger_reason,
                    target_leg_id,
                    replacement_leg_id,
                    claimed_at,
                    pre_adjustment_net_delta,
                    post_adjustment_net_delta,
                    realized_pnl,
                    charges,
                    lifecycle_state,
                    _now(),
                ),
            )

    def load_cycle_adjustments(self, *, cycle_id: str) -> list[sqlite3.Row]:
        return list(
            self._db.connect()
            .execute(
                "SELECT * FROM strategy_cycle_adjustments WHERE cycle_id = ? "
                "ORDER BY adjustment_sequence",
                (cycle_id,),
            )
            .fetchall()
        )

    def append_cycle_event(
        self,
        *,
        runtime_id: str,
        strategy_id: str,
        execution_mode: ExecutionMode,
        cycle_id: str,
        event_type: str,
        detail: str | None,
        trading_date: str,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO strategy_cycle_events
                    (runtime_id, strategy_id, execution_mode, cycle_id, event_type, detail,
                     trading_date, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    runtime_id,
                    strategy_id,
                    execution_mode.value,
                    cycle_id,
                    event_type,
                    detail,
                    trading_date,
                    _now(),
                ),
            )

    def load_cycle_events(self, *, cycle_id: str, limit: int = 200) -> list[sqlite3.Row]:
        return list(
            self._db.connect()
            .execute(
                "SELECT * FROM strategy_cycle_events WHERE cycle_id = ? "
                "ORDER BY occurred_at DESC LIMIT ?",
                (cycle_id, limit),
            )
            .fetchall()
        )

    def record_cycle_decision_snapshot(
        self,
        *,
        runtime_id: str,
        strategy_id: str,
        execution_mode: ExecutionMode,
        cycle_id: str,
        decision_type: str,
        leg_role: str | None,
        security_id: str,
        option_type: str | None,
        strike: float | None,
        spot: float | None,
        bid: float | None,
        ask: float | None,
        quote_age_ms: float | None,
        quote_source_timestamp: str | None,
        delta: float | None,
        gamma: float | None,
        theta: float | None,
        vega: float | None,
        implied_volatility: float | None,
        greek_source: str | None,
        greek_source_timestamp: str | None,
        risk_free_rate: float | None,
        dividend_yield: float | None,
        evaluation_timestamp: str,
        time_to_expiry_years: float | None,
    ) -> None:
        """Append-only. One row per candidate leg evaluated for one decision
        (spec section 4: every Greek carries a source timestamp; every
        candidate in one entry/adjustment evaluation shares one snapshot)."""
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO cycle_decision_snapshots
                    (runtime_id, strategy_id, execution_mode, cycle_id, decision_type, leg_role,
                     security_id, option_type, strike, spot, bid, ask, quote_age_ms,
                     quote_source_timestamp, delta, gamma, theta, vega, implied_volatility,
                     greek_source, greek_source_timestamp, risk_free_rate, dividend_yield,
                     evaluation_timestamp, time_to_expiry_years, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?)
                """,
                (
                    runtime_id,
                    strategy_id,
                    execution_mode.value,
                    cycle_id,
                    decision_type,
                    leg_role,
                    security_id,
                    option_type,
                    strike,
                    spot,
                    bid,
                    ask,
                    quote_age_ms,
                    quote_source_timestamp,
                    delta,
                    gamma,
                    theta,
                    vega,
                    implied_volatility,
                    greek_source,
                    greek_source_timestamp,
                    risk_free_rate,
                    dividend_yield,
                    evaluation_timestamp,
                    time_to_expiry_years,
                    _now(),
                ),
            )

    def load_cycle_decision_snapshots(
        self, *, cycle_id: str, limit: int = 200
    ) -> list[sqlite3.Row]:
        return list(
            self._db.connect()
            .execute(
                "SELECT * FROM cycle_decision_snapshots WHERE cycle_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (cycle_id, limit),
            )
            .fetchall()
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
