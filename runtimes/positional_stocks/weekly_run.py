"""The weekly run of ``wsr1_weekly_stochrsi`` (spec 10) — ``decide`` mode.

    python -m runtimes.positional_stocks.weekly_run --mode decide \
        [--as-of auto|YYYY-MM-DD] [--dry-run]

``--mode fetch`` is Phase 4b-2 and refuses for now.

**Offline (spec 10.3).** ``decide`` reads only the local, symbol-keyed daily
cache and the operator CSVs. It constructs no scrip master, no
``AuthBootstrap``, no historical client, no market-feed adapter, no worker
and no supervisor. Telegram is its one outbound call, and a missing or
failing notifier is non-fatal.

**Order of a run.** Paper mode; ``scripts.validate_environment`` and the
paper-safety check; the process lock (shared by both modes); the 5-minute
deadline; the weeks to process (10.2); a verified snapshot of the database
before its first write (D104); each week through
:func:`~.accounting.run_decision_week`; the report, journal and Telegram
summary, once, for everything processed.

**Exit codes:** 0 done or already up to date; 1 refused (not paper, an
environment or paper-safety check, an input file, a backup); 2 no trades
(cold cache) or fetch mode not built; 3 another run holds the lock (nothing
touched); 4 deadline exceeded (fail closed).

``--dry-run`` computes and writes ``<week_ending>-dry-run.md`` on a temporary
copy of the book: ``positional_stocks.db`` is never created or changed, no
snapshot is taken, no journal is written, no Telegram is sent.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
import time as _time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout

from common.config.paths import ProjectPaths
from common.logging import get_logger
from common.notifications.base import Notifier
from strategies.positional_stocks.wsr1_weekly_stochrsi.daily_cache import DailyBarCache
from strategies.positional_stocks.wsr1_weekly_stochrsi.gaps import (
    block_window_start,
    blocking_gaps,
    scan,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.inputs import InputFileError
from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import (
    WeekKey,
    shift,
    week_of,
    weeks_between,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    PositionState,
    money,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.pacing import (
    RunDeadline,
    RunDeadlineExceeded,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import watchlist
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar
from strategies.positional_stocks.wsr1_weekly_stochrsi.weekly_bars import is_week_complete

from . import backup, journal
from .accounting import WeekOutcome, complete_symbols, run_decision_week
from .database import open_stock_database
from .report import OpenPosition, ReportData, WeekRecord, render, render_failure, week_label
from .repository import StockRepository, drawdown_pct
from .run_config import RunConfig, RunRefused
from .telegram_summary import send_alert, send_summary
from .week_inputs import (
    ColdCache,
    OperatorInputs,
    PreparedWeek,
    load_operator_inputs,
    prepare_week,
)

_log = get_logger(__name__)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_NO_TRADES = 2
EXIT_LOCKED = 3
EXIT_DEADLINE = 4

LOCK_NAME = "positional_stocks.weekly_run.lock"


@dataclass
class RunEnvironment:
    """Everything a run touches outside its arguments — injected by tests."""

    project_root: Path
    now: Callable[[], datetime]
    notifier: Notifier
    #: Returns the problems found; empty means the run may start.
    preflight: Callable[[], list[str]]
    monotonic: Callable[[], float] = _time.monotonic
    out: Callable[[str], None] = print
    config: RunConfig = field(default_factory=RunConfig)

    @property
    def paths(self) -> ProjectPaths:
        return ProjectPaths(project_root=self.project_root)


@dataclass(frozen=True)
class Options:
    mode: str
    as_of: str = "auto"
    dry_run: bool = False


def default_environment() -> RunEnvironment:
    """The real environment: settings, paths, clock, Telegram, the checks."""
    from common.config import load_settings
    from common.config.paths import load_paths
    from common.notifications.base import SafeNotifier
    from common.notifications.factory import build_notifier
    from common.utils.timeutils import now_ist

    settings = load_settings()
    paths = load_paths(settings=settings)
    return RunEnvironment(
        project_root=paths.project_root,
        now=now_ist,
        notifier=SafeNotifier(build_notifier(settings)),
        preflight=lambda: default_preflight(paths.config_root, RunConfig().runtime_id),
    )


def default_preflight(config_root: Path, runtime_id: str) -> list[str]:
    """Spec 10.1: ``scripts.validate_environment`` and the paper-safety check,
    both offline-safe, both called unchanged. Legacy detection is inside
    ``validate_environment``, so the paper-safety check skips its own."""
    from orchestration.auto_start.paper_safety import verify_paper_only
    from scripts import validate_environment

    problems: list[str] = []
    if validate_environment.main(["--runtime-id", runtime_id]) != validate_environment.EXIT_OK:
        problems.append("scripts.validate_environment reported problems (its output is above)")
    report = verify_paper_only(config_root, check_legacy=False, check_environment=False)
    problems += [f"paper-safety: {v}" for v in report.violations]
    return problems


# ------------------------------------------------------------ the weeks
def as_of_moment(as_of: str, now: datetime, tz_name: str) -> datetime:
    """``auto`` is now; a date is the end of that day in IST."""
    if as_of == "auto":
        return now
    return datetime.combine(date.fromisoformat(as_of), time(23, 59, 59), ZoneInfo(tz_name))


def target_week(moment: datetime, calendar: TradingCalendar) -> WeekKey:
    """The most recent ISO week complete as of ``moment`` (spec 10.1 ``auto``)."""
    week = week_of(moment.date())
    for _ in range(8):
        if is_week_complete(calendar.expected_last_session(week), moment, calendar=calendar):
            return week
        week = shift(week, -1)
    raise RunRefused(f"no complete week found before {moment.isoformat()}")


def weeks_to_process(latest: tuple[date, str] | None, target: WeekKey) -> list[WeekKey]:
    """Spec 10.2 step 2: every week after the last COMPLETED run, in order, up
    to the target; a STARTED week is redone first; with no prior run, only
    the target week (no backfill of trades)."""
    if latest is None:
        return [target]
    week, status = week_of(latest[0]), latest[1]
    start = week if status == "STARTED" else shift(week, 1)
    out: list[WeekKey] = []
    while weeks_between(start, target) >= 0:
        out.append(start)
        start = shift(start, 1)
    return out


def _latest_run(db_path: Path, strategy_id: str) -> tuple[date, str] | None:
    """Read-only: the most recent run, without creating or migrating the DB."""
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'stock_weekly_runs'"
        ).fetchone()
        if exists is None:
            return None
        row = conn.execute(
            "SELECT week_ending, status FROM stock_weekly_runs WHERE strategy_id = ? "
            "ORDER BY week_ending DESC LIMIT 1",
            (strategy_id,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else (date.fromisoformat(row[0]), str(row[1]))


def _copy_readonly(source: Path, target: Path) -> None:
    """Dry run: SQLite's backup API from a read-only connection."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


# ------------------------------------------------------------------ CLI
def parse(argv: Sequence[str] | None) -> Options:
    parser = argparse.ArgumentParser(
        prog="python -m runtimes.positional_stocks.weekly_run",
        description="The weekly run of wsr1_weekly_stochrsi (spec 10). PAPER ONLY.",
    )
    parser.add_argument("--mode", required=True, choices=("fetch", "decide"))
    parser.add_argument("--as-of", default="auto", help="auto, or YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.as_of != "auto":
        try:
            date.fromisoformat(args.as_of)
        except ValueError:
            parser.error(f"--as-of must be auto or YYYY-MM-DD, got {args.as_of!r}")
    return Options(mode=args.mode, as_of=args.as_of, dry_run=args.dry_run)


def main(argv: Sequence[str] | None = None, env: RunEnvironment | None = None) -> int:
    options = parse(argv)
    if options.mode == "fetch":
        print("--mode fetch: not built yet (Phase 4b-2)")
        return EXIT_NO_TRADES
    return run(options, env if env is not None else default_environment())


def run(options: Options, env: RunEnvironment) -> int:
    say = env.out
    config = env.config
    try:
        config.check_paper()
    except RunRefused as exc:
        say(f"REFUSED: {exc}")
        return EXIT_REFUSED
    problems = env.preflight()
    if problems:
        for problem in problems:
            say(f"REFUSED: {problem}")
        return EXIT_REFUSED

    paths = env.paths
    paths.lock_root.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(paths.lock_root / LOCK_NAME), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        say(
            f"another weekly run holds the lock ({paths.lock_root / LOCK_NAME}); "
            "nothing done. Try again once it finishes."
        )
        return EXIT_LOCKED
    try:
        return _run_locked(options, env)
    finally:
        lock.release()


def _run_locked(options: Options, env: RunEnvironment) -> int:
    say = env.out
    config = env.config
    paths = env.paths
    # Started at the lock, so the budget covers the whole locked run.
    deadline = RunDeadline.of_minutes(config.decide_deadline_minutes, monotonic=env.monotonic)
    calendar = TradingCalendar.from_config(paths.config_root)
    reports = paths.data_root / "reports" / "positional_stocks"
    db_path = paths.database_path(config.runtime_id)
    suffix = "-dry-run" if options.dry_run else ""

    try:
        operator = load_operator_inputs(paths.config_root / "positional_stocks")
    except InputFileError as exc:
        say(f"REFUSED: {exc}")
        return EXIT_REFUSED
    moment = as_of_moment(options.as_of, env.now(), calendar.timezone)
    target = target_week(moment, calendar)
    weeks = weeks_to_process(_latest_run(db_path, config.strategy_id), target)
    if not weeks:
        say(f"up to date: {week_label(target)} is already decided; nothing to do")
        return EXIT_OK

    with tempfile.TemporaryDirectory(prefix="positional_stocks_dry_run_") as scratch:
        if options.dry_run:
            working = Path(scratch) / "positional_stocks.db"
            if db_path.is_file():
                _copy_readonly(db_path, working)
        else:
            working = db_path
            try:
                snap = backup.snapshot(db_path, paths.backup_root, keep=config.backups_kept)
            except backup.BackupError as exc:
                say(f"REFUSED: {exc}")
                return EXIT_REFUSED
            if snap is not None:
                say(f"snapshot: {snap}")
            db_path.parent.mkdir(parents=True, exist_ok=True)
        database = open_stock_database(working)
        try:
            repository = StockRepository(database, config.strategy_id, config.params.capital)
            return _process(
                options, env, repository, operator, calendar, weeks, reports, suffix, deadline
            )
        finally:
            database.close()


def _process(
    options: Options,
    env: RunEnvironment,
    repository: StockRepository,
    operator: OperatorInputs,
    calendar: TradingCalendar,
    weeks: list[WeekKey],
    reports: Path,
    suffix: str,
    deadline: RunDeadline,
) -> int:
    say = env.out
    config = env.config
    cache = DailyBarCache.under(env.paths.cache_root, tz_name=calendar.timezone)
    records: list[tuple[PreparedWeek, WeekOutcome]] = []
    reports.mkdir(parents=True, exist_ok=True)

    for week in weeks:
        week_ending = calendar.expected_last_session(week)
        try:
            deadline.check(f"preparing {week_label(week)}")
            book = repository.book()
            prepared = prepare_week(
                week,
                cache=cache,
                calendar=calendar,
                operator=operator,
                held=[p.symbol for p in book.positions],
                pending=[o.symbol for o in book.pending],
                index_symbol=config.index_symbol,
                params=config.params,
            )
            deadline.check(f"persisting {week_label(week)}")
        except ColdCache as exc:
            return _no_trades(
                env, options, reports, suffix, week, week_ending, "cold cache", str(exc), records
            )
        except RunDeadlineExceeded as exc:
            return _no_trades(
                env,
                options,
                reports,
                suffix,
                week,
                week_ending,
                "deadline exceeded",
                str(exc),
                records,
                code=EXIT_DEADLINE,
            )
        outcome = run_decision_week(
            repository, prepared.inputs, calendar=calendar, params=config.params
        )
        say(f"{week_label(week)}: {outcome.status}")
        records.append((prepared, outcome))

    data = _report_data(env, options, repository, records, calendar)
    status = "not sent (dry run)"
    if not options.dry_run:
        status = send_summary(
            env.notifier, data, runtime_id=config.runtime_id, strategy_id=config.strategy_id
        )
        journal.write(reports / "journal.csv", repository)
    data = replace(data, notifier_status=status)
    path = reports / f"{records[-1][1].week_ending}{suffix}.md"
    path.write_text(render(data), encoding="utf-8")
    say(f"report: {path}")
    return EXIT_OK


def _no_trades(
    env: RunEnvironment,
    options: Options,
    reports: Path,
    suffix: str,
    week: WeekKey,
    week_ending: date,
    kind: str,
    reason: str,
    records: list[tuple[PreparedWeek, WeekOutcome]],
    *,
    code: int = EXIT_NO_TRADES,
) -> int:
    """Spec 10.3: no trades, report the reason, alert, exit non-zero."""
    config = env.config
    done = ", ".join(week_label(p.week) for p, _ in records)
    message = f"{week_label(week)}: {kind} — {reason}" + (
        f" (weeks completed first: {done})" if done else ""
    )
    status = "not sent (dry run)"
    if not options.dry_run:
        status = send_alert(
            env.notifier, message, runtime_id=config.runtime_id, strategy_id=config.strategy_id
        )
    text = render_failure(
        strategy_id=config.strategy_id,
        generated_at=env.now(),
        week=week,
        kind=kind,
        reason=reason + (f" (weeks completed before it: {done})" if done else ""),
        dry_run=options.dry_run,
        notifier_status=status,
    )
    path = reports / f"{week_ending}{suffix}.md"
    path.write_text(text, encoding="utf-8")
    env.out(f"NO TRADES — {message}")
    env.out(f"report: {path}")
    return code


def _report_data(
    env: RunEnvironment,
    options: Options,
    repository: StockRepository,
    records: list[tuple[PreparedWeek, WeekOutcome]],
    calendar: TradingCalendar,
) -> ReportData:
    config = env.config
    prepared, outcome = records[-1]
    decision = outcome.decision
    assert decision is not None
    inputs = prepared.inputs
    ctx = inputs.ctx
    week_records = []
    for prep, out in records:
        daily = prep.inputs.daily
        week_gaps = tuple(
            gap
            for symbol in sorted(daily)
            for gap in scan(symbol, daily[symbol])
            if week_of(gap.session) == prep.week
        )
        week_records.append(
            WeekRecord(
                week=prep.week,
                week_ending=out.week_ending,
                outcome=out,
                stale=prep.stale,
                unlisted_sessions=prep.unlisted_sessions,
                warnings=prep.warnings,
                week_gaps=week_gaps,
            )
        )

    frozen = {f.position_id: f for f in outcome.frozen}
    stale = {s.symbol: s.weeks for s in prepared.stale if s.kept}
    held = {
        p.position_id: p
        for p in repository.positions().values()
        if p.state is not PositionState.CLOSED
    }
    rows = []
    for position in sorted(held.values(), key=lambda p: p.symbol):
        series = prepared.series.get(position.symbol)
        close = series.bars[-1].close if series is not None and series.bars else None
        freeze = frozen.get(position.position_id)
        mark = None
        if close is not None:
            factor = freeze.factor if freeze is not None else Decimal("1")
            mark = money(Decimal(str(close)) / factor)
        rows.append(
            OpenPosition(
                position=position,
                mark=mark,
                weeks_held=weeks_between(position.t1_fill_week, ctx.week),
                freeze=freeze,
                stale_weeks=stale.get(position.symbol, 0),
            )
        )

    daily_upto = {
        s: [bar for bar in bars if bar.session <= ctx.week_ending]
        for s, bars in inputs.daily.items()
    }
    completed = complete_symbols(inputs, daily_upto, repository.last_seen_rows())
    book = repository.book()
    blocked = tuple(
        gap
        for symbol, week_inputs in sorted(completed.items())
        for gap in blocking_gaps(
            scan(symbol, daily_upto.get(symbol, ())),
            inputs.acknowledgements,
            window_start=block_window_start(week_inputs.series.bars),
        )
    )
    assert decision.brakes.peak is not None
    return ReportData(
        strategy_id=config.strategy_id,
        generated_at=env.now(),
        dry_run=options.dry_run,
        weeks=tuple(week_records),
        decision=decision,
        cash=repository.cash(),
        drawdown_pct=drawdown_pct(decision.equity, decision.brakes.peak),
        positions=tuple(rows),
        pending=tuple(book.pending),
        held=held,
        watchlist=tuple(watchlist(completed, inputs.index, book, ctx, config.params)),
        blocking_gaps=blocked,
        today=env.now().date(),
    )


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "EXIT_DEADLINE",
    "EXIT_LOCKED",
    "EXIT_NO_TRADES",
    "EXIT_OK",
    "EXIT_REFUSED",
    "Options",
    "RunEnvironment",
    "main",
    "run",
]
