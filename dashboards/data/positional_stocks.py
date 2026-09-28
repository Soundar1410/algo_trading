"""Read model for the Positional Stocks page — the ``wsr1_weekly_stochrsi`` paper book.

Spec v1.3.2 section 11.1 (Phase 6). **Read-only, always.** The book
(``data/operational/positional_stocks.db``) is opened only through
:func:`dashboards._shared.run_bounded`, which wraps the driver-enforced
``mode=ro`` :func:`~common.persistence.connect_readonly`. Every other source is
a file read: ``journal.csv``, the weekly reports, the backup directory, the
daily cache, the two committed YAML files and — as a file-existence check
only — ``~/Library/LaunchAgents``. Nothing here writes, shells out or opens a
socket.

**No trading logic is re-derived.** Each value comes from what the weekly run
persisted, or from the runtime's own read functions on the read-only
connection:

* positions and pending orders through
  :meth:`~runtimes.positional_stocks.repository.StockRepository.positions` /
  ``pending_orders`` (levels already rescaled by the runtime's own
  ``rescale_levels``), given the read-only connection explicitly. The
  repository is built around :class:`_NoDatabase`, whose ``connect()`` raises:
  a read that ever fell back to its own (write-capable) database would fail
  loudly, never open one;
* regime, equity, cash, peak, drawdown and brakes from ``stock_equity``;
  runs from ``stock_weekly_runs``; frozen flags from
  ``stock_position_reviews``; needs-quality from ``stock_signals``;
* the mark, unrealised P&L and weeks held from the **latest decision
  report's** section 4, only when that report is for the latest COMPLETED
  week — the mark involves the freeze factor and the R5-1 mark close, which
  the run does not persist anywhere else (D136);
* silent freeze lifts and stale held symbols from the latest decision report,
  failed fetch symbols from the newest preview. A section the parser cannot
  read is reported as "could not read <item> from <file>", never as zero.

**Never loads network code.** ``runtimes.positional_stocks.run_config``,
``report`` and ``weekly_run`` are *not* imported: each loads
``common.authentication.bootstrap`` (AuthBootstrap) transitively, through
``strategies.…pacing``. The few configuration values the page shows are read
through the shared ``common.config`` loaders onto
:class:`~strategies.positional_stocks.wsr1_weekly_stochrsi.models.RulesParameters`
defaults, and a contract test holds them equal to ``RunConfig``'s (D135).
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from common.config import load_runtime_config, load_strategy_config
from common.utils import timeutils
from dashboards._shared import SnapshotUnavailable, run_bounded
from runtimes.positional_stocks.journal import COLUMNS as JOURNAL_COLUMNS
from runtimes.positional_stocks.repository import (
    StockRepository,
    order_id,
    week_key,
    week_text,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.daily_cache import DailyBarCache
from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import WeekKey, shift, week_of
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    OrderAction,
    Position,
    PositionState,
    RulesParameters,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import FROZEN_FLAG
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import NEEDS_QUALITY as NEEDS_QUALITY
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar
from strategies.positional_stocks.wsr1_weekly_stochrsi.weekly_bars import is_week_complete

RUNTIME_ID = "positional_stocks"
STRATEGY_ID = "wsr1_weekly_stochrsi"
#: ``runtimes.positional_stocks.run_config.INDEX_SYMBOL`` (a test holds them equal).
INDEX_SYMBOL = "NIFTY"
#: ``orchestration.launchd.generate_plists.LABEL_PREFIX`` and the two
#: operator-installed agents' short names (a test holds them equal).
LABEL_PREFIX = "com.soundarraj.algotrading"
AGENT_NAMES = ("positional_stocks_fetch", "positional_stocks_decide")
#: A review flag of an escalated or mixed-units freeze (``rules._frozen_review``).
OPERATOR_ACTION_PREFIX = "operator action:"
#: Recent fills shown on the Trades tab.
RECENT_FILLS = 25
#: Refusal / failed-preview reports listed on the Health tab.
RECENT_REFUSALS = 10

GO_LIVE_POINTER = (
    "See the go-live checklist: docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md → "
    "“positional_stocks — Operator guide” → “Go-live checklist”."
)
NOT_STARTED = (
    "Not started yet — no weekly decide run has written the book "
    "(data/operational/positional_stocks.db does not exist). " + GO_LIVE_POINTER
)
BUSY = "Book busy — a weekly run is writing to it right now. Refresh in a moment."
#: A week counts as expected-decided once its decide slot plus this grace has passed.
DECIDE_GRACE = timedelta(minutes=60)
#: ``run_config._WEEKDAYS``: the day names ``parameters.schedule`` uses.
WEEKDAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")

#: Book states.
OK = "ok"
NOT_STARTED_STATE = "not_started"
BUSY_STATE = "busy"
UNREADABLE_STATE = "unreadable"


class ReportParseError(ValueError):
    """A report section the dashboard cannot read. Shown, never swallowed as 0."""


# ============================================================== config
@dataclass(frozen=True)
class ConfigView:
    """What the page shows from the two committed YAML files."""

    runtime_enabled: bool | None = None
    strategy_enabled: bool | None = None
    capital: Decimal | None = None
    max_positions: int | None = None
    committed_cap: Decimal | None = None
    dd1_pct: Decimal | None = None
    dd2_pct: Decimal | None = None
    error: str | None = None
    #: ``parameters.schedule.decide`` (Python weekday, IST time), or why not.
    decide_slot: tuple[int, time] | None = None
    schedule_error: str | None = None

    def decide_after(self, week_ending: date, tz_name: str = timeutils.DEFAULT_TZ) -> datetime:
        """``run_config.Schedule.decide_after``: the first decide slot after
        ``week_ending`` (the week's last session), in calendar days — so a
        Monday-holiday week still decides at the Monday slot. A test holds the
        two equal."""
        if self.decide_slot is None:
            raise ValueError(self.schedule_error or "no decide slot")
        weekday, at = self.decide_slot
        start = week_ending + timedelta(days=1)
        day = start + timedelta(days=(weekday - start.weekday()) % 7)
        return datetime.combine(day, at, timeutils.get_tz(tz_name))


def parse_slot(text: str) -> tuple[int, time]:
    """``run_config.Slot.parse``: ``"MONDAY 08:30"`` -> ``(0, 08:30)``."""
    try:
        day, clock = text.split()
        hour, minute = clock.split(":")
        return WEEKDAYS.index(day.upper()), time(int(hour), int(minute))
    except (ValueError, IndexError) as exc:
        raise ValueError(f"schedule slot must be like 'MONDAY 08:30', got {text!r}") from exc


#: ``parameters`` key -> RulesParameters field, for the values shown here only.
#: The same pairs as ``run_config._RULES`` (a test holds them equal).
_CONFIG_KEYS: dict[tuple[str | None, str], str] = {
    (None, "capital"): "capital",
    (None, "max_positions"): "max_positions",
    (None, "committed_cap_pct"): "committed_cap_pct",
    ("brakes", "dd1_pct"): "dd1_pct",
    ("brakes", "dd2_pct"): "dd2_pct",
}


def load_config_view(config_root: Path) -> ConfigView:
    """Both ``enabled`` flags and five parameters, through the shared loaders.

    Never raises: a broken file is a message (``check_config`` is the
    authority on whether the weekly run would accept it).
    """
    try:
        runtime = load_runtime_config(config_root, RUNTIME_ID)
        strategy = load_strategy_config(config_root, STRATEGY_ID, runtime_id=RUNTIME_ID)
        raw = dict(strategy.parameters)
        overrides: dict[str, Any] = {}
        for (section, key), attr in _CONFIG_KEYS.items():
            where = raw if section is None else raw.get(section) or {}
            if isinstance(where, Mapping) and key in where:
                default = getattr(RulesParameters(), attr)
                value = where[key]
                overrides[attr] = int(value) if isinstance(default, int) else Decimal(str(value))
        params = RulesParameters(**overrides)
    except Exception as exc:  # any load or validation problem is shown, never raised
        error = f"could not read the configuration: {type(exc).__name__}: {exc}"
        return ConfigView(error=error, schedule_error=error)
    decide_slot: tuple[int, time] | None = None
    schedule_error: str | None = None
    try:
        schedule = raw.get("schedule")
        decide = schedule.get("decide") if isinstance(schedule, Mapping) else None
        if not isinstance(decide, str):
            raise ValueError("parameters.schedule.decide is missing")
        decide_slot = parse_slot(decide)
    except ValueError as exc:
        schedule_error = f"could not read the decide slot: {exc}"
    return ConfigView(
        decide_slot=decide_slot,
        schedule_error=schedule_error,
        runtime_enabled=runtime.enabled,
        strategy_enabled=strategy.enabled,
        capital=params.capital,
        max_positions=params.max_positions,
        committed_cap=params.committed_cap,
        dd1_pct=params.dd1_pct,
        dd2_pct=params.dd2_pct,
    )


def agents_installed(launch_agents_dir: Path) -> dict[str, bool]:
    """Whether each agent's plist file exists. A file check only — no launchctl."""
    return {
        name: (launch_agents_dir / f"{LABEL_PREFIX}.{name}.plist").is_file() for name in AGENT_NAMES
    }


# ================================================================ book
@dataclass(frozen=True)
class RunRow:
    week_ending: str
    iso_week: str
    status: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True)
class EquityRow:
    week_ending: str
    iso_week: str
    cash: Decimal
    positions_value: Decimal
    equity: Decimal
    peak: Decimal
    drawdown_pct: Decimal
    regime: str
    brake1_until: str | None
    brake1_can_fire: bool
    brake2_fired_on: str | None
    entries_blocked: str | None


@dataclass(frozen=True)
class ReviewRow:
    position_id: str
    symbol: str
    reason: str
    flags: tuple[str, ...]

    @property
    def frozen(self) -> bool:
        return FROZEN_FLAG in self.flags

    @property
    def escalated(self) -> bool:
        return self.frozen and any(f.startswith(OPERATOR_ACTION_PREFIX) for f in self.flags)


@dataclass(frozen=True)
class PendingRow:
    symbol: str
    action: str
    decided_week: str
    execute_on_or_after: str
    amount: Decimal | None
    quantity: int | None
    reason: str
    catch_up: bool
    position_id: str | None
    spacing: Decimal | None


@dataclass(frozen=True)
class FillRow:
    session: str
    symbol: str
    action: str
    shares: int
    price: Decimal
    fees: Decimal
    late_fill: bool
    not_traded: bool
    catch_up: bool
    recorded_week: str


@dataclass(frozen=True)
class BookData:
    """Everything read from ``positional_stocks.db`` in one bounded connection."""

    runs: tuple[RunRow, ...]
    equity: tuple[EquityRow, ...]
    held: tuple[Position, ...]
    pending: tuple[PendingRow, ...]
    fills: tuple[FillRow, ...]
    #: Reviews of the latest COMPLETED week, by position id.
    reviews: dict[str, ReviewRow]
    #: Symbols the latest COMPLETED week refused for want of a quality row.
    needs_quality: tuple[str, ...]

    @property
    def latest_completed(self) -> RunRow | None:
        done = [r for r in self.runs if r.status == "COMPLETED"]
        return done[-1] if done else None

    @property
    def latest_run(self) -> RunRow | None:
        return self.runs[-1] if self.runs else None

    @property
    def latest_equity(self) -> EquityRow | None:
        return self.equity[-1] if self.equity else None


class _NoDatabase:
    """Stands in for the repository's write-capable ``Database``.

    Every repository read the dashboard makes passes the read-only connection
    explicitly; if one ever fell back to ``self._db.connect()``, this raises
    instead of opening a read-write connection.
    """

    def connect(self) -> sqlite3.Connection:
        raise RuntimeError("the dashboard never opens a write-capable connection")


def _dec(value: object) -> Decimal:
    return Decimal(str(value))


def _read_book(conn: sqlite3.Connection, capital: Decimal) -> BookData:
    repository = StockRepository(_NoDatabase(), STRATEGY_ID, capital)  # type: ignore[arg-type]
    runs = tuple(
        RunRow(r["week_ending"], r["iso_week"], r["status"], r["started_at"], r["finished_at"])
        for r in conn.execute(
            "SELECT week_ending, iso_week, status, started_at, finished_at "
            "FROM stock_weekly_runs WHERE strategy_id = ? ORDER BY week_ending",
            (STRATEGY_ID,),
        )
    )
    equity = tuple(
        EquityRow(
            week_ending=r["week_ending"],
            iso_week=r["iso_week"],
            cash=_dec(r["cash"]),
            positions_value=_dec(r["positions_value"]),
            equity=_dec(r["equity"]),
            peak=_dec(r["peak"]),
            drawdown_pct=_dec(r["drawdown_pct"]),
            regime=r["regime"],
            brake1_until=r["brake1_until"],
            brake1_can_fire=bool(r["brake1_can_fire"]),
            brake2_fired_on=r["brake2_fired_on"],
            entries_blocked=r["entries_blocked"],
        )
        for r in conn.execute(
            "SELECT * FROM stock_equity WHERE strategy_id = ? ORDER BY week_ending",
            (STRATEGY_ID,),
        )
    )
    positions = repository.positions(conn)
    held = tuple(
        sorted(
            (p for p in positions.values() if p.state is not PositionState.CLOSED),
            key=lambda p: p.symbol,
        )
    )
    catch_up = {
        r["order_id"]
        for r in conn.execute(
            "SELECT order_id FROM stock_pending_orders WHERE strategy_id = ? "
            "AND state = 'PENDING' AND catch_up = 1",
            (STRATEGY_ID,),
        )
    }
    pending = tuple(
        PendingRow(
            symbol=o.symbol,
            action=o.action.value,
            decided_week=week_text(o.decided_week),
            execute_on_or_after=o.execute_on_or_after.isoformat(),
            amount=o.amount,
            quantity=o.quantity,
            reason=o.reason,
            catch_up=order_id(STRATEGY_ID, o) in catch_up,
            position_id=o.position_id,
            spacing=o.sizing.spacing if o.sizing is not None else None,
        )
        for o in sorted(repository.pending_orders(conn), key=lambda o: (o.symbol, o.action.value))
    )
    fills = tuple(
        FillRow(
            session=r["session"],
            symbol=r["symbol"],
            action=r["action"],
            shares=int(r["shares"]),
            price=_dec(r["price"]),
            fees=_dec(r["fees"]),
            late_fill=bool(r["late_fill"]),
            not_traded=bool(r["not_traded_on_execution_session"]),
            catch_up=bool(r["catch_up"]),
            recorded_week=r["recorded_week"],
        )
        for r in conn.execute(
            "SELECT * FROM stock_fills WHERE strategy_id = ? ORDER BY fill_id DESC LIMIT ?",
            (STRATEGY_ID, RECENT_FILLS),
        )
    )
    completed = [r for r in runs if r.status == "COMPLETED"]
    reviews: dict[str, ReviewRow] = {}
    needs_quality: tuple[str, ...] = ()
    if completed:
        latest = completed[-1].week_ending
        for r in conn.execute(
            "SELECT position_id, symbol, reason, flags FROM stock_position_reviews "
            "WHERE strategy_id = ? AND week_ending = ?",
            (STRATEGY_ID, latest),
        ):
            reviews[r["position_id"]] = ReviewRow(
                r["position_id"], r["symbol"], r["reason"], tuple(json.loads(r["flags"]))
            )
        needs_quality = tuple(
            r["symbol"]
            for r in conn.execute(
                "SELECT symbol FROM stock_signals WHERE strategy_id = ? AND week_ending = ? "
                "AND reason = ? ORDER BY symbol",
                (STRATEGY_ID, latest, NEEDS_QUALITY),
            )
        )
    return BookData(runs, equity, held, pending, fills, reviews, needs_quality)


# ============================================================= reports
_DECISION_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_PREVIEW_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-preview\.md$")
_REFUSAL_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-(refused|preview-failed)\.md$")
#: ``weekly_run.refuse`` when not even the target week could be worked out.
_UNDATED_REFUSAL_NAME = re.compile(r"^refused-(\d{4}-\d{2}-\d{2})\.md$")
#: ``report.render_failure``'s title and its one ``**kind:** reason`` line.
_FAILURE_TITLE = re.compile(r"^# \S+ — week \S+ — NO TRADES \((?P<kind>.*)\)$")
_FAILURE_LINE = re.compile(r"^\*\*(?P<kind>.+?):\*\* (?P<reason>.*)$")
#: ``fetch._systemic``'s list of failed symbols inside the reason.
FETCH_FAILED_MARKER = " Failed: "
_NOT_IN_SCRIP_MASTER = " Not in the scrip master: "
_FAILED_SYMBOL = re.compile(r"(?:^|; )([A-Z0-9&.\-]+) \(")
#: Report kinds that record a run which decided or previewed nothing.
FAILURE_KINDS = frozenset({"refused", "preview-failed", "no-trades"})
_TITLE = re.compile(r"^# \S+ — week (\d{4}-W\d{2}) \(ending (\d{4}-\d{2}-\d{2})\)")

#: Report section headings the parsers rely on (``runtimes.positional_stocks.report``).
SECTION_FETCH = "## 0. Fetch"
SECTION_POSITIONS = "## 4. Open positions"
SECTION_CORPORATE_ACTIONS = "## 8. Corporate actions (spec 4.14)"
POSITIONS_HEADER = (
    "| Symbol | State | Shares | P1 | s | L1 | L2 | Stop / trail | Mark | Unrealised | "
    "Weeks held | Flags |"
)
SILENT_LIFT_PHRASE = "freeze lifted with neither an acknowledgement nor a rescale"
_SILENT_LIFT = re.compile(
    r"^- \*\*⚠ (?P<symbol>\S+) \((?P<position_id>[^)]+)\) — "
    + re.escape(SILENT_LIFT_PHRASE)
    + r":\*\* (?P<detail>.*)$"
)
FETCH_FAILED_PHRASE = "Fetch failed ("
_FETCH_FAILED = re.compile(
    r"^- \*\*Fetch failed \((?P<count>\d+)\), treated as stale \(spec 6\.2\):\*\* "
    r"(?P<symbols>.+)$"
)
_NO_BAR = re.compile(r"no bar (\d+) week\(s\)")


@dataclass(frozen=True)
class ReportFile:
    path: Path
    day: date
    kind: str  # "decision", "preview", "refused", "preview-failed"

    @property
    def order_key(self) -> tuple[date, float]:
        return (self.day, self.path.stat().st_mtime)


@dataclass(frozen=True)
class ParsedOpenRow:
    symbol: str
    mark: Decimal | None
    unrealised: Decimal | None
    weeks_held: int
    flags: str

    @property
    def stale_weeks(self) -> int:
        match = _NO_BAR.search(self.flags)
        return int(match.group(1)) if match else 0


@dataclass(frozen=True)
class SilentLift:
    symbol: str
    position_id: str
    detail: str


def _section(text: str, heading: str) -> list[str]:
    """The lines under ``heading`` up to the next ``## `` heading."""
    lines = text.splitlines()
    try:
        start = lines.index(heading)
    except ValueError as exc:
        raise ReportParseError(f"section “{heading}” not found") from exc
    out: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        out.append(line)
    return out


def parse_report_week(text: str) -> tuple[str, str]:
    """The (ISO week, week-ending date) of a report's title."""
    first = text.splitlines()[0] if text else ""
    match = _TITLE.match(first)
    if match is None:
        raise ReportParseError("title line not recognised")
    return match.group(1), match.group(2)


def _money_cell(cell: str) -> Decimal | None:
    if cell == "—":
        return None
    try:
        return Decimal(cell.replace(",", ""))
    except InvalidOperation as exc:
        raise ReportParseError(f"not an amount: {cell!r}") from exc


def parse_open_positions(text: str) -> dict[str, ParsedOpenRow]:
    """Section 4 of a decision report: mark, unrealised and weeks held by symbol."""
    body = [line for line in _section(text, SECTION_POSITIONS) if line.strip()]
    if body == ["None."]:
        return {}
    if not body or body[0] != POSITIONS_HEADER:
        raise ReportParseError("open-positions table header not recognised")
    out: dict[str, ParsedOpenRow] = {}
    for line in body[2:]:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 12:
            raise ReportParseError(f"open-positions row has {len(cells)} cells, expected 12")
        try:
            weeks = int(cells[10])
        except ValueError as exc:
            raise ReportParseError(f"weeks held not a number: {cells[10]!r}") from exc
        out[cells[0]] = ParsedOpenRow(
            symbol=cells[0],
            mark=_money_cell(cells[8]),
            unrealised=_money_cell(cells[9]),
            weeks_held=weeks,
            flags=cells[11],
        )
    return out


def parse_silent_lifts(text: str) -> tuple[SilentLift, ...]:
    """Section 8's "freeze lifted with neither …" lines, all weeks of the report."""
    out: list[SilentLift] = []
    for line in _section(text, SECTION_CORPORATE_ACTIONS):
        if SILENT_LIFT_PHRASE not in line:
            continue
        match = _SILENT_LIFT.match(line)
        if match is None:
            raise ReportParseError(f"silent-lift line not recognised: {line!r}")
        out.append(SilentLift(match["symbol"], match["position_id"], match["detail"]))
    return tuple(out)


def parse_fetch_failed(text: str) -> tuple[str, ...]:
    """A preview's "Fetch failed (N), treated as stale" symbols (empty if none)."""
    for line in _section(text, SECTION_FETCH):
        if FETCH_FAILED_PHRASE not in line:
            continue
        match = _FETCH_FAILED.match(line)
        if match is None:
            raise ReportParseError(f"fetch-failed line not recognised: {line!r}")
        symbols = tuple(s.strip() for s in match["symbols"].split(","))
        if len(symbols) != int(match["count"]):
            raise ReportParseError(
                f"fetch-failed count {match['count']} but {len(symbols)} symbols listed"
            )
        return symbols
    return ()


def _is_failure_report(path: Path) -> bool:
    """A ``<week_ending>.md`` written by ``render_failure`` (a decide cold cache,
    deadline or failed backup) is a failure report, not a decision."""
    try:
        with path.open(encoding="utf-8") as fh:
            first = fh.readline().rstrip("\n")
    except (OSError, UnicodeDecodeError):
        return False
    return _FAILURE_TITLE.match(first) is not None


def list_reports(reports_dir: Path) -> list[ReportFile]:
    """Every decision, preview and failure report, oldest first. Dry runs are ignored.

    Kinds: ``decision``, ``preview``, ``refused`` (``<date>-refused.md`` or
    ``refused-<date>.md``), ``preview-failed``, and ``no-trades`` (a
    ``<date>.md`` whose title is a NO TRADES failure).
    """
    if not reports_dir.is_dir():
        return []
    out: list[ReportFile] = []
    for path in reports_dir.iterdir():
        day = path.name[:10] if not path.name.startswith("refused-") else path.name[8:18]
        if match := _DECISION_NAME.match(path.name):
            kind = "no-trades" if _is_failure_report(path) else "decision"
        elif match := _PREVIEW_NAME.match(path.name):
            kind = "preview"
        elif match := _REFUSAL_NAME.match(path.name):
            kind = match.group(2)
        elif match := _UNDATED_REFUSAL_NAME.match(path.name):
            kind = "refused"
        else:
            continue
        out.append(ReportFile(path, date.fromisoformat(day), kind))
    return sorted(out, key=lambda r: r.order_key)


@dataclass(frozen=True)
class FailureInfo:
    kind: str
    reason: str


def parse_failure(text: str) -> FailureInfo:
    """``report.render_failure``: the kind in the title and the reason line."""
    lines = text.splitlines()
    title = _FAILURE_TITLE.match(lines[0]) if lines else None
    if title is None:
        raise ReportParseError("failure-report title not recognised")
    for line in lines[1:]:
        match = _FAILURE_LINE.match(line)
        if match is not None:
            if match["kind"] != title["kind"]:
                raise ReportParseError(
                    f"reason line kind {match['kind']!r} differs from the title's {title['kind']!r}"
                )
            return FailureInfo(match["kind"], match["reason"])
    raise ReportParseError("failure-report reason line not found")


def parse_failed_fetch_symbols(reason: str) -> tuple[str, ...] | None:
    """The symbols ``fetch._systemic`` lists after "Failed:"; None when it lists none."""
    start = reason.find(FETCH_FAILED_MARKER)
    if start < 0:
        return None
    segment = reason[start + len(FETCH_FAILED_MARKER) :]
    end = segment.find(_NOT_IN_SCRIP_MASTER)
    if end >= 0:
        segment = segment[:end]
    symbols = tuple(_FAILED_SYMBOL.findall(segment))
    if not symbols:
        raise ReportParseError(f"failed-symbol list not recognised: {segment[:120]!r}")
    return symbols


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ============================================================= journal
@dataclass(frozen=True)
class JournalTotals:
    trades: int
    wins: int
    losses: int
    net: Decimal
    avg_win: Decimal | None
    avg_loss: Decimal | None
    best: Decimal | None
    worst: Decimal | None

    @property
    def win_rate_pct(self) -> float | None:
        return None if not self.trades else self.wins / self.trades * 100


@dataclass(frozen=True)
class JournalView:
    rows: tuple[dict[str, str], ...] = ()
    totals: JournalTotals | None = None
    #: None when the file does not exist yet.
    exists: bool = False
    error: str | None = None


def read_journal(path: Path) -> JournalView:
    """``journal.csv``: its header must be the runtime's own column list."""
    if not path.is_file():
        return JournalView()
    try:
        text = path.read_bytes().decode("utf-8")
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        if header != JOURNAL_COLUMNS:
            raise ValueError("header does not match the journal's columns")
        rows: list[dict[str, str]] = []
        for number, record in enumerate(reader, start=2):
            if not record:
                continue
            if len(record) != len(JOURNAL_COLUMNS):
                raise ValueError(
                    f"line {number} has {len(record)} fields, expected {len(JOURNAL_COLUMNS)}"
                )
            rows.append(dict(zip(JOURNAL_COLUMNS, record, strict=True)))
        pnls = [Decimal(row["pnl_rs"]) for row in rows]
    except (OSError, UnicodeDecodeError, ValueError, InvalidOperation, csv.Error) as exc:
        return JournalView(exists=True, error=f"journal.csv could not be read: {exc}")
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    totals = JournalTotals(
        trades=len(pnls),
        wins=len(wins),
        losses=len(losses),
        net=sum(pnls, Decimal("0")),
        avg_win=sum(wins, Decimal("0")) / len(wins) if wins else None,
        avg_loss=sum(losses, Decimal("0")) / len(losses) if losses else None,
        best=max(pnls) if pnls else None,
        worst=min(pnls) if pnls else None,
    )
    return JournalView(rows=tuple(rows), totals=totals, exists=True)


# ======================================================= health extras
@dataclass(frozen=True)
class BackupInfo:
    count: int
    newest: str | None


def backups(backup_root: Path) -> BackupInfo:
    if not backup_root.is_dir():
        return BackupInfo(0, None)
    files = sorted(p.name for p in backup_root.glob(f"{RUNTIME_ID}_*.db"))
    return BackupInfo(len(files), files[-1] if files else None)


@dataclass(frozen=True)
class CacheFreshness:
    index_last_session: date | None = None
    expected_session: date | None = None
    expected_week: str | None = None
    error: str | None = None

    @property
    def fresh(self) -> bool | None:
        if self.index_last_session is None or self.expected_session is None:
            return None
        return self.index_last_session >= self.expected_session


def latest_complete_week(moment: datetime, calendar: TradingCalendar) -> WeekKey:
    """``weekly_run.target_week``: the latest ISO week complete as of ``moment``.

    The same loop over the runtime's own ``is_week_complete``;
    ``weekly_run`` itself cannot be imported here (it loads AuthBootstrap). A
    test holds the two equal.
    """
    week = week_of(moment.date())
    for _ in range(8):
        if is_week_complete(calendar.expected_last_session(week), moment, calendar=calendar):
            return week
        week = shift(week, -1)
    raise ValueError(f"no complete week found before {moment.isoformat()}")


def latest_expected_decided_week(
    moment: datetime, calendar: TradingCalendar, config: ConfigView
) -> WeekKey:
    """The newest complete week whose decide slot plus :data:`DECIDE_GRACE`
    has passed at ``moment`` — the week the book should have decided by now.
    From a week's close until its slot + grace, the week before is expected."""
    week = latest_complete_week(moment, calendar)
    for _ in range(8):
        if moment > config.decide_after(calendar.expected_last_session(week)) + DECIDE_GRACE:
            return week
        week = shift(week, -1)
    raise ValueError(f"no decided week expected before {moment.isoformat()}")


def behind_warning(
    book: BookData, config: ConfigView, config_root: Path, now: datetime
) -> str | None:
    """ "Book is behind: …" when the last COMPLETED week is older than the week
    whose decide slot (+ grace) has passed; a message when it cannot be checked."""
    try:
        calendar = TradingCalendar.from_config(config_root)
        expected = latest_expected_decided_week(now, calendar, config)
    except Exception as exc:
        return f"Cannot check whether the book is behind: {exc}"
    completed = book.latest_completed
    last = week_key(completed.iso_week) if completed is not None else None
    if last is not None and last >= expected:
        return None
    assert config.decide_slot is not None
    day = WEEKDAYS[config.decide_slot[0]].capitalize()
    return (
        f"Book is behind: last decided {completed.iso_week if completed else 'no week yet'}, "
        f"expected {week_text(expected)} — the {day} decide run did not complete; see Health."
    )


def cache_freshness(cache_root: Path, config_root: Path, now: datetime) -> CacheFreshness:
    try:
        bars = DailyBarCache.under(cache_root).read(INDEX_SYMBOL)
        calendar = TradingCalendar.from_config(config_root)
        week = latest_complete_week(now, calendar)
        expected = calendar.expected_last_session(week)
    except Exception as exc:
        return CacheFreshness(error=f"could not check the cache: {type(exc).__name__}: {exc}")
    return CacheFreshness(
        index_last_session=bars[-1].session if bars else None,
        expected_session=expected,
        expected_week=f"{week[0]}-W{week[1]:02d}",
    )


# ==================================================== operator actions
@dataclass(frozen=True)
class ActionItem:
    label: str
    #: None when the source could not be read — never shown as 0.
    count: int | None
    items: tuple[str, ...]
    what_to_do: str
    error: str | None = None


# ======================================================== positions view
@dataclass(frozen=True)
class PositionView:
    position: Position
    mark: Decimal | None
    unrealised: Decimal | None
    unrealised_pct: Decimal | None
    weeks_held: int | None
    frozen: bool
    escalated: bool
    review_reason: str | None
    stale_weeks: int


# =============================================================== view
@dataclass(frozen=True)
class StocksView:
    state: str
    state_detail: str
    config: ConfigView
    agents: dict[str, bool]
    book: BookData | None = None
    positions: tuple[PositionView, ...] = ()
    #: Why marks are "—" (no report for the latest completed week, or unreadable).
    marks_note: str | None = None
    committed_held: Decimal | None = None
    pending_entries: int = 0
    actions: tuple[ActionItem, ...] = ()
    latest_decision: ReportFile | None = None
    latest_preview: ReportFile | None = None
    #: The preview shown under "Latest report": only when newer than the decision.
    newer_preview: ReportFile | None = None
    #: Every failure report (refused, preview-failed, no-trades), oldest first.
    refusals: tuple[ReportFile, ...] = ()
    #: Every preview, oldest first (listed before the first book).
    previews: tuple[ReportFile, ...] = ()
    #: R10-1: a failed fetch newer than the newest preview and decision.
    fetch_failure: ReportFile | None = None
    fetch_failure_reason: str | None = None
    #: R10-2: "<file>: <reason>" for failures newer than the last COMPLETED
    #: run, and the "book is behind" line.
    warnings: tuple[str, ...] = ()
    journal: JournalView = field(default_factory=JournalView)
    backups: BackupInfo = field(default_factory=lambda: BackupInfo(0, None))
    cache: CacheFreshness = field(default_factory=CacheFreshness)

    @property
    def started(self) -> bool:
        return self.state != NOT_STARTED_STATE

    @property
    def last_preview_label(self) -> str:
        if self.fetch_failure is not None:
            return f"FAILED — {self.fetch_failure.path.name}"
        return self.latest_preview.path.name if self.latest_preview is not None else "none yet"


@dataclass(frozen=True)
class StocksPaths:
    project_root: Path
    launch_agents_dir: Path

    @property
    def config_root(self) -> Path:
        return self.project_root / "config"

    @property
    def database(self) -> Path:
        return self.project_root / "data" / "operational" / f"{RUNTIME_ID}.db"

    @property
    def reports(self) -> Path:
        return self.project_root / "data" / "reports" / RUNTIME_ID

    @property
    def backups(self) -> Path:
        return self.project_root / "data" / "backups"

    @property
    def cache_root(self) -> Path:
        return self.project_root / "data" / "cache"


def _classify(result: SnapshotUnavailable) -> tuple[str, str]:
    lowered = result.reason.lower()
    if "locked" in lowered or "busy" in lowered:
        return BUSY_STATE, BUSY
    return UNREADABLE_STATE, f"The book could not be read: {result.reason}"


def _could_not(item: str, report: ReportFile, exc: Exception) -> str:
    return f"could not read {item} from {report.path.name}: {exc}"


def load_view(
    paths: StocksPaths,
    *,
    now: Callable[[], datetime] = lambda: timeutils.now_tz(timeutils.DEFAULT_TZ),
) -> StocksView:
    """Everything the page shows. Never raises for a missing, locked or corrupt input."""
    config = load_config_view(paths.config_root)
    agents = agents_installed(paths.launch_agents_dir)
    reports = list_reports(paths.reports)
    decisions = [r for r in reports if r.kind == "decision"]
    previews = [r for r in reports if r.kind == "preview"]
    failures = tuple(r for r in reports if r.kind in FAILURE_KINDS)
    latest_decision = decisions[-1] if decisions else None
    latest_preview = previews[-1] if previews else None
    fetch_failure = _fetch_failure(reports, latest_preview, latest_decision)
    fetch_failure_reason = _failure_reason(fetch_failure) if fetch_failure is not None else None
    if not paths.database.is_file():
        return StocksView(
            NOT_STARTED_STATE,
            NOT_STARTED,
            config,
            agents,
            latest_preview=latest_preview,
            refusals=failures,
            previews=tuple(previews),
            fetch_failure=fetch_failure,
            fetch_failure_reason=fetch_failure_reason,
        )

    capital = config.capital if config.capital is not None else RulesParameters().capital
    result = run_bounded(paths.database, lambda conn: _read_book(conn, capital))
    if isinstance(result, SnapshotUnavailable):
        state, detail = _classify(result)
        book: BookData | None = None
    else:
        state, detail, book = OK, "", result

    newer_preview = (
        latest_preview
        if latest_preview is not None
        and (latest_decision is None or latest_preview.order_key > latest_decision.order_key)
        else None
    )
    moment = now()
    positions, marks_note, actions = _positions_and_actions(
        book, latest_decision, newer_preview, fetch_failure
    )
    warnings: list[str] = []
    if book is not None:
        warnings += [
            f"{r.path.name}: {_failure_reason(r)}"
            for r in reversed(failures)
            if r != fetch_failure and _newer_than_completed(r, book.latest_completed)
        ]
        behind = behind_warning(book, config, paths.config_root, moment)
        if behind is not None:
            warnings.append(behind)
    committed_held = (
        sum((p.committed for p in book.held), Decimal("0")) if book is not None else None
    )
    pending_entries = (
        sum(1 for o in book.pending if o.action == OrderAction.BUY_T1.value) if book else 0
    )
    return StocksView(
        state=state,
        state_detail=detail,
        config=config,
        agents=agents,
        book=book,
        positions=positions,
        marks_note=marks_note,
        committed_held=committed_held,
        pending_entries=pending_entries,
        actions=actions,
        latest_decision=latest_decision,
        latest_preview=latest_preview,
        newer_preview=newer_preview,
        refusals=failures,
        previews=tuple(previews),
        fetch_failure=fetch_failure,
        fetch_failure_reason=fetch_failure_reason,
        warnings=tuple(warnings),
        journal=read_journal(paths.reports / "journal.csv"),
        backups=backups(paths.backups),
        cache=cache_freshness(paths.cache_root, paths.config_root, moment),
    )


def _fetch_failure(
    reports: list[ReportFile], preview: ReportFile | None, decision: ReportFile | None
) -> ReportFile | None:
    """R10-1: the newest failed fetch, when newer than both the newest preview
    and the newest decision report (a later success clears it)."""
    failed = [r for r in reports if r.kind == "preview-failed"]
    if not failed:
        return None
    newest = failed[-1]
    for other in (preview, decision):
        if other is not None and other.order_key >= newest.order_key:
            return None
    return newest


def _failure_reason(report: ReportFile) -> str:
    try:
        info = parse_failure(_read_text(report.path))
    except (OSError, UnicodeDecodeError, ReportParseError) as exc:
        return f"could not read the reason from {report.path.name}: {exc}"
    return f"{info.kind} — {info.reason}"


def _newer_than_completed(report: ReportFile, completed: RunRow | None) -> bool:
    """After the last COMPLETED run: a later day, or the same day written after it finished."""
    if completed is None:
        return True
    ending = date.fromisoformat(completed.week_ending)
    if report.day != ending:
        return report.day > ending
    finished = datetime.fromisoformat(completed.finished_at) if completed.finished_at else None
    return finished is None or report.path.stat().st_mtime > finished.timestamp()


def _positions_and_actions(
    book: BookData | None,
    decision: ReportFile | None,
    preview: ReportFile | None,
    fetch_failure: ReportFile | None = None,
) -> tuple[tuple[PositionView, ...], str | None, tuple[ActionItem, ...]]:
    if book is None:
        return (), None, ()
    completed = book.latest_completed

    # --- the decision report: marks (section 4) and silent lifts (section 8)
    parsed: dict[str, ParsedOpenRow] | None = None
    marks_note: str | None = None
    lifts: tuple[SilentLift, ...] | None = None
    lifts_error: str | None = None
    stale_error: str | None = None
    if decision is None:
        marks_note = "no decision report yet"
        lifts_error = "no decision report to read silent freeze lifts from"
        stale_error = "no decision report to read stale symbols from"
    else:
        try:
            text = _read_text(decision.path)
        except (OSError, UnicodeDecodeError) as exc:
            text = None
            marks_note = lifts_error = stale_error = _could_not("the report", decision, exc)
        if text is not None:
            try:
                week, _ = parse_report_week(text)
                if completed is None or week != completed.iso_week:
                    marks_note = (
                        f"no report for week {completed.iso_week if completed else '—'} "
                        f"(the newest decision report, {decision.path.name}, is for {week})"
                    )
                    stale_error = marks_note
                else:
                    parsed = parse_open_positions(text)
            except ReportParseError as exc:
                marks_note = stale_error = _could_not("open positions", decision, exc)
            try:
                lifts = parse_silent_lifts(text)
            except ReportParseError as exc:
                lifts_error = _could_not("silent freeze lifts", decision, exc)

    views: list[PositionView] = []
    for position in book.held:
        row = parsed.get(position.symbol) if parsed is not None else None
        review = book.reviews.get(position.position_id)
        cost = position.average_cost * position.shares_held
        pct = (
            (row.unrealised / cost * 100).quantize(Decimal("0.01"))
            if row is not None and row.unrealised is not None and cost
            else None
        )
        views.append(
            PositionView(
                position=position,
                mark=row.mark if row else None,
                unrealised=row.unrealised if row else None,
                unrealised_pct=pct,
                weeks_held=row.weeks_held if row else None,
                frozen=review.frozen if review else False,
                escalated=review.escalated if review else False,
                review_reason=review.reason if review else None,
                stale_weeks=row.stale_weeks if row else 0,
            )
        )
    if parsed is not None:
        missing = [p.symbol for p in book.held if p.symbol not in parsed]
        if missing:
            marks_note = (
                f"{', '.join(missing)} not in {decision.path.name if decision else 'the report'}"
            )

    # --- operator actions
    frozen = [r for r in book.reviews.values() if r.frozen]
    escalated = [r for r in frozen if r.escalated]
    actions = [
        ActionItem(
            "Frozen positions",
            len(frozen),
            tuple(f"{r.symbol} ({r.position_id}): {r.reason}" for r in frozen),
            "Confirm the corporate action in config/positional_stocks/corporate_actions.csv, or "
            "acknowledge a real move in gap_acknowledgements.csv (the report gives both lines).",
        ),
        ActionItem(
            "Escalated freezes",
            len(escalated),
            tuple(f"{r.symbol}: {r.flags[-1]}" for r in escalated),
            "Resolve now: the position has been frozen for several weekly runs (spec 4.14).",
        ),
        ActionItem(
            "Silent freeze lifts",
            None if lifts is None else len(lifts),
            tuple(f"{x.symbol} ({x.position_id}): {x.detail}" for x in lifts or ()),
            "Check the symbol's history: a freeze lifted with neither an acknowledgement nor a "
            "rescale (spec 4.14 item 7).",
            error=lifts_error,
        ),
        ActionItem(
            "Stale held symbols (2+ weeks without a bar)",
            None if parsed is None else sum(1 for r in parsed.values() if r.stale_weeks >= 2),
            tuple(f"{r.symbol}: {r.flags}" for r in (parsed or {}).values() if r.stale_weeks >= 2),
            "A suspended stock cannot hit its stop: check the exchange for the symbol's status.",
            error=None if parsed is not None else stale_error,
        ),
        ActionItem(
            "Candidates waiting for a quality row",
            len(book.needs_quality),
            book.needs_quality,
            "Add a row for each to config/positional_stocks/quality_gate.csv before the Monday "
            "decide run.",
        ),
    ]
    if fetch_failure is not None:
        actions.append(_failed_fetch_action(fetch_failure))
    elif preview is None:
        actions.append(
            ActionItem(
                "Failed fetch symbols",
                0,
                (),
                "No preview newer than the last decision run.",
            )
        )
    else:
        try:
            failed: tuple[str, ...] | None = parse_fetch_failed(_read_text(preview.path))
            error = None
        except (OSError, UnicodeDecodeError, ReportParseError) as exc:
            failed, error = None, _could_not("failed fetch symbols", preview, exc)
        actions.append(
            ActionItem(
                "Failed fetch symbols",
                None if failed is None else len(failed),
                failed or (),
                "Fix the fetch and re-run it before the decide run: a failed symbol is treated "
                "as stale.",
                error=error,
            )
        )
    return tuple(views), marks_note, tuple(actions)


def _failed_fetch_action(report: ReportFile) -> ActionItem:
    """R10-1: the symbols a failed fetch lists, or unknown — never 0."""
    what = "Fix the cause (see Health) and re-run the fetch before the decide run."
    label = "Failed fetch symbols"
    try:
        info = parse_failure(_read_text(report.path))
        symbols = parse_failed_fetch_symbols(info.reason)
    except (OSError, UnicodeDecodeError, ReportParseError) as exc:
        return ActionItem(
            label, None, (), what, error=_could_not("failed fetch symbols", report, exc)
        )
    if symbols is None:
        return ActionItem(
            label,
            None,
            (),
            what,
            error=f"unknown: {report.path.name} does not list the symbols ({info.kind})",
        )
    return ActionItem(label, len(symbols), symbols, what)


def total_actions(actions: tuple[ActionItem, ...]) -> int | None:
    """The operator-action total, or None when any source was unreadable.

    Escalated freezes are a subset of frozen ones and are not added twice,
    matching ``telegram_summary.OperatorActions.total``.
    """
    if any(a.count is None for a in actions):
        return None
    return sum(a.count or 0 for a in actions if a.label != "Escalated freezes")
