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
from common.persistence import Database
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
from .report import (
    OpenPosition,
    PreviewInfo,
    ReportData,
    WeekRecord,
    render,
    render_failure,
    week_label,
)
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
#: Fetch mode (Phase 4b-2): a systemic fetch failure — the index, or more
#: than :data:`~.fetch.MAX_FAILED_SYMBOLS` symbols, failed. No preview.
EXIT_FETCH_FAILED = 5
#: Fetch mode: the preview was written, but some symbols failed to refresh.
EXIT_PREVIEW_PARTIAL = 6

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
    #: Phase 4b-2: with no COMPLETED run yet, a writing decide run must target
    #: the latest completed week as of now (spec 10.2: no backfill of trades).
    #: Only the replay harness, on a temporary project, turns this off.
    first_run_guard: bool = True

    @property
    def paths(self) -> ProjectPaths:
        return ProjectPaths(project_root=self.project_root)


@dataclass(frozen=True)
class Options:
    mode: str
    as_of: str = "auto"
    dry_run: bool = False
    force_refetch: bool = False


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
    conn = sqlite3.connect(backup.read_only_uri(db_path), uri=True)
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


def _has_completed_run(db_path: Path, strategy_id: str) -> bool:
    """Read-only: whether any week was ever COMPLETED."""
    if not db_path.is_file():
        return False
    conn = sqlite3.connect(backup.read_only_uri(db_path), uri=True)
    try:
        if not _has_table(conn, "stock_weekly_runs"):
            return False
        row = conn.execute(
            "SELECT 1 FROM stock_weekly_runs WHERE strategy_id = ? AND status = 'COMPLETED' "
            "LIMIT 1",
            (strategy_id,),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _held_and_pending(db_path: Path, strategy_id: str) -> tuple[list[str], list[str]]:
    """Read-only (R5-4): the held and pending symbols, so the first week can
    be prepared before the snapshot and before the database is opened."""
    if not db_path.is_file():
        return [], []
    conn = sqlite3.connect(backup.read_only_uri(db_path), uri=True)
    try:
        if not _has_table(conn, "stock_positions"):
            return [], []
        held = [
            str(row[0])
            for row in conn.execute(
                "SELECT symbol FROM stock_positions WHERE strategy_id = ? AND state != 'CLOSED'",
                (strategy_id,),
            )
        ]
        pending = [
            str(row[0])
            for row in conn.execute(
                "SELECT symbol FROM stock_pending_orders WHERE strategy_id = ? "
                "AND state = 'PENDING'",
                (strategy_id,),
            )
        ]
    finally:
        conn.close()
    return held, pending


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _first_run_refusal(
    db_path: Path,
    strategy_id: str,
    latest: tuple[date, str] | None,
    target: WeekKey,
    env: RunEnvironment,
    calendar: TradingCalendar,
) -> str | None:
    """The first-run guard (Phase 4b-2). With no COMPLETED run in the real
    database, a writing run decides only the latest week complete as of now:
    an earlier ``--as-of`` would start the book in the past, and the next
    ``auto`` run would then catch up — backfilling trades (spec 10.2)."""
    if _has_completed_run(db_path, strategy_id):
        return None
    now_target = target_week(env.now(), calendar)
    if target != now_target:
        return (
            f"no week has been COMPLETED yet, so the first run must decide the latest "
            f"completed week, {week_label(now_target)} — not {week_label(target)} "
            "(spec 10.2: no backfill of trades). Use --as-of auto, or --dry-run to look "
            "at an earlier week."
        )
    if latest is not None and week_of(latest[0]) != target:
        return (
            f"no week has been COMPLETED yet, but {latest[0]} was STARTED and never finished; "
            f"the first run must decide {week_label(target)}. Nothing was ever completed, "
            f"so move {db_path} aside and run again."
        )
    return None


def _copy_readonly(source: Path, target: Path) -> None:
    """Dry run: SQLite's backup API from a read-only connection."""
    src = sqlite3.connect(backup.read_only_uri(source), uri=True)
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
    parser.add_argument("--dry-run", action="store_true", help="decide mode only")
    parser.add_argument(
        "--force-refetch",
        action="store_true",
        help="fetch mode only: refetch every symbol's full history now (limitation 40)",
    )
    args = parser.parse_args(argv)
    if args.as_of != "auto":
        try:
            date.fromisoformat(args.as_of)
        except ValueError:
            parser.error(f"--as-of must be auto or YYYY-MM-DD, got {args.as_of!r}")
    if args.mode == "fetch" and args.dry_run:
        parser.error(
            "--dry-run is for decide mode: fetch never writes the book (its preview "
            "always runs on a copy)"
        )
    if args.mode == "decide" and args.force_refetch:
        parser.error("--force-refetch is for fetch mode: decide makes no network call")
    return Options(
        mode=args.mode,
        as_of=args.as_of,
        dry_run=args.dry_run,
        force_refetch=args.force_refetch,
    )


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


def _run_locked(
    options: Options,
    env: RunEnvironment,
    *,
    preview: PreviewInfo | None = None,
    deadline: RunDeadline | None = None,
) -> int:
    """Decide the weeks due — on the real book, or (dry run, preview) on a
    temporary copy of it. ``preview`` is the fetch run's (Phase 4b-2): its
    report is ``<week_ending>-preview.md``, and its one Telegram is marked
    PREVIEW. ``deadline`` is the fetch run's, when it calls this."""
    say = env.out
    config = env.config
    paths = env.paths
    if deadline is None:
        # Started at the lock, so the budget covers the whole locked run.
        deadline = RunDeadline.of_minutes(config.decide_deadline_minutes, monotonic=env.monotonic)
    calendar = TradingCalendar.from_config(paths.config_root)
    reports = paths.data_root / "reports" / "positional_stocks"
    db_path = paths.database_path(config.runtime_id)
    on_copy = options.dry_run or preview is not None
    suffix = "-preview" if preview is not None else "-dry-run" if options.dry_run else ""

    try:
        operator = load_operator_inputs(paths.config_root / "positional_stocks")
    except InputFileError as exc:
        say(f"REFUSED: {exc}")
        return EXIT_REFUSED
    moment = as_of_moment(options.as_of, env.now(), calendar.timezone)
    target = target_week(moment, calendar)
    latest = _latest_run(db_path, config.strategy_id)
    if not on_copy and env.first_run_guard:
        refusal = _first_run_refusal(db_path, config.strategy_id, latest, target, env, calendar)
        if refusal is not None:
            say(f"REFUSED: {refusal}")
            return EXIT_REFUSED
    weeks = weeks_to_process(latest, target)
    if not weeks:
        say(f"up to date: {week_label(target)} is already decided; nothing to do")
        return EXIT_OK

    run = _Run(options, env, operator, calendar, reports, suffix, deadline, preview)
    with tempfile.TemporaryDirectory(prefix="positional_stocks_dry_run_") as scratch:
        if on_copy:
            working = Path(scratch) / "positional_stocks.db"
            if db_path.is_file():
                _copy_readonly(db_path, working)
        else:
            working = db_path
        held, pending = _held_and_pending(working, config.strategy_id)
        try:
            return _process(run, weeks, held, pending, working, db_path, write_real=not on_copy)
        finally:
            run.close()


@dataclass
class _Run:
    """One invocation's fixed context, and the book once it is opened."""

    options: Options
    env: RunEnvironment
    operator: OperatorInputs
    calendar: TradingCalendar
    reports: Path
    suffix: str
    deadline: RunDeadline
    preview: PreviewInfo | None
    database: Database | None = None
    records: list[tuple[PreparedWeek, WeekOutcome]] = field(default_factory=list)

    @property
    def sends(self) -> bool:
        """A real run and a preview send Telegram; a dry run does not."""
        return not self.options.dry_run or self.preview is not None

    def close(self) -> None:
        if self.database is not None:
            self.database.close()
            self.database = None


def _process(
    run: _Run,
    weeks: list[WeekKey],
    held: list[str],
    pending: list[str],
    working: Path,
    db_path: Path,
    *,
    write_real: bool,
) -> int:
    env = run.env
    config = env.config
    cache = DailyBarCache.under(env.paths.cache_root, tz_name=run.calendar.timezone)
    run.reports.mkdir(parents=True, exist_ok=True)
    repository: StockRepository | None = None

    def prepare(week: WeekKey, held: list[str], pending: list[str]) -> PreparedWeek:
        return prepare_week(
            week,
            cache=cache,
            calendar=run.calendar,
            operator=run.operator,
            held=held,
            pending=pending,
            index_symbol=config.index_symbol,
            params=config.params,
        )

    for week in weeks:
        week_ending = run.calendar.expected_last_session(week)
        try:
            run.deadline.check(f"preparing {week_label(week)}")
            if repository is not None:
                book = repository.book()
                held = [p.symbol for p in book.positions]
                pending = [o.symbol for o in book.pending]
            prepared = prepare(week, held, pending)
            run.deadline.check(f"persisting {week_label(week)}")
        except ColdCache as exc:
            return _stopped(run, repository, week, week_ending, "cold cache", str(exc))
        except RunDeadlineExceeded as exc:
            return _stopped(
                run,
                repository,
                week,
                week_ending,
                "deadline exceeded",
                str(exc),
                code=EXIT_DEADLINE,
            )
        if repository is None:
            # R5-4: the snapshot only now — the first week is prepared and
            # about to be written — so a cold-cache or deadline attempt never
            # takes one and never pushes an older one out.
            if write_real:
                code = _snapshot(run, db_path, week, week_ending)
                if code is not None:
                    return code
                db_path.parent.mkdir(parents=True, exist_ok=True)
            run.database = open_stock_database(working)
            repository = StockRepository(run.database, config.strategy_id, config.params.capital)
            book = repository.book()
            opened = (
                sorted(p.symbol for p in book.positions),
                sorted(o.symbol for o in book.pending),
            )
            if opened != (sorted(held), sorted(pending)):
                # Read before the open without the repository's rebuild;
                # never decide on a week prepared for another book.
                prepared = prepare(week, *opened)
        outcome = run_decision_week(
            repository, prepared.inputs, calendar=run.calendar, params=config.params
        )
        env.out(f"{week_label(week)}: {outcome.status}")
        run.records.append((prepared, outcome))

    assert repository is not None
    path = _finish(run, repository)
    env.out(f"report: {path}")
    return EXIT_OK


def _snapshot(run: _Run, db_path: Path, week: WeekKey, week_ending: date) -> int | None:
    """D104, R5-5: any failure of the snapshot step refuses the run, with a
    report line and an alert; nothing of the snapshot is left on disk."""
    env = run.env
    config = env.config
    try:
        snap = backup.snapshot(db_path, env.paths.backup_root, keep=config.backups_kept)
    except backup.BackupError as exc:
        env.out(f"REFUSED: {exc}")
        status = send_alert(
            env.notifier,
            f"{week_label(week)}: REFUSED — {exc}",
            runtime_id=config.runtime_id,
            strategy_id=config.strategy_id,
        )
        text = render_failure(
            strategy_id=config.strategy_id,
            generated_at=env.now(),
            week=week,
            kind="backup failed",
            reason=f"REFUSED: {exc}. Nothing was written; fix the backup directory and re-run.",
            dry_run=False,
            notifier_status=status,
        )
        path = run.reports / f"{week_ending}.md"
        path.write_text(text, encoding="utf-8")
        env.out(f"report: {path}")
        return EXIT_REFUSED
    if snap is not None:
        env.out(f"snapshot: {snap}")
    return None


def _finish(run: _Run, repository: StockRepository, *, stopped: str | None = None) -> Path:
    """The report, journal and Telegram for every committed week (R5-2: also
    when a later week then stopped the run — ``stopped`` says why)."""
    env = run.env
    config = env.config
    data = replace(
        _report_data(env, run.options, repository, run.records, run.calendar),
        preview=run.preview,
        stopped=stopped,
    )
    status = "not sent (dry run)"
    if run.sends:
        status = send_summary(
            env.notifier, data, runtime_id=config.runtime_id, strategy_id=config.strategy_id
        )
    if not run.options.dry_run and run.preview is None:
        journal.write(run.reports / "journal.csv", repository)
    data = replace(data, notifier_status=status)
    path = run.reports / f"{run.records[-1][1].week_ending}{run.suffix}.md"
    path.write_text(render(data), encoding="utf-8")
    return path


def _stopped(
    run: _Run,
    repository: StockRepository | None,
    week: WeekKey,
    week_ending: date,
    kind: str,
    reason: str,
    *,
    code: int = EXIT_NO_TRADES,
) -> int:
    """Spec 10.3: no trades for ``week``, report the reason, alert, exit
    non-zero. R5-2: every week already committed in this invocation is
    reported, journalled and telegrammed first, as a normal run would, with
    the stop appended — one Telegram in all."""
    env = run.env
    config = env.config
    done = ", ".join(week_label(p.week) for p, _ in run.records)
    message = f"{week_label(week)}: {kind} — {reason}" + (
        f" (weeks completed first: {done})" if done else ""
    )
    if run.records and repository is not None and run.preview is None:
        path = _finish(run, repository, stopped=message)
        env.out(f"report: {path}")
        status = "included in the summary of the weeks completed first"
    elif run.sends:
        status = send_alert(
            env.notifier, message, runtime_id=config.runtime_id, strategy_id=config.strategy_id
        )
    else:
        status = "not sent (dry run)"
    text = render_failure(
        strategy_id=config.strategy_id,
        generated_at=env.now(),
        week=week,
        kind=kind,
        reason=reason + (f" (weeks completed before it: {done})" if done else ""),
        dry_run=run.options.dry_run,
        notifier_status=status,
    )
    failed_suffix = "-preview-failed" if run.preview is not None else run.suffix
    path = run.reports / f"{week_ending}{failed_suffix}.md"
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
        symbol_week = inputs.symbols.get(position.symbol)
        close = series.bars[-1].close if series is not None and series.bars else None
        if symbol_week is not None and symbol_week.mark_close is not None:
            close = symbol_week.mark_close  # R5-1: as the rules marked it
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
