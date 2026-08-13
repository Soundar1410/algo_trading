"""SQLite connection behaviour: pragmas, transactions, read-only enforcement."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from common.persistence import Database, DatabaseError, connect_readonly


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "operational" / "intraday_options.db")
    database.connect()
    return database


def test_wal_mode_is_enabled(db: Database):
    """WAL is what lets the dashboard read while a worker writes."""
    assert db.journal_mode() == "wal"


def test_foreign_keys_are_enforced(db: Database):
    """SQLite defaults foreign keys OFF; orphaned fills are undetectable later."""
    conn = db.connect()
    conn.executescript(
        """
        CREATE TABLE orders (id INTEGER PRIMARY KEY);
        CREATE TABLE fills (
            id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL REFERENCES orders(id)
        );
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO fills (id, order_id) VALUES (1, 999)")


def test_parent_directory_is_created(tmp_path: Path):
    database = Database(tmp_path / "nested" / "deeper" / "group.db")
    database.connect()
    assert database.path.is_file()


def test_transaction_commits_on_success(db: Database):
    conn = db.connect()
    conn.execute("CREATE TABLE t (v INTEGER)")
    with db.transaction() as tx:
        tx.execute("INSERT INTO t (v) VALUES (1)")
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_transaction_rolls_back_on_exception(db: Database):
    conn = db.connect()
    conn.execute("CREATE TABLE t (v INTEGER)")
    with pytest.raises(RuntimeError), db.transaction() as tx:
        tx.execute("INSERT INTO t (v) VALUES (1)")
        raise RuntimeError("strategy blew up mid-write")
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_integrity_and_foreign_key_checks_pass_on_a_healthy_database(db: Database):
    assert db.integrity_check() == []


# ------------------------------------------------------- Phase 10: immediate
def test_immediate_transaction_commits_on_success(db: Database):
    conn = db.connect()
    conn.execute("CREATE TABLE t (v INTEGER)")
    with db.transaction(immediate=True) as tx:
        tx.execute("INSERT INTO t (v) VALUES (1)")
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1


def test_immediate_transaction_rolls_back_on_exception(db: Database):
    conn = db.connect()
    conn.execute("CREATE TABLE t (v INTEGER)")
    with pytest.raises(RuntimeError), db.transaction(immediate=True) as tx:
        tx.execute("INSERT INTO t (v) VALUES (1)")
        raise RuntimeError("strategy blew up mid-write")
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_a_second_immediate_transaction_blocks_until_the_first_commits(tmp_path: Path):
    """The actual property this exists for: a concurrent writer cannot even
    start reading inside its own BEGIN IMMEDIATE until the first one
    finishes — the deferred default does not give this.

    Each side opens its *own* ``Database``/connection inside its own
    thread — sqlite3 connections are not shareable across threads, and this
    mirrors what two real OS processes (the actual scenario this property
    protects) would each do anyway.
    """
    import threading
    import time

    path = tmp_path / "operational" / "intraday_options.db"
    Database(path).connect().execute("CREATE TABLE t (v INTEGER)")

    order: list[str] = []

    def hold_first_transaction():
        first = Database(path)
        with first.transaction(immediate=True) as tx:
            order.append("first_started")
            tx.execute("INSERT INTO t (v) VALUES (1)")
            time.sleep(0.2)
            order.append("first_finishing")

    thread = threading.Thread(target=hold_first_transaction)
    thread.start()
    time.sleep(0.05)  # let the first transaction actually acquire the lock

    second = Database(path, busy_timeout_ms=2_000)
    with second.transaction(immediate=True) as tx:
        order.append("second_started")
        tx.execute("INSERT INTO t (v) VALUES (2)")

    thread.join()

    assert order.index("first_finishing") < order.index("second_started"), (
        f"the second transaction must not start until the first commits: {order}"
    )


def test_close_allows_reconnect(db: Database):
    db.close()
    assert db.connect().execute("SELECT 1").fetchone()[0] == 1


# ------------------------------------------------------------- read-only
def test_readonly_connection_can_read(db: Database):
    conn = db.connect()
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.execute("INSERT INTO t (v) VALUES (42)")

    with connect_readonly(db.path) as ro:
        assert ro.execute("SELECT v FROM t").fetchone()[0] == 42


def test_readonly_connection_refuses_writes(db: Database):
    """The dashboard must be unable to mutate operational state, by construction."""
    db.connect().execute("CREATE TABLE t (v INTEGER)")
    ro = connect_readonly(db.path)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO t (v) VALUES (1)")
    ro.close()


def test_readonly_connection_cannot_create_tables(db: Database):
    ro = connect_readonly(db.path)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("CREATE TABLE sneaky (v INTEGER)")
    ro.close()


def test_readonly_open_of_missing_file_fails_clearly(tmp_path: Path):
    """Without this, SQLite would silently create an empty database."""
    with pytest.raises(DatabaseError, match="does not exist"):
        connect_readonly(tmp_path / "never_created.db")
