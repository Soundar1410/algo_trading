"""Account-wide daily-P&L formula: idempotent realised sum plus latest
(never accumulated) unrealised marks, staleness blocking, paper exclusion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from common.persistence import migrate_account_shared_database, open_account_shared_database
from common.risk import account_daily_pnl, check_account_daily_loss

NOW = datetime(2026, 8, 13, 9, 15, tzinfo=UTC)


def _database(tmp_path: Path):
    database = open_account_shared_database(tmp_path / "dhan_account_shared.db")
    migrate_account_shared_database(database)
    return database


def _insert_realised(db, *, key="bf_001", pnl=500.0, trading_date="2026-08-13"):
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO live_realised_pnl_events (account_key, runtime_id, "
            "strategy_id, trading_date, idempotency_key, realised_pnl_delta, recorded_at) "
            "VALUES ('acct1', 'intraday_options', 'st01', ?, ?, ?, ?)",
            (trading_date, key, pnl, NOW.isoformat()),
        )


def _insert_position(db, *, security_id="sec1", capital=40000.0):
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO live_open_positions (account_key, runtime_id, strategy_id, "
            "security_id, quantity, average_price, deployed_capital, opened_at, updated_at) "
            "VALUES ('acct1', 'intraday_options', 'st01', ?, 75, 190.0, ?, ?, ?)",
            (security_id, capital, NOW.isoformat(), NOW.isoformat()),
        )


def _insert_mtm(db, *, security_id="sec1", pnl=100.0, as_of=None):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO live_position_mtm (account_key, runtime_id, strategy_id, security_id, "
            "unrealised_pnl, as_of) VALUES ('acct1', 'intraday_options', 'st01', ?, ?, ?) "
            "ON CONFLICT (account_key, runtime_id, strategy_id, security_id) "
            "DO UPDATE SET unrealised_pnl = excluded.unrealised_pnl, as_of = excluded.as_of",
            (security_id, pnl, (as_of or NOW).isoformat()),
        )


# ------------------------------------------------------------------- realised
def test_realised_pnl_sums_idempotently(tmp_path: Path):
    db = _database(tmp_path)
    _insert_realised(db, key="bf_001", pnl=500.0)
    _insert_realised(db, key="bf_002", pnl=-200.0)
    _insert_realised(db, key="bf_001", pnl=500.0)  # replay, same key — must not double count

    snapshot = account_daily_pnl(
        db, account_key="acct1", trading_date="2026-08-13", now=NOW, max_mtm_age_seconds=None
    )
    assert snapshot.realised_pnl == 300.0


def test_realised_pnl_is_scoped_to_trading_date(tmp_path: Path):
    db = _database(tmp_path)
    _insert_realised(db, key="bf_yesterday", pnl=1000.0, trading_date="2026-08-12")
    _insert_realised(db, key="bf_today", pnl=200.0, trading_date="2026-08-13")

    snapshot = account_daily_pnl(
        db, account_key="acct1", trading_date="2026-08-13", now=NOW, max_mtm_age_seconds=None
    )
    assert snapshot.realised_pnl == 200.0


# ------------------------------------------------------------------ unrealised
def test_repeated_mtm_marks_do_not_double_count(tmp_path: Path):
    """The double-counting fix, at the aggregation layer: three marks for
    the same position must contribute only the LATEST value."""
    db = _database(tmp_path)
    _insert_position(db)
    _insert_mtm(db, pnl=100.0)
    _insert_mtm(db, pnl=150.0)
    _insert_mtm(db, pnl=90.0)

    snapshot = account_daily_pnl(
        db, account_key="acct1", trading_date="2026-08-13", now=NOW, max_mtm_age_seconds=None
    )
    assert snapshot.unrealised_pnl == 90.0


def test_unrealised_pnl_sums_across_positions(tmp_path: Path):
    db = _database(tmp_path)
    _insert_position(db, security_id="sec1", capital=40000.0)
    _insert_position(db, security_id="sec2", capital=20000.0)
    _insert_mtm(db, security_id="sec1", pnl=100.0)
    _insert_mtm(db, security_id="sec2", pnl=-50.0)

    snapshot = account_daily_pnl(
        db, account_key="acct1", trading_date="2026-08-13", now=NOW, max_mtm_age_seconds=None
    )
    assert snapshot.unrealised_pnl == 50.0
    assert snapshot.open_position_count == 2
    assert snapshot.open_positions_capital == 60000.0


# --------------------------------------------------------------- staleness
def test_a_position_with_no_mtm_mark_at_all_is_stale(tmp_path: Path):
    db = _database(tmp_path)
    _insert_position(db)

    snapshot = account_daily_pnl(
        db, account_key="acct1", trading_date="2026-08-13", now=NOW, max_mtm_age_seconds=60
    )
    assert snapshot.mtm_stale
    assert "sec1" in snapshot.stale_security_ids


def test_a_stale_mtm_mark_beyond_the_age_bound_is_flagged(tmp_path: Path):
    db = _database(tmp_path)
    _insert_position(db)
    _insert_mtm(db, pnl=100.0, as_of=NOW - timedelta(seconds=120))

    snapshot = account_daily_pnl(
        db, account_key="acct1", trading_date="2026-08-13", now=NOW, max_mtm_age_seconds=60
    )
    assert snapshot.mtm_stale


def test_a_fresh_mtm_mark_within_the_age_bound_is_not_stale(tmp_path: Path):
    db = _database(tmp_path)
    _insert_position(db)
    _insert_mtm(db, pnl=100.0, as_of=NOW - timedelta(seconds=10))

    snapshot = account_daily_pnl(
        db, account_key="acct1", trading_date="2026-08-13", now=NOW, max_mtm_age_seconds=60
    )
    assert not snapshot.mtm_stale


def test_no_age_bound_configured_never_flags_staleness(tmp_path: Path):
    db = _database(tmp_path)
    _insert_position(db)
    _insert_mtm(db, pnl=100.0, as_of=NOW - timedelta(days=5))

    snapshot = account_daily_pnl(
        db, account_key="acct1", trading_date="2026-08-13", now=NOW, max_mtm_age_seconds=None
    )
    assert not snapshot.mtm_stale


# ----------------------------------------------------------------- exclusion
def test_paper_rows_never_contribute_to_live_account_risk(tmp_path: Path):
    """Structural: the tables this module reads (live_realised_pnl_events,
    live_open_positions, live_position_mtm) exist only in the account-shared
    database, which no paper code path ever writes to — there is no
    execution_mode column to filter here because paper data cannot appear
    in these tables at all."""
    db = _database(tmp_path)
    _insert_realised(db, pnl=500.0)
    _insert_position(db)
    _insert_mtm(db, pnl=100.0)

    with db.connect() as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(live_realised_pnl_events)")
        }
    assert "execution_mode" not in columns  # structurally live-only, nothing to filter


# ------------------------------------------------------------------ decision
def test_check_account_daily_loss_passes_within_limit(tmp_path: Path):
    db = _database(tmp_path)
    _insert_realised(db, pnl=-1000.0)
    snapshot = account_daily_pnl(
        db, account_key="acct1", trading_date="2026-08-13", now=NOW, max_mtm_age_seconds=None
    )
    decision = check_account_daily_loss(snapshot, max_daily_loss=5000.0)
    assert decision.allowed


def test_check_account_daily_loss_blocks_when_the_limit_is_hit(tmp_path: Path):
    db = _database(tmp_path)
    _insert_realised(db, pnl=-6000.0)
    snapshot = account_daily_pnl(
        db, account_key="acct1", trading_date="2026-08-13", now=NOW, max_mtm_age_seconds=None
    )
    decision = check_account_daily_loss(snapshot, max_daily_loss=5000.0)
    assert not decision.allowed
    assert "daily loss" in decision.reason


def test_check_account_daily_loss_blocks_on_stale_mtm_regardless_of_pnl(tmp_path: Path):
    db = _database(tmp_path)
    _insert_position(db)  # no mark at all -> stale
    snapshot = account_daily_pnl(
        db, account_key="acct1", trading_date="2026-08-13", now=NOW, max_mtm_age_seconds=60
    )
    decision = check_account_daily_loss(snapshot, max_daily_loss=5000.0)
    assert not decision.allowed
    assert "stale" in decision.reason


def test_check_account_daily_loss_with_no_limit_configured_never_blocks_on_pnl(tmp_path: Path):
    db = _database(tmp_path)
    _insert_realised(db, pnl=-1_000_000.0)
    snapshot = account_daily_pnl(
        db, account_key="acct1", trading_date="2026-08-13", now=NOW, max_mtm_age_seconds=None
    )
    decision = check_account_daily_loss(snapshot, max_daily_loss=None)
    assert decision.allowed
