"""Phase 4b-2: the spec 14 replay harness runs on a temporary project only."""

from __future__ import annotations

from datetime import datetime, time
from pathlib import Path

import pytest
from _stock_run_fixtures import IST, REPO, Root, falling, week

from runtimes.positional_stocks import replay


def test_the_replay_refuses_the_real_project_and_anywhere_inside_it(tmp_path: Path) -> None:
    for inside in (REPO, REPO / "data" / "replay"):
        with pytest.raises(replay.ReplayRefused, match="inside the project"):
            replay.prepare_workdir(inside)
    (tmp_path / "used").mkdir()
    (tmp_path / "used" / "x").write_text("x")
    with pytest.raises(replay.ReplayRefused, match="not empty"):
        replay.prepare_workdir(tmp_path / "used")


def test_the_replay_decides_each_week_in_order_from_an_empty_book(tmp_path: Path) -> None:
    source = Root.create(tmp_path / "source")
    source.standard_cache(a=falling(source.first_session(2)))
    workdir = replay.prepare_workdir(tmp_path / "work", source=source.path)
    assert sorted(p.name for p in (workdir / "config" / "positional_stocks").iterdir()) == [
        "corporate_actions.csv",
        "gap_acknowledgements.csv",
        "quality_gate.csv",
        "results_calendar.csv",
        "universe.csv",
    ]
    lines: list[str] = []
    evening = datetime.combine(source.last_session(3), time(20, 0), IST)
    summaries = replay.replay(workdir, weeks=3, now=lambda: evening, out=lines.append)
    assert [s.week for s in summaries] == [week(1), week(2), week(3)]
    assert all(s.exit_code == 0 for s in summaries)
    assert all(s.equity.startswith("1000000") for s in summaries)  # no trade: cash only
    assert lines[0].startswith("2026-W23 | exit 0 | ")
    reports = workdir / "data" / "reports" / "positional_stocks"
    assert len(list(reports.glob("*.md"))) == 3
    assert not (source.path / "data" / "operational").exists()  # the source is untouched
