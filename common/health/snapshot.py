"""The health snapshot: one read model shared by every downstream consumer.

The Phase 7 audit found two problems this module exists to fix:

* :mod:`dashboards.app` built its own ad-hoc ``SELECT`` statements
  (``_build_tile``) rather than sharing a read model — every future page
  would have repeated that.
* Migration 0002 shipped ``auth_events`` and ``feed_events`` with nothing
  reading them (nothing was writing them either; see
  :mod:`common.execution.health_events` and :mod:`common.feed.reconnect` for
  the writers Phase 7 Part 1 added).

From here on, nobody downstream writes their own SQL against these tables —
the dashboard, ``scripts/status`` and anything else that wants a runtime's
health calls :func:`read_snapshot`.

**Read-only by construction.** Every function here takes an already-open
connection and never opens one of its own, so the caller decides whether that
connection is :func:`common.persistence.connect_readonly` (the dashboard,
scripts/status) or a write connection already open for another reason (a
supervisor diagnosing itself). Nothing here hardcodes a database path, for the
same reason :mod:`dashboards.app` doesn't: the caller owns connection
lifetime.

**Broker health is derived, not polled.** There is no live broker connection
to query from a read-only connection in another process — a dashboard opening
its own broker connection is exactly the side-effecting import the dashboard
forbids (spec: "no runtime imports that create side effects"), and the paper
broker has no connectivity to poll in the first place. So :class:`BrokerHealth`
reports the latest recorded ``errors`` row with ``component='broker'``, which
is what a startup health check (see ``runtimes/intraday_options/worker.py``)
writes when the broker reports itself unhealthy. Absence of such a row is
read as healthy, the same way the rest of this system treats operational data.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessHealth:
    """One session's process/heartbeat picture. ``strategy_id=None`` is the group."""

    strategy_id: str | None
    health_state: str
    heartbeat_age_seconds: float | None
    last_tick_at: str | None
    queue_depth: int | None
    dropped_events: int
    pid: int | None
    started_at: str | None
    ended_at: str | None


@dataclass(frozen=True)
class AuthHealth:
    """The most recent authentication event, if any has ever been recorded."""

    event: str | None
    token_source: str | None
    token_expiry: str | None
    occurred_at: str | None


@dataclass(frozen=True)
class MarketDataHealth:
    """Derived from ``feed_events``. Fields are zeroed, not None, when absent —
    a group whose feed has never reported has definitely not reconnected, which
    is different from "unknown"."""

    last_event: str | None
    last_event_at: str | None
    last_reason: str | None
    reconnect_count: int
    reconnect_exhausted_count: int
    expected_subscriptions: int | None
    active_subscriptions: int | None
    stale_instrument_count: int
    total_downtime_seconds: float

    @property
    def subscriptions_match(self) -> bool:
        if self.expected_subscriptions is None or self.active_subscriptions is None:
            return True  # nothing reported yet is not a mismatch
        return self.expected_subscriptions == self.active_subscriptions


@dataclass(frozen=True)
class BrokerHealth:
    """Derived from the latest ``errors`` row with ``component='broker'``."""

    last_error: str | None
    last_error_at: str | None

    @property
    def healthy(self) -> bool:
        return self.last_error is None


@dataclass(frozen=True)
class DatabaseHealth:
    """Computed directly against the connection handed in — no write needed.

    ``PRAGMA integrity_check`` and ``PRAGMA foreign_key_check`` do not modify
    the database, so both run cleanly on a ``mode=ro`` connection.
    """

    integrity_ok: bool
    integrity_problems: tuple[str, ...]
    foreign_key_violation_count: int
    journal_mode: str
    migration_version: str | None


@dataclass(frozen=True)
class StrategyHealth:
    """One strategy's latest heartbeat and most recent error, if any."""

    strategy_id: str
    health_state: str
    heartbeat_age_seconds: float | None
    execution_mode: str | None
    last_error: str | None


@dataclass(frozen=True)
class HealthSnapshot:
    """Everything one runtime group's dashboard/CLI page needs, in one read."""

    runtime_id: str
    trading_date: str
    generated_at: str
    group: ProcessHealth | None
    strategies: tuple[StrategyHealth, ...]
    auth: AuthHealth
    market_data: MarketDataHealth
    broker: BrokerHealth
    database: DatabaseHealth
    open_positions: int
    realised_pnl_paper: float
    realised_pnl_live: float
    orders_today: int
    recent_errors: tuple[str, ...]


def read_snapshot(
    conn: sqlite3.Connection,
    *,
    runtime_id: str,
    trading_date: str,
    recent_error_limit: int = 5,
) -> HealthSnapshot:
    """Build one snapshot from an already-open connection.

    Every query here is a plain ``SELECT`` or a read-only ``PRAGMA`` — nothing
    in this function can mutate the database, independent of whether the
    connection itself enforces that.
    """
    from datetime import UTC, datetime

    return HealthSnapshot(
        runtime_id=runtime_id,
        trading_date=trading_date,
        generated_at=datetime.now(UTC).isoformat(),
        group=_process_health(conn, runtime_id, strategy_id=None),
        strategies=_strategy_healths(conn, runtime_id),
        auth=_auth_health(conn, runtime_id),
        market_data=_market_data_health(conn, runtime_id),
        broker=_broker_health(conn, runtime_id),
        database=_database_health(conn),
        open_positions=_open_positions(conn, runtime_id, trading_date),
        realised_pnl_paper=_realised_pnl(conn, runtime_id, trading_date, "paper"),
        realised_pnl_live=_realised_pnl(conn, runtime_id, trading_date, "live"),
        orders_today=_orders_today(conn, runtime_id, trading_date),
        recent_errors=_recent_errors(conn, runtime_id, recent_error_limit),
    )


# --------------------------------------------------------------------- process
def _process_health(
    conn: sqlite3.Connection, runtime_id: str, *, strategy_id: str | None
) -> ProcessHealth | None:
    heartbeat_sql = """
        SELECT health_state, beat_at, last_tick_at, queue_depth, dropped_events,
               (julianday('now') - julianday(beat_at)) * 86400.0 AS age_seconds
        FROM runtime_heartbeats
        WHERE runtime_id = ? AND strategy_id IS ?
        ORDER BY id DESC LIMIT 1
    """
    heartbeat = conn.execute(heartbeat_sql, (runtime_id, strategy_id)).fetchone()
    if heartbeat is None:
        return None

    session_sql = """
        SELECT pid, started_at, ended_at
        FROM runtime_sessions
        WHERE runtime_id = ? AND strategy_id IS ?
        ORDER BY id DESC LIMIT 1
    """
    session = conn.execute(session_sql, (runtime_id, strategy_id)).fetchone()

    return ProcessHealth(
        strategy_id=strategy_id,
        health_state=heartbeat["health_state"],
        heartbeat_age_seconds=float(heartbeat["age_seconds"]),
        last_tick_at=heartbeat["last_tick_at"],
        queue_depth=heartbeat["queue_depth"],
        dropped_events=int(heartbeat["dropped_events"] or 0),
        pid=int(session["pid"]) if session else None,
        started_at=session["started_at"] if session else None,
        ended_at=session["ended_at"] if session else None,
    )


def _strategy_healths(conn: sqlite3.Connection, runtime_id: str) -> tuple[StrategyHealth, ...]:
    strategy_ids = conn.execute(
        """
        SELECT DISTINCT strategy_id FROM runtime_heartbeats
        WHERE runtime_id = ? AND strategy_id IS NOT NULL
        ORDER BY strategy_id
        """,
        (runtime_id,),
    ).fetchall()

    results: list[StrategyHealth] = []
    for row in strategy_ids:
        strategy_id = row["strategy_id"]
        heartbeat = conn.execute(
            """
            SELECT health_state,
                   (julianday('now') - julianday(beat_at)) * 86400.0 AS age_seconds
            FROM runtime_heartbeats
            WHERE runtime_id = ? AND strategy_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (runtime_id, strategy_id),
        ).fetchone()
        session = conn.execute(
            """
            SELECT execution_mode FROM runtime_sessions
            WHERE runtime_id = ? AND strategy_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (runtime_id, strategy_id),
        ).fetchone()
        error = conn.execute(
            "SELECT message FROM errors WHERE runtime_id = ? AND strategy_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (runtime_id, strategy_id),
        ).fetchone()
        results.append(
            StrategyHealth(
                strategy_id=strategy_id,
                health_state=heartbeat["health_state"] if heartbeat else "STOPPED",
                heartbeat_age_seconds=(
                    float(heartbeat["age_seconds"]) if heartbeat is not None else None
                ),
                execution_mode=session["execution_mode"] if session else None,
                last_error=error["message"] if error else None,
            )
        )
    return tuple(results)


# ---------------------------------------------------------------------- auth
def _auth_health(conn: sqlite3.Connection, runtime_id: str) -> AuthHealth:
    row = conn.execute(
        """
        SELECT event, token_source, token_expiry, occurred_at
        FROM auth_events WHERE runtime_id = ? ORDER BY id DESC LIMIT 1
        """,
        (runtime_id,),
    ).fetchone()
    if row is None:
        return AuthHealth(event=None, token_source=None, token_expiry=None, occurred_at=None)
    return AuthHealth(
        event=row["event"],
        token_source=row["token_source"],
        token_expiry=row["token_expiry"],
        occurred_at=row["occurred_at"],
    )


# ---------------------------------------------------------------- market data
def _market_data_health(conn: sqlite3.Connection, runtime_id: str) -> MarketDataHealth:
    latest = conn.execute(
        """
        SELECT event, reason, occurred_at
        FROM feed_events WHERE runtime_id = ? ORDER BY id DESC LIMIT 1
        """,
        (runtime_id,),
    ).fetchone()
    # Subscription counts come from the latest row that actually carries
    # them, not the literal latest row: a subsequent `stale_instrument` or
    # `degraded` event (neither reports subscriptions) must not blank out the
    # last real `resubscribed`/`connected` reading.
    latest_subscriptions = conn.execute(
        """
        SELECT expected_subscriptions, active_subscriptions
        FROM feed_events
        WHERE runtime_id = ? AND expected_subscriptions IS NOT NULL
        ORDER BY id DESC LIMIT 1
        """,
        (runtime_id,),
    ).fetchone()
    counts = conn.execute(
        """
        SELECT
            SUM(CASE WHEN event = 'reconnect_attempted' THEN 1 ELSE 0 END) AS reconnects,
            SUM(CASE WHEN event = 'reconnect_exhausted' THEN 1 ELSE 0 END) AS exhausted,
            SUM(CASE WHEN event = 'stale_instrument' THEN 1 ELSE 0 END) AS stale,
            SUM(COALESCE(downtime_seconds, 0.0)) AS downtime
        FROM feed_events WHERE runtime_id = ?
        """,
        (runtime_id,),
    ).fetchone()

    return MarketDataHealth(
        last_event=latest["event"] if latest else None,
        last_event_at=latest["occurred_at"] if latest else None,
        last_reason=latest["reason"] if latest else None,
        reconnect_count=int(counts["reconnects"] or 0),
        reconnect_exhausted_count=int(counts["exhausted"] or 0),
        expected_subscriptions=(
            int(latest_subscriptions["expected_subscriptions"])
            if latest_subscriptions is not None
            else None
        ),
        active_subscriptions=(
            int(latest_subscriptions["active_subscriptions"])
            if latest_subscriptions is not None
            else None
        ),
        stale_instrument_count=int(counts["stale"] or 0),
        total_downtime_seconds=float(counts["downtime"] or 0.0),
    )


# -------------------------------------------------------------------- broker
def _broker_health(conn: sqlite3.Connection, runtime_id: str) -> BrokerHealth:
    row = conn.execute(
        """
        SELECT message, occurred_at FROM errors
        WHERE runtime_id = ? AND component = 'broker'
        ORDER BY id DESC LIMIT 1
        """,
        (runtime_id,),
    ).fetchone()
    if row is None:
        return BrokerHealth(last_error=None, last_error_at=None)
    return BrokerHealth(last_error=row["message"], last_error_at=row["occurred_at"])


# ------------------------------------------------------------------ database
def _database_health(conn: sqlite3.Connection) -> DatabaseHealth:
    integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
    problems = tuple(row[0] for row in integrity_rows if row[0] != "ok")
    fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    version_row = conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return DatabaseHealth(
        integrity_ok=not problems,
        integrity_problems=problems,
        foreign_key_violation_count=len(fk_violations),
        journal_mode=journal_mode,
        migration_version=version_row[0] if version_row else None,
    )


# ---------------------------------------------------------------- positions
def _open_positions(conn: sqlite3.Connection, runtime_id: str, trading_date: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM positions
        WHERE runtime_id = ? AND trading_date = ? AND status = 'OPEN' AND quantity != 0
        """,
        (runtime_id, trading_date),
    ).fetchone()
    return int(row["c"]) if row else 0


def _realised_pnl(
    conn: sqlite3.Connection, runtime_id: str, trading_date: str, execution_mode: str
) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(realised_pnl), 0.0) AS total FROM positions
        WHERE runtime_id = ? AND trading_date = ? AND execution_mode = ?
        """,
        (runtime_id, trading_date, execution_mode),
    ).fetchone()
    return float(row["total"]) if row else 0.0


def _orders_today(conn: sqlite3.Connection, runtime_id: str, trading_date: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM orders o
        JOIN order_intents i ON i.id = o.intent_id
        WHERE o.runtime_id = ? AND i.trading_date = ?
        """,
        (runtime_id, trading_date),
    ).fetchone()
    return int(row["c"]) if row else 0


def _recent_errors(conn: sqlite3.Connection, runtime_id: str, limit: int) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT message FROM errors WHERE runtime_id = ? ORDER BY id DESC LIMIT ?",
        (runtime_id, limit),
    ).fetchall()
    return tuple(row["message"] for row in rows)
