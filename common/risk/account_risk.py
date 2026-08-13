"""Account-wide live-risk aggregation (spec section 13, account-level
controls) — precisely defined, no double-counting.

Three distinct concepts, kept in three distinct tables (architecture report
§4.5), summed here rather than conflated:

* **Realised P&L** — an idempotent, append-only event ledger
  (``live_realised_pnl_events``). Safe to ``SUM()``.
* **Open-position state** — current-state, upserted, never appended
  (``live_open_positions``).
* **Latest unrealised P&L (MTM)** — overwritten on every mark, never
  appended (``live_position_mtm``). Summing this always reflects the
  *current* picture, never an accumulation of repeated snapshots.

``account_daily_pnl`` = realised (idempotent sum) + unrealised (latest mark
per open position, not accumulated). A position with no mark, or one older
than ``max_mtm_age_seconds``, makes the whole snapshot's ``mtm_stale`` true
— the daily-loss figure cannot be trusted as current, and callers must
treat that as blocking new live entries (spec: "block entries on stale
market data", extended here to the account-level aggregate).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from common.persistence.database import Database

#: Reservation states whose projected_capital still counts as active
#: exposure — mirrors common.risk.account_reservations.ACTIVE_STATES,
#: repeated here (not imported) because this module's SQL needs the literal
#: list, and importing a frozenset into an f-string invites a subtle bug if
#: the two ever drift — a single shared constant would hide that they are
#: the same list re-derived, not a coincidence to catch.
_ACTIVE_RESERVATION_STATES = (
    "RESERVED",
    "SUBMITTED",
    "ACKNOWLEDGED",
    "PARTIALLY_FILLED",
    "UNKNOWN",
    "RECONCILED",
)


@dataclass(frozen=True, slots=True)
class AccountExposureSnapshot:
    account_key: str
    trading_date: str
    realised_pnl: float
    unrealised_pnl: float
    open_position_count: int
    open_positions_capital: float
    reserved_capital: float
    mtm_stale: bool
    stale_security_ids: tuple[str, ...]

    @property
    def daily_pnl(self) -> float:
        return self.realised_pnl + self.unrealised_pnl

    @property
    def total_deployed_capital(self) -> float:
        """Open positions' capital plus everything still reserved (not yet
        filled or not yet released) — the full "what could this account be
        on the hook for right now" figure."""
        return self.open_positions_capital + self.reserved_capital


def account_daily_pnl(
    database: Database,
    *,
    account_key: str,
    trading_date: str,
    now: datetime,
    max_mtm_age_seconds: float | None,
) -> AccountExposureSnapshot:
    return account_daily_pnl_from_connection(
        database.connect(),
        account_key=account_key,
        trading_date=trading_date,
        now=now,
        max_mtm_age_seconds=max_mtm_age_seconds,
    )


def account_daily_pnl_from_connection(
    conn: sqlite3.Connection,
    *,
    account_key: str,
    trading_date: str,
    now: datetime,
    max_mtm_age_seconds: float | None,
) -> AccountExposureSnapshot:
    """Compute the snapshot on a caller-owned connection/transaction.

    The reservation gate uses this form inside the same ``BEGIN IMMEDIATE``
    transaction as its capacity check.  That prevents a second worker from
    changing shared exposure between the daily-loss decision and reservation.
    """

    realised = conn.execute(
        "SELECT COALESCE(SUM(realised_pnl_delta), 0) AS total FROM live_realised_pnl_events "
        "WHERE account_key = ? AND trading_date = ?",
        (account_key, trading_date),
    ).fetchone()["total"]

    positions = conn.execute(
        "SELECT security_id, deployed_capital FROM live_open_positions WHERE account_key = ?",
        (account_key,),
    ).fetchall()

    mtm_rows = conn.execute(
        "SELECT security_id, unrealised_pnl, as_of FROM live_position_mtm WHERE account_key = ?",
        (account_key,),
    ).fetchall()
    mtm_by_security = {row["security_id"]: row for row in mtm_rows}

    unrealised = 0.0
    stale: list[str] = []
    for position in positions:
        mark = mtm_by_security.get(position["security_id"])
        if mark is None:
            stale.append(position["security_id"])
            continue
        as_of = datetime.fromisoformat(mark["as_of"])
        if max_mtm_age_seconds is not None and (now - as_of).total_seconds() > max_mtm_age_seconds:
            stale.append(position["security_id"])
        unrealised += mark["unrealised_pnl"]

    placeholders = ",".join("?" * len(_ACTIVE_RESERVATION_STATES))
    reserved = conn.execute(
        f"SELECT COALESCE(SUM(projected_capital), 0) AS total FROM live_risk_reservations "
        f"WHERE account_key = ? AND state IN ({placeholders})",
        (account_key, *_ACTIVE_RESERVATION_STATES),
    ).fetchone()["total"]

    return AccountExposureSnapshot(
        account_key=account_key,
        trading_date=trading_date,
        realised_pnl=realised,
        unrealised_pnl=unrealised,
        open_position_count=len(positions),
        open_positions_capital=sum(p["deployed_capital"] for p in positions),
        reserved_capital=reserved,
        mtm_stale=bool(stale),
        stale_security_ids=tuple(stale),
    )


@dataclass(frozen=True, slots=True)
class AccountRiskDecision:
    allowed: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.allowed


def check_account_daily_loss(
    snapshot: AccountExposureSnapshot, *, max_daily_loss: float | None
) -> AccountRiskDecision:
    """Stale MTM blocks new entries outright — a daily-loss figure computed
    from a stale mark cannot be trusted either direction."""
    if snapshot.mtm_stale:
        return AccountRiskDecision(
            False,
            f"unrealised P&L is stale for {snapshot.stale_security_ids} — "
            "the daily-loss figure cannot be trusted as current",
        )
    if max_daily_loss is not None and snapshot.daily_pnl <= -max_daily_loss:
        return AccountRiskDecision(
            False,
            f"account daily loss limit hit (realised {snapshot.realised_pnl:.2f} + "
            f"unrealised {snapshot.unrealised_pnl:.2f} = {snapshot.daily_pnl:.2f} "
            f"<= -{max_daily_loss:.2f})",
        )
    return AccountRiskDecision(True)
