"""Phase 4b-2 Part A: the audit round 5 findings of Phase 4b-1, each with its
repro.

* R5-1: a held symbol stale this week, with an in-week gap, is marked at its
  last daily close over the freeze factor of that same session — never last
  week's close over this week's factor (a false peak).
* R5-2: a catch-up that stops part-way still reports, journals and
  telegrams every week it committed.
* R5-3: a silent freeze lift is explained only by acknowledging a gap that
  was part of the previous run's freeze.
* R5-4 to R5-7, the cosmetic items and the first-run guard.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from _stock_run_fixtures import Root, falling, week, wobble
from _wsr1_rules_fixtures import friday
from test_stock_accounting import _repo
from test_stock_unadjusted_gaps import _ack, _run, _through, _world, monday

from common.notifications.base import RecordingNotifier
from runtimes.positional_stocks import backup, journal, weekly_run
from runtimes.positional_stocks.weekly_run import (
    EXIT_DEADLINE,
    EXIT_NO_TRADES,
    EXIT_OK,
    EXIT_REFUSED,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.gaps import scan
from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import shift, week_of
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    CorporateActionKind,
    CorporateActionRow,
    money,
)

D = Decimal


# ==================================================================== R5-3
def test_r5_3_an_older_acknowledged_gap_does_not_explain_a_lift(tmp_path: Path) -> None:
    """The audit's L4: a real +18.8% jump at 211's Monday, acknowledged; then
    a 1:5 bonus ex 214 (-16.7%) freezes the position; from run 216 Dhan
    restates only back to 212's Monday, leaving a -14.2% boundary gap, so the
    freeze lifts. The old acknowledgement is of a gap that was never part of
    the freeze: the lift is silent and must be flagged."""
    world = _world(
        {
            210: 1010.0,
            211: 1200.0,
            212: 1235.0,
            213: 1235.0,
            **{w: 1235.0 / 1.2 for w in range(214, 222)},
        },
        restatements=((216, monday(212), monday(214), 1 / 1.2),),
    )
    (jump,) = [g for g in scan("A", world.daily(211)) if g.session == monday(211)]
    world.extra_acks.append(_ack(monday(211), str(jump.ratio)))
    repo = _repo(tmp_path / "positional_stocks.db")
    outcomes = _through(repo, world, 215)
    assert [len(o.frozen) for o in outcomes[-2:]] == [1, 1]
    assert outcomes[-3].frozen == ()  # the acknowledged jump froze nothing
    lifted = _run(repo, world, 216)
    assert lifted.frozen == () and lifted.rescaled == ()
    (lift,) = lifted.silent_lifts
    assert lift.symbol == "A"


def test_r5_3_the_frozen_gaps_are_stored_with_the_review(tmp_path: Path) -> None:
    """R1's raw 1:1 bonus freezes the position at run 212 on the gap
    (212's Monday, 0.5000); run 213 reads exactly that key back."""
    world = _world({210: 1010.0, 211: 1010.0, **{w: 505.0 for w in range(212, 220)}})
    repo = _repo(tmp_path / "positional_stocks.db")
    _through(repo, world, 212)
    (position,) = repo.positions().values()
    assert repo.previous_frozen_gaps(position.position_id, week_of(friday(212))) == set()
    after = shift(week_of(friday(212)), 1)
    assert repo.previous_frozen_gaps(position.position_id, after) == {(monday(212), D("0.5000"))}


# ==================================================================== R5-1
def _stale_gap_root(tmp_path: Path) -> tuple[Root, date]:
    """A held from W1; in W3 it gaps -35% on Thursday and has no Friday bar,
    so W3 is stale for it and its weekly series is cut to W2."""
    root = Root.create(tmp_path)
    thursday = root.last_session(3) - timedelta(days=1)

    def a(day: date) -> tuple[float, float]:
        return (650.0, 650.0) if day >= thursday else (1000.0, 1000.0)

    root.standard_cache()
    root.write("A", a, skip={root.last_session(3)})
    root.seed_entry()
    return root, thursday


def test_r5_1_a_stale_held_symbol_is_marked_where_its_freeze_is_measured(
    tmp_path: Path,
) -> None:
    root, _ = _stale_gap_root(tmp_path)
    for n in (1, 2, 3):
        assert root.run("--as-of", root.as_of(n)) == EXIT_OK
    repo = root.repo()
    (position,) = repo.positions().values()
    rows = {
        r["iso_week"]: r
        for r in repo.database.connect().execute("SELECT * FROM stock_equity").fetchall()
    }
    w2, w3 = rows["2026-W24"], rows["2026-W25"]
    (freeze_line,) = [
        line
        for line in root.report(3).splitlines()
        if line.startswith("- A (") and "factor" in line
    ]
    assert "factor 0.6500" in freeze_line
    # The true value: Thursday's close of 650 over the 0.65 of that same gap,
    # 1,000 a share in the position's own units.
    true_mark = money(Decimal("650") / Decimal("0.65"))
    assert true_mark == Decimal("1000.00")
    assert Decimal(w3["positions_value"]) == true_mark * position.shares_held
    assert Decimal(w3["equity"]) == Decimal(w3["cash"]) + true_mark * position.shares_held
    # The audit's figures: 999,952.00, not last week's 1,000 over this week's
    # 0.65 (1,021,490.40), and that false peak is never saved.
    assert Decimal(w3["equity"]) == Decimal("999952.00")
    assert Decimal(w3["peak"]) == Decimal(w2["peak"])


# ==================================================================== R5-2
def _catch_up_root(tmp_path: Path) -> Root:
    """The 4b-1 fixture: A seeded, falling from week 2; weeks 1-3 decided."""
    root = Root.create(tmp_path)
    root.standard_cache(a=falling(root.first_session(2)))
    root.seed_entry()
    for n in (1, 2, 3):
        assert root.run("--as-of", root.as_of(n)) == EXIT_OK
    return root


def _assert_w26_is_reported(root: Root) -> None:
    """The audit's repro: the W26 stop exit (SELL_ALL 40 @ 664.83, net
    -13,499.05) was committed, then the run stopped at W27."""
    report = root.report(4)
    assert "**A** (A-2026W23) closed" in report and "@ 664.83 x 40" in report
    assert "net -13,499.05" in report
    assert "## Run stopped" in report and "2026-W27" in report.split("## Run stopped")[1]
    journal = (root.reports / "journal.csv").read_text().splitlines()
    assert len(journal) == 2 and journal[1].startswith("A-2026W23,A,")
    assert ",664.83,40,stop,-13499.05," in journal[1]
    assert isinstance(root.notifier, RecordingNotifier)
    (event,) = root.notifier.events[-1:]
    assert event.event_type == "weekly_summary"
    assert "Exits: A stop -Rs 13,499.05" in event.message
    assert "STOPPED: 2026-W27" in event.message
    assert "re-run" in (event.required_action or "")
    assert (root.reports / f"{root.last_session(5)}.md").is_file()  # the failed week's own


def test_r5_2_a_catch_up_stopped_by_a_cold_cache_reports_its_committed_weeks(
    tmp_path: Path,
) -> None:
    root = _catch_up_root(tmp_path)
    root.write("NIFTY", wobble(20000.0), skip={root.last_session(5)})
    before = len(root.notifier.events)  # type: ignore[attr-defined]
    assert root.run("--as-of", root.as_of(6)) == EXIT_NO_TRADES
    assert len(root.notifier.events) == before + 1  # type: ignore[attr-defined]
    _assert_w26_is_reported(root)
    assert root.repo().run_status(root.last_session(4)) == "COMPLETED"
    assert root.repo().run_status(root.last_session(5)) is None


def test_r5_2_a_catch_up_stopped_by_the_deadline_reports_its_committed_weeks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _catch_up_root(tmp_path)
    clock = [0.0]
    real = weekly_run.run_decision_week

    def decide_then_expire(*args: object, **kwargs: object) -> object:
        outcome = real(*args, **kwargs)  # type: ignore[arg-type]
        clock[0] = 1e6  # the budget is gone after week 4 commits
        return outcome

    monkeypatch.setattr(weekly_run, "run_decision_week", decide_then_expire)
    assert root.run("--as-of", root.as_of(6), monotonic=lambda: clock[0]) == EXIT_DEADLINE
    _assert_w26_is_reported(root)
    assert "deadline exceeded" in root.report(4)


# ==================================================================== R5-4
def test_r5_4_a_cold_cache_or_deadline_attempt_takes_no_snapshot(tmp_path: Path) -> None:
    root = _catch_up_root(tmp_path)
    snapshots = sorted(root.backups.glob("*.db"))
    assert len(snapshots) == 3
    root.write("NIFTY", wobble(20000.0), skip={root.last_session(4)})
    assert root.run("--as-of", root.as_of(4)) == EXIT_NO_TRADES
    ticks = iter([0.0, 1e6, 1e6, 1e6, 1e6])
    assert root.run("--as-of", root.as_of(4), monotonic=lambda: next(ticks)) == EXIT_DEADLINE
    assert sorted(root.backups.glob("*.db")) == snapshots


# ==================================================================== R5-5
def test_r5_5_an_unusable_backup_directory_refuses_cleanly(tmp_path: Path) -> None:
    root = _catch_up_root(tmp_path)
    shutil.rmtree(root.backups)
    root.backups.write_text("not a directory")
    before = _sha(root.db)
    assert root.run("--as-of", root.as_of(4)) == EXIT_REFUSED
    assert any(line.startswith("REFUSED: snapshot") for line in root.output)
    assert _sha(root.db) == before
    report = root.report(4)
    assert "NO TRADES (backup failed)" in report and "REFUSED" in report
    assert root.notifier.events[-1].event_type == "weekly_run_no_trades"  # type: ignore[attr-defined]


def test_r5_5_a_full_disk_leaves_no_partial_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _catch_up_root(tmp_path)
    snapshots = sorted(root.backups.iterdir())
    real_copy = backup._copy

    def disk_full(source: Path, dest: Path) -> None:
        dest.write_bytes(b"")  # the file exists, as it would mid-copy
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(backup, "_copy", disk_full)
    before = _sha(root.db)
    assert root.run("--as-of", root.as_of(4)) == EXIT_REFUSED
    assert sorted(root.backups.iterdir()) == snapshots  # no 0-byte file left
    assert _sha(root.db) == before
    assert "database or disk is full" in root.report(4)
    monkeypatch.setattr(backup, "_copy", real_copy)
    assert root.run("--as-of", root.as_of(4)) == EXIT_OK


# ==================================================================== R5-6
def test_r5_6_snapshots_leave_no_wal_or_shm_and_prune_removes_siblings(tmp_path: Path) -> None:
    root = _catch_up_root(tmp_path)
    names = sorted(p.name for p in root.backups.iterdir())
    assert names and all(n.endswith(".db") for n in names)
    for snap in root.backups.glob("*.db"):
        backup._verify(snap)  # read-only opens of a snapshot create nothing
        # Nor does a plain read-only open, as the operator's sqlite3 would do:
        # the snapshot is in rollback-journal mode, not WAL.
        conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        conn.execute("SELECT count(*) FROM stock_weekly_runs").fetchone()
        conn.close()
    assert sorted(p.name for p in root.backups.iterdir()) == names
    oldest = root.backups / names[0]
    for suffix in ("-wal", "-shm"):
        oldest.with_name(oldest.name + suffix).write_bytes(b"x")
    backup.prune("positional_stocks", root.backups, keep=2)
    assert sorted(p.name for p in root.backups.iterdir()) == names[1:]


def test_r5_6_the_source_is_read_immutable_only_without_a_live_wal(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    sqlite3.connect(db).close()
    assert backup.read_only_uri(db).endswith("&immutable=1")
    db.with_name("x.db-wal").write_bytes(b"")
    assert backup.read_only_uri(db).endswith("&immutable=1")  # empty: nothing to miss
    db.with_name("x.db-wal").write_bytes(b"frames")
    assert backup.read_only_uri(db) == f"file:{db}?mode=ro"


# ======================================================= first-run guard
def test_the_first_writing_run_must_decide_the_latest_completed_week(tmp_path: Path) -> None:
    root = Root.create(tmp_path)
    root.standard_cache()
    # now = 25 Sep 2026 20:00 IST: the latest completed week is 2026-W39.
    assert root.run("--as-of", root.as_of(1)) == EXIT_REFUSED
    assert "the first run must decide the latest completed week, 2026-W39" in root.output[-1]
    assert not root.db.exists()
    assert root.run("--as-of", root.as_of(1), "--dry-run") == EXIT_OK  # still allowed
    assert root.run("--as-of", "auto") == EXIT_OK
    assert root.repo().latest_run() == (date(2026, 9, 25), "COMPLETED")


def test_a_book_with_a_completed_week_is_not_guarded(tmp_path: Path) -> None:
    root = Root.create(tmp_path)
    root.standard_cache()
    root.seed_entry()  # W0 COMPLETED
    assert root.run("--as-of", root.as_of(1)) == EXIT_OK


def test_a_first_run_left_started_for_another_week_is_refused(tmp_path: Path) -> None:
    root = Root.create(tmp_path)
    root.standard_cache()
    root.seed_entry(mark_week=False)
    repo = root.repo()
    repo.mark_started(root.last_session(1), week(1), "crashed")
    repo.database.close()
    assert root.run("--as-of", "auto") == EXIT_REFUSED
    assert "was STARTED and never finished" in root.output[-1]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ==================================================================== R5-7
def test_r5_7_a_trade_rescaled_mid_way_is_journalled_in_each_events_units(
    tmp_path: Path,
) -> None:
    """R1's 1:1 bonus, confirmed and rescaled at run 213 (40 -> 80 shares,
    stop 700 -> 350); the stop then sells all 80 at 217's open. T1 and its
    levels are in T1's units, the exit as filled."""
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _world(
        {210: 1010.0, 211: 1010.0, **{w: 505.0 for w in range(212, 220)}},
        restatements=((213, date.min, monday(212), 0.5),),
    )
    world.close.update({214: 440.0, 215: 380.0, **{w: 340.0 for w in range(216, 220)}})
    rows = (_bonus_row("2"),)
    _through(repo, world, 217, rows)
    (row,) = journal.build_rows(repo)
    assert (row["T1_price"], row["T1_shares"]) == ("1000.00", "40")
    assert (row["L1"], row["stop"]) == ("900.00", "700.00")  # as set at T1, not 450 / 350
    assert (row["exit_shares"], row["exit_type"]) == ("80", "stop")
    assert row["exit_price"] == "340.00"
    assert f"1:1 bonus ex {monday(212)}: 40 -> 80 shares (BUY_T1)" in row["notes"]
    assert "avg_cost in the units after the last rescale" in row["notes"]


def test_r5_7_a_consolidation_close_is_journalled_in_the_old_units(tmp_path: Path) -> None:
    """D101: 1 share, a 10:1 consolidation, 30,000 cash in lieu. The exit is
    the old-unit holding at cash / old shares — not the adjusted close of
    300,000 against 1 old share."""
    from test_stock_corporate_actions import _bonus, _bonus_world
    from test_stock_corporate_actions import _run as _ca_run
    from test_stock_corporate_actions import _through as _ca_through

    repo = _repo(tmp_path / "positional_stocks.db")
    world = _bonus_world(factor=10.0, pre=30000.0, t1_open=30000.0)
    _ca_through(repo, world, 211)
    _ca_run(repo, world, 212, (_bonus("0.1"),))
    (row,) = journal.build_rows(repo)
    assert (row["exit_shares"], row["exit_price"]) == ("1", "30000.00")
    assert row["exit_type"] == "consolidation_cash_in_lieu"
    assert "10:1 consolidation ex" in row["notes"] and "1 -> 0 shares" in row["notes"]


def test_r5_7_rescale_labels() -> None:
    assert journal.rescale_label("BONUS_SPLIT", D("2")) == "1:1 bonus"
    assert journal.rescale_label("BONUS_SPLIT", D("1.2")) == "1:5 bonus"
    assert journal.rescale_label("BONUS_SPLIT", D("0.1")) == "10:1 consolidation"
    assert journal.rescale_label("BONUS_SPLIT", D("1.5")) == "1:2 bonus"
    assert journal.rescale_label("DEMERGER", D("0.8")) == "DEMERGER 0.8"


def _bonus_row(ratio: str) -> CorporateActionRow:
    return CorporateActionRow(
        "A", monday(212), CorporateActionKind.BONUS_SPLIT, D(ratio), date(2026, 9, 26), "test"
    )


# ================================================================ cosmetic
def test_the_repeat_signal_reason_names_the_week_not_a_tuple() -> None:
    from test_wsr1_rules_entry import _entry, _repeat_tape

    funnel, _ = _entry(_repeat_tape({-9: 1100.0, -1: 1000.0}, prior_high=1010.0))
    assert re.search(r"previous trigger's close \(\d{4}-W\d{2}\) without", funnel.reason)
    assert "((" not in funnel.reason


def test_a_stuck_freeze_exit_is_labelled_as_one_in_telegram() -> None:
    from runtimes.positional_stocks.accounting import STUCK_EXIT
    from runtimes.positional_stocks.telegram_summary import exits

    def fill(reason: str, pnl: str) -> SimpleNamespace:
        return SimpleNamespace(
            order=SimpleNamespace(reason=reason),
            outcome=SimpleNamespace(closed=SimpleNamespace(symbol="A", net_pnl=D(pnl))),
        )

    week_record = SimpleNamespace(
        outcome=SimpleNamespace(
            fills=[fill(STUCK_EXIT, "-120.50"), fill("trail exit: close below", "900")],
            events=[],
        )
    )
    assert exits(SimpleNamespace(weeks=[week_record])) == [  # type: ignore[arg-type]
        "A stuck freeze exit -Rs 120.50",
        "A trail +Rs 900.00",
    ]


def test_a_stale_pending_buy_with_nothing_held_is_not_said_to_miss_its_stop(
    tmp_path: Path,
) -> None:
    root = Root.create(tmp_path)
    root.standard_cache()
    # B, seeded with a pending BUY_T1 and never held, stops trading at W0.
    root.write("B", wobble(800.0), until=root.last_session(0))
    root.seed_entry("B", industry="Chemicals")
    assert root.run("--as-of", root.as_of(1)) == EXIT_OK
    assert root.run("--as-of", root.as_of(2)) == EXIT_OK
    warnings = root.report(2).split("## 10.")[1]
    (line,) = [x for x in warnings.splitlines() if "stale (2 week(s)) — B" in x]
    assert line.startswith("- ⚠ ") and "its pending buy cannot fill while it has no bar" in line
    assert "cannot hit its stop" not in line
