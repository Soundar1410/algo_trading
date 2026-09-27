"""The replay smoke of spec 14 — the last N completed weeks through decide mode,
from an empty book, on a TEMPORARY project.

    python -m runtimes.positional_stocks.replay --workdir DIR [--weeks 12] \
        [--through YYYY-MM-DD]

It copies ``config/global.yaml`` (the verified calendar), the operator CSVs of
``config/positional_stocks/`` and the local daily cache into ``DIR``, then runs
:func:`~.weekly_run.run` once per week, in order, exactly as the Monday job
would — each week its own report under ``DIR/data/reports/``. Afterwards it
prints one line per week from the temporary book.

**It never touches the real book.** ``DIR`` must not be the project root, nor
anywhere inside it, so the real ``data/operational/positional_stocks.db``,
``data/backups/`` and ``data/reports/`` cannot be written. It makes no
network call (decide mode is offline), and its notifier only records: no
Telegram is sent.

Three deliberate differences from a scheduled run, each because the replay is
not one: both ``enabled`` flags are treated as true (D115: they gate the real
book, and this is a copy); the preflight (``scripts.validate_environment`` and the paper-safety
check, which inspect the real project and its LaunchAgents) is not run, and
the first-run guard is off — starting a book in the past is the whole point
of a replay, and the guard exists to stop exactly that on the real one.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from common.notifications.base import RecordingNotifier
from common.utils.timeutils import now_ist
from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import WeekKey, shift
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar

from .report import week_label
from .run_config import RunConfig
from .weekly_run import EXIT_OK, Options, RunEnvironment, run, target_week

#: This checkout's root — the project the replay must never write to.
REPO_ROOT = Path(__file__).resolve().parents[2]


class ReplayRefused(RuntimeError):
    """The work directory would put the replay inside the real project."""


@dataclass(frozen=True)
class WeekSummary:
    week: WeekKey
    exit_code: int
    regime: str
    triggered: int
    taken: int
    orders: int
    fills: int
    exits: tuple[str, ...]
    equity: str
    warnings: tuple[str, ...]

    def line(self) -> str:
        exits = ", ".join(self.exits) or "none"
        return (
            f"{week_label(self.week)} | exit {self.exit_code} | {self.regime} | triggered "
            f"{self.triggered} / taken {self.taken} | orders {self.orders} | fills {self.fills} | "
            f"exits: {exits} | equity {self.equity} | warnings {len(self.warnings)}"
        )


def prepare_workdir(workdir: Path, *, source: Path = REPO_ROOT) -> Path:
    """Copy the calendar, the operator CSVs and the daily cache into ``workdir``.

    Raises:
        ReplayRefused: ``workdir`` is ``source`` or inside it.
    """
    workdir = workdir.resolve()
    real = source.resolve()
    if workdir == real or real in workdir.parents:
        raise ReplayRefused(
            f"refusing to replay inside the project ({real}): pass a --workdir outside it"
        )
    if workdir.exists() and any(workdir.iterdir()):
        raise ReplayRefused(f"{workdir} is not empty; the replay starts from an empty book")
    (workdir / "config" / "positional_stocks").mkdir(parents=True)
    shutil.copy(source / "config" / "global.yaml", workdir / "config" / "global.yaml")
    for csv_file in sorted((source / "config" / "positional_stocks").glob("*.csv")):
        shutil.copy(csv_file, workdir / "config" / "positional_stocks" / csv_file.name)
    daily = Path("data") / "cache" / "positional_stocks" / "daily"
    shutil.copytree(source / daily, workdir / daily)
    return workdir


def replay(
    workdir: Path,
    *,
    weeks: int,
    now: Callable[[], datetime] = now_ist,
    through: WeekKey | None = None,
    out: Callable[[str], None] = print,
    config: RunConfig | None = None,
) -> list[WeekSummary]:
    """Decide ``weeks`` weeks ending at ``through`` (default: the latest
    complete as of ``now``) on the book in ``workdir``, one run per week."""
    calendar = TradingCalendar.from_config(workdir / "config")
    last = through if through is not None else target_week(now(), calendar)
    order = [shift(last, -n) for n in range(weeks - 1, -1, -1)]
    notifier = RecordingNotifier()
    lines: list[str] = []
    env = RunEnvironment(
        project_root=workdir,
        now=now,
        notifier=notifier,
        preflight=lambda: [],
        out=lines.append,
        # D115: both enabled flags gate a writing run on the real project; the
        # replay's book is a temporary copy, so it runs whatever they say.
        config=replace(config or RunConfig(), runtime_enabled=True, strategy_enabled=True),
        first_run_guard=False,
    )
    summaries = []
    for week in order:
        lines.clear()
        ending = calendar.expected_last_session(week)
        code = run(Options(mode="decide", as_of=ending.isoformat()), env)
        summary = _summarise(env, week, ending.isoformat(), code, lines)
        out(summary.line())
        for warning in summary.warnings:
            out(f"    {warning}")
        summaries.append(summary)
    return summaries


def _summarise(
    env: RunEnvironment, week: WeekKey, ending: str, code: int, lines: list[str]
) -> WeekSummary:
    db = env.paths.database_path(env.config.runtime_id)
    label = week_label(week)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        equity = conn.execute(
            "SELECT regime, equity, warnings FROM stock_equity WHERE week_ending = ?", (ending,)
        ).fetchone()
        triggered = conn.execute(
            "SELECT count(*) FROM stock_signals WHERE week_ending = ? AND triggered = 1",
            (ending,),
        ).fetchone()[0]
        taken = conn.execute(
            "SELECT count(*) FROM stock_signals WHERE week_ending = ? AND stage = 'taken'",
            (ending,),
        ).fetchone()[0]
        orders = conn.execute(
            "SELECT count(*) FROM stock_pending_orders WHERE decided_week = ?", (label,)
        ).fetchone()[0]
        fills = conn.execute(
            "SELECT count(*) FROM stock_fills WHERE recorded_week = ?", (label,)
        ).fetchone()[0]
        exits = tuple(
            f"{row['symbol']} {row['net_pnl']}"
            for row in conn.execute(
                "SELECT symbol, net_pnl FROM stock_positions WHERE exit_week = ? ORDER BY symbol",
                (label,),
            )
        )
    finally:
        conn.close()
    warnings = [line for line in lines if not line.startswith(("report:", "snapshot:", label))]
    if equity is not None:
        warnings += json.loads(equity["warnings"])
    return WeekSummary(
        week=week,
        exit_code=code,
        regime=equity["regime"] if equity is not None else "—",
        triggered=int(triggered),
        taken=int(taken),
        orders=int(orders),
        fills=int(fills),
        exits=exits,
        equity=equity["equity"] if equity is not None else "—",
        warnings=tuple(warnings),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m runtimes.positional_stocks.replay",
        description="Spec 14's replay: the last N weeks through decide mode, on a copy.",
    )
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--weeks", type=int, default=12)
    parser.add_argument("--through", help="YYYY-MM-DD: the last week replayed contains it")
    args = parser.parse_args(argv)
    try:
        workdir = prepare_workdir(args.workdir)
    except ReplayRefused as exc:
        print(f"REFUSED: {exc}")
        return 1
    through = None
    if args.through:
        from datetime import date

        from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import week_of

        through = week_of(date.fromisoformat(args.through))
    summaries = replay(workdir, weeks=args.weeks, through=through)
    print(f"reports: {workdir / 'data' / 'reports' / 'positional_stocks'}")
    return EXIT_OK if all(s.exit_code == EXIT_OK for s in summaries) else 1


if __name__ == "__main__":
    sys.exit(main())
