"""Reserve-before-submit account risk gate (Phase 10, architecture report §4.2).

Two live workers independently checking "is there capacity" and only
*afterward* submitting is a race: both can pass the check before either's
fill ever reaches a ledger. This closes it by making the check and the
reservation one atomic transaction — via ``BEGIN IMMEDIATE``
(:meth:`~common.persistence.database.Database.transaction`) — that
completes *before* the broker is ever called.

The reservation state machine mirrors every state the architecture review
named, with the property that matters most enforced structurally, not just
documented: there is no legal transition out of ``UNKNOWN`` except to
``RECONCILED`` (:data:`_LEGAL_TRANSITIONS`). "Never release a reservation
merely because the immediate correlation lookup returns 'not found'" is
therefore not a discipline callers must remember — it is a transition the
graph itself refuses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from common.persistence.database import Database

#: Every legal transition. A CHECK constraint (the account-shared
#: migration) proves the *vocabulary* is closed; this proves which
#: transitions between those states are legal — a stronger property a
#: column constraint cannot express.
_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "RESERVED": frozenset({"SUBMITTED", "REJECTED", "CANCELLED", "UNKNOWN", "RELEASED"}),
    "SUBMITTED": frozenset(
        {
            "ACKNOWLEDGED",
            "PARTIALLY_FILLED",
            "FILLED",
            "REJECTED",
            "CANCELLED",
            "EXPIRED",
            "UNKNOWN",
        }
    ),
    "ACKNOWLEDGED": frozenset({"PARTIALLY_FILLED", "FILLED", "CANCELLED", "EXPIRED", "UNKNOWN"}),
    "PARTIALLY_FILLED": frozenset(
        {"PARTIALLY_FILLED", "FILLED", "CANCELLED", "EXPIRED", "UNKNOWN"}
    ),
    #: The one rule that matters most: UNKNOWN can only ever be resolved by
    #: the reconciliation runner's own bounded policy — never released,
    #: never silently upgraded to a terminal state from an inline retry.
    "UNKNOWN": frozenset({"RECONCILED"}),
    "RECONCILED": frozenset({"RELEASED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED"}),
    "FILLED": frozenset({"RELEASED"}),
    "REJECTED": frozenset({"RELEASED"}),
    "CANCELLED": frozenset({"RELEASED"}),
    "EXPIRED": frozenset({"RELEASED"}),
    "RELEASED": frozenset(),
}

#: States whose projected_capital still counts as active account exposure —
#: everything except a terminal, released reservation.
ACTIVE_STATES = frozenset(
    {"RESERVED", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "UNKNOWN", "RECONCILED"}
)


class IllegalReservationTransition(RuntimeError):
    """Raised when a caller attempts a transition the state graph refuses."""


@dataclass(frozen=True, slots=True)
class ReservationDecision:
    allowed: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.allowed


class AccountReservationGate:
    """Wraps ``live_risk_reservations`` in the account-shared database."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def check_and_reserve(
        self,
        *,
        account_key: str,
        runtime_id: str,
        strategy_id: str,
        correlation_id: str,
        trading_date: str,
        projected_capital: float,
        projected_legs: int,
        quantity: int,
        max_deployed_capital: float | None,
        max_open_positions: int | None,
        max_open_legs: int | None,
        now: datetime,
    ) -> ReservationDecision:
        """Atomically: sum current active exposure (open reservations plus
        realised open positions), add this order's own projected worst-case
        exposure, and either create the reservation or refuse — all inside
        one ``BEGIN IMMEDIATE`` transaction, so a second process's own
        check-and-reserve cannot interleave with this one (see
        ``Database.transaction``'s docstring for why a plain deferred
        ``BEGIN`` would not give this property).

        Idempotent on ``(account_key, correlation_id)``: a retried call for
        an already-reserved correlation ID hits the table's own UNIQUE
        constraint rather than double-reserving capacity.
        """
        with self._db.transaction(immediate=True) as conn:
            existing = conn.execute(
                "SELECT COALESCE(SUM(projected_capital), 0) AS capital, "
                "COALESCE(SUM(projected_legs), 0) AS legs, COUNT(*) AS n "
                "FROM live_risk_reservations WHERE account_key = ? AND state IN "
                "('RESERVED','SUBMITTED','ACKNOWLEDGED','PARTIALLY_FILLED','UNKNOWN','RECONCILED')",
                (account_key,),
            ).fetchone()
            open_positions = conn.execute(
                "SELECT COALESCE(SUM(deployed_capital), 0) AS capital, COUNT(*) AS n "
                "FROM live_open_positions WHERE account_key = ?",
                (account_key,),
            ).fetchone()

            projected_capital_total = (
                existing["capital"] + open_positions["capital"] + projected_capital
            )
            projected_positions_total = existing["n"] + open_positions["n"] + 1
            projected_legs_total = existing["legs"] + projected_legs

            if max_deployed_capital is not None and projected_capital_total > max_deployed_capital:
                return ReservationDecision(
                    False,
                    f"would exceed max_deployed_capital "
                    f"({projected_capital_total:.2f} > {max_deployed_capital:.2f})",
                )
            if max_open_positions is not None and projected_positions_total > max_open_positions:
                return ReservationDecision(
                    False,
                    f"would exceed max_open_positions "
                    f"({projected_positions_total} > {max_open_positions})",
                )
            if max_open_legs is not None and projected_legs_total > max_open_legs:
                return ReservationDecision(
                    False, f"would exceed max_open_legs ({projected_legs_total} > {max_open_legs})"
                )

            existing_row = conn.execute(
                "SELECT 1 FROM live_risk_reservations WHERE account_key = ? AND correlation_id = ?",
                (account_key, correlation_id),
            ).fetchone()
            if existing_row is not None:
                return ReservationDecision(
                    False, f"a reservation for {correlation_id!r} already exists — not re-reserving"
                )

            conn.execute(
                "INSERT INTO live_risk_reservations (account_key, runtime_id, strategy_id, "
                "correlation_id, trading_date, state, projected_capital, projected_legs, "
                "residual_quantity, created_at, updated_at) VALUES "
                "(?, ?, ?, ?, ?, 'RESERVED', ?, ?, ?, ?, ?)",
                (
                    account_key,
                    runtime_id,
                    strategy_id,
                    correlation_id,
                    trading_date,
                    projected_capital,
                    projected_legs,
                    quantity,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            return ReservationDecision(True)

    def current_state(self, *, account_key: str, correlation_id: str) -> str | None:
        row = self._db.connect().execute(
            "SELECT state FROM live_risk_reservations WHERE account_key = ? AND correlation_id = ?",
            (account_key, correlation_id),
        ).fetchone()
        return str(row["state"]) if row is not None else None

    def transition(
        self,
        *,
        account_key: str,
        correlation_id: str,
        new_state: str,
        now: datetime,
        residual_quantity: int | None = None,
    ) -> None:
        """Move one reservation to ``new_state``. Raises
        :class:`IllegalReservationTransition` if there is no such
        reservation, or if the current state has no legal edge to
        ``new_state`` — including, always, ``UNKNOWN`` to anything but
        ``RECONCILED``.
        """
        with self._db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT state FROM live_risk_reservations WHERE account_key = ? "
                "AND correlation_id = ?",
                (account_key, correlation_id),
            ).fetchone()
            if row is None:
                raise IllegalReservationTransition(
                    f"no reservation found for account_key={account_key!r} "
                    f"correlation_id={correlation_id!r}"
                )
            current = str(row["state"])
            if new_state not in _LEGAL_TRANSITIONS.get(current, frozenset()):
                raise IllegalReservationTransition(
                    f"{correlation_id!r}: {current} -> {new_state} is not a legal transition"
                )
            if residual_quantity is not None:
                conn.execute(
                    "UPDATE live_risk_reservations SET state = ?, residual_quantity = ?, "
                    "updated_at = ? WHERE account_key = ? AND correlation_id = ?",
                    (new_state, residual_quantity, now.isoformat(), account_key, correlation_id),
                )
            else:
                conn.execute(
                    "UPDATE live_risk_reservations SET state = ?, updated_at = ? "
                    "WHERE account_key = ? AND correlation_id = ?",
                    (new_state, now.isoformat(), account_key, correlation_id),
                )
