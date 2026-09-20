"""Read-only database access shared by every dashboard page.

Centralises two things: what a page shows when its database is missing,
locked, or has pending migrations — a message, never a traceback — and what
"today" means to a dashboard (:func:`trading_date_today`). Every page module
in this package imports only this module, :mod:`common.health.snapshot`,
:func:`common.persistence.connect_readonly` and :mod:`common.utils.timeutils`.
No page imports a broker, a feed, or a write connection — enforced by
``tests/unit/test_dashboard.py``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TypeVar

from common.health import HealthSnapshot, read_snapshot
from common.persistence import DatabaseError, connect_readonly
from common.utils import timeutils

T = TypeVar("T")


def trading_date_today(tz_name: str = timeutils.DEFAULT_TZ) -> date:
    """Today's trading date in the exchange's timezone — never the host's.

    Every page scopes its queries to a trading date, and until D88 three of
    them took it from a naive ``date.today()``. That is the machine's local
    date, which is the right answer only on a machine set to IST. On a UTC
    host — a cloud VPS, which is where the SEBI static-IP requirement points
    this project — the two disagree for the first 5.5 hours of every calendar
    day, so from 00:00 to 05:30 IST every page quietly showed the *previous*
    trading day: yesterday's positions, yesterday's P&L, yesterday's
    incidents, with nothing on screen to say so. The same bug class as
    ``8bd41ae`` (the UTC-vs-IST entry gate).

    No new time helper is introduced: this is
    :func:`common.utils.timeutils.now_tz` plus
    :func:`common.utils.timeutils.local_date_in`, which is exactly the pair
    ``dashboards/positional_options.py`` had already been using on its own
    and which now lives in one place for the whole package. They are reached
    through the module rather than imported by name so a test can pin the
    clock at this single seam — see
    ``tests/unit/test_dashboard_trading_date.py``.

    ``tz_name`` defaults to :data:`common.utils.timeutils.DEFAULT_TZ`, which
    ``config/global.yaml``'s ``global.timezone`` matches (asserted by that
    same test module). Deliberately not read from YAML here: a dashboard's
    contract is to degrade to a message rather than raise, and a
    ``ConfigError`` reaching a date computation on five pages would buy
    nothing while the two values agree. The parameter is the one place a
    future non-IST configuration would be wired in.
    """
    return timeutils.local_date_in(timeutils.now_tz(tz_name), tz_name)


@dataclass(frozen=True)
class SnapshotUnavailable:
    """Why a page has nothing to show. Rendered as a message, not raised."""

    reason: str


def run_bounded(
    database_path: Path | str, fn: Callable[[sqlite3.Connection], T]
) -> T | SnapshotUnavailable:
    """Open a bounded read-only connection, run ``fn``, always close it.

    The same three failure modes :func:`load_snapshot` handles (missing,
    locked, pre-migration/corrupt), generalised for every read-model query
    added since — one place, so a new query function in ``dashboards/data/``
    never has to repeat this try/except by hand. ``fn`` must not close the
    connection itself; this function owns its lifetime end to end.
    """
    db_path = Path(database_path)
    if not db_path.is_file():
        return SnapshotUnavailable(f"No database yet at {db_path}. Start the supervisor first.")
    try:
        conn = connect_readonly(db_path)
    except DatabaseError as exc:
        return SnapshotUnavailable(str(exc))
    try:
        return fn(conn)
    except sqlite3.Error as exc:
        return SnapshotUnavailable(f"Database not ready ({type(exc).__name__}): {exc}")
    finally:
        conn.close()


def load_snapshot(
    database_path: Path | str, runtime_id: str, trading_date: str
) -> HealthSnapshot | SnapshotUnavailable:
    """Read one runtime's health snapshot, or explain why it could not be read.

    Three failure modes an operator will actually hit, all handled here so no
    page has to:

    * **No database yet** — the runtime has never started. Checked before
      ``connect_readonly`` is even called, since that function raises for
      exactly this case and a pre-check gives a clearer message than
      catching its exception would.
    * **Locked** — a writer is mid-transaction past the read connection's
      busy timeout. Surfaces as ``sqlite3.OperationalError``.
    * **Pre-migration or corrupt** — the file exists but a table
      :func:`~common.health.snapshot.read_snapshot` queries does not (an old
      schema, or a database this project never wrote), or the file is not a
      valid SQLite database at all. Both surface as a ``sqlite3.Error``
      subclass the moment a query runs, not at connection time — SQLite
      opens lazily.
    """
    db_path = Path(database_path)
    if not db_path.is_file():
        return SnapshotUnavailable(f"No database yet at {db_path}. Start the supervisor first.")

    try:
        conn = connect_readonly(db_path)
    except DatabaseError as exc:
        return SnapshotUnavailable(str(exc))

    try:
        return read_snapshot(conn, runtime_id=runtime_id, trading_date=trading_date)
    except sqlite3.Error as exc:
        return SnapshotUnavailable(f"Database not ready ({type(exc).__name__}): {exc}")
    finally:
        conn.close()
