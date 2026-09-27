"""Phase 4b-2 Part B: ``weekly_run --mode fetch`` end to end, Dhan faked.

Every test runs the real CLI entry point against a temp project root with the
real calendar. Authentication, the scrip master and the HTTP transport are
fakes (:class:`FakeDhan`), and the throttle's clock and sleep are fake too, so
nothing here reaches the network or sleeps for real. The fake serves Dhan's
wire shape (parallel arrays, epoch seconds, an exclusive ``toDate``), so the
real ``DhanHistoricalDataClient``, parser and cache do all the work.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from _stock_run_fixtures import IST, QUALITY, UNIVERSE, PriceFn, Root, wobble
from filelock import FileLock

from common.notifications.base import RecordingNotifier
from runtimes.positional_stocks import fetch, weekly_run
from runtimes.positional_stocks.fetch import Credentials, FetchRefused, FetchServices
from runtimes.positional_stocks.weekly_run import (
    EXIT_DEADLINE,
    EXIT_FETCH_FAILED,
    EXIT_LOCKED,
    EXIT_NO_TRADES,
    EXIT_OK,
    EXIT_PREVIEW_PARTIAL,
    EXIT_REFUSED,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.daily_cache import FULL_HISTORY_FROM
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import DailyBar

FRIDAY = date(2026, 9, 25)  # the fixture's HISTORY_END: 2026-W39's last session
W38_END = date(2026, 9, 18)
THIS_MONTH = "2026-09-01T18:00:00+05:30"
LAST_MONTH = "2026-08-28T18:00:00+05:30"


@dataclass
class Request:
    symbol: str
    start: date
    #: Inclusive: Dhan's toDate is exclusive, so this is toDate - 1 day.
    end: date
    at: float


@dataclass
class FakeDhan:
    """Dhan's daily endpoint, the scrip master and a login, all in memory."""

    root: Root
    prices: dict[str, PriceFn]
    #: Symbols absent from the scrip master.
    unlisted: set[str] = field(default_factory=set)
    #: Symbols whose every request fails (HTTP 500: transient, retried).
    failing: set[str] = field(default_factory=set)
    #: Sessions Dhan has not published, per symbol.
    missing: dict[str, set[date]] = field(default_factory=dict)
    #: (symbol, before, factor): Dhan's back-adjustment of a corporate action.
    restated: list[tuple[str, date, float]] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    clock: float = 0.0
    on_request: Callable[[FakeDhan], None] | None = None

    def ids(self) -> dict[str, str]:
        return {s: str(100 + i) for i, s in enumerate(sorted(self.prices)) if s != "NIFTY"}

    # ------------------------------------------------------------- services
    def services(
        self, *, auth: Callable[[float, bool], Credentials] | None = None
    ) -> FetchServices:
        return FetchServices(
            authenticate=auth
            or (lambda minimum, allow: Credentials("CID", "TOKEN", "cache", 50_000.0)),
            scrip_master_text=self.master,
            http_post=self.post,
            sleep=self.sleep,
            monotonic=lambda: self.clock,
        )

    def sleep(self, seconds: float) -> None:
        self.clock += seconds

    def master(self) -> str:
        out = io.StringIO()
        columns = [
            "SEM_EXM_EXCH_ID",
            "SEM_INSTRUMENT_NAME",
            "SEM_SERIES",
            "SEM_TRADING_SYMBOL",
            "SEM_CUSTOM_SYMBOL",
            "SEM_SMST_SECURITY_ID",
            "SEM_TICK_SIZE",
        ]
        writer = csv.DictWriter(out, fieldnames=columns)
        writer.writeheader()
        writer.writerow(
            dict(zip(columns, ["NSE", "INDEX", "", "NIFTY", "Nifty 50", "13", "5"], strict=True))
        )
        writer.writerow(
            dict(
                zip(columns, ["NSE", "INDEX", "", "NIFTY 100", "Nifty 100", "17", "5"], strict=True)
            )
        )
        for symbol, security_id in self.ids().items():
            if symbol not in self.unlisted:
                row = ["NSE", "EQUITY", "EQ", symbol, f"{symbol} Ltd", security_id, "5"]
                writer.writerow(dict(zip(columns, row, strict=True)))
        return out.getvalue()

    # ------------------------------------------------------------ transport
    def post(self, endpoint: str, *, json: dict[str, Any], **_: object) -> httpx.Response:
        by_id = {v: k for k, v in self.ids().items()} | {"13": "NIFTY"}
        symbol = by_id[json["securityId"]]
        start = date.fromisoformat(json["fromDate"])
        end = date.fromisoformat(json["toDate"]) - timedelta(days=1)
        self.requests.append(Request(symbol, start, end, self.clock))
        if self.on_request is not None:
            self.on_request(self)
        if symbol in self.failing:
            return httpx.Response(500, json={"errorMessage": "upstream timeout"})
        bars = [b for b in self.truth(symbol) if start <= b.session <= end]
        stamps = [datetime.combine(b.session, time(9, 15), IST).timestamp() for b in bars]
        return httpx.Response(
            200,
            json={
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
                "timestamp": stamps,
            },
        )

    def truth(self, symbol: str) -> list[DailyBar]:
        missing = self.missing.get(symbol, set())
        out = []
        for day in self.root.sessions():
            if day in missing:
                continue
            open_, close = self.prices[symbol](day)
            f = 1.0
            for name, before, factor in self.restated:
                if name == symbol and day < before:
                    f *= factor
            open_, close = round(open_ * f, 2), round(close * f, 2)
            out.append(
                DailyBar(
                    day, open_, max(open_, close) * 1.002, min(open_, close) * 0.998, close, 5e6
                )
            )
        return out

    def of(self, symbol: str) -> list[Request]:
        return [r for r in self.requests if r.symbol == symbol]


def _prices() -> dict[str, PriceFn]:
    return {
        "NIFTY": lambda d: (20000.0 + (d.toordinal() % 7), 20000.0 + d.toordinal() % 5),
        "A": wobble(1000.0),
        "B": wobble(800.0),
        "C": wobble(500.0),
    }


def _setup(tmp_path: Path, **kw: Any) -> tuple[Root, FakeDhan]:
    root = Root.create(tmp_path)
    dhan = FakeDhan(root, _prices(), **kw)
    return root, dhan


def _warm(root: Root, dhan: FakeDhan, until: date, stamp: str | None = THIS_MONTH) -> None:
    """The local cache as a previous fetch left it: Dhan's truth up to ``until``."""
    for symbol in dhan.prices:
        bars = [b for b in dhan.truth(symbol) if b.session <= until]
        root.cache.write(
            symbol, bars, fetched_at=datetime(2026, 9, 1, tzinfo=IST), full_history_at=stamp
        )


def _fetch(root: Root, dhan: FakeDhan, *argv: str, **overrides: Any) -> int:
    overrides.setdefault("fetch_services", dhan.services())
    return weekly_run.main(["--mode", "fetch", *argv], root.env(**overrides))


def _preview(root: Root, end: date = FRIDAY) -> Path:
    return root.reports / f"{end}-preview.md"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ============================================================ the refresh
def test_a_cold_cache_is_fetched_in_full_and_the_preview_written(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path)
    assert _fetch(root, dhan) == EXIT_OK
    assert [r.symbol for r in dhan.requests] == ["NIFTY", "A", "B", "C"]  # NIFTY first
    assert {(r.start, r.end) for r in dhan.requests} == {(FULL_HISTORY_FROM, FRIDAY)}
    for symbol in dhan.prices:
        assert root.cache.read(symbol) == dhan.truth(symbol)
        assert root.cache.metadata(symbol)["full_history_at"] is not None
    text = _preview(root).read_text()
    assert "— PREVIEW" in text and "**PREVIEW**" in text and "## 0. Fetch" in text
    assert "tail 0, full 4 (of which restated 0), failed 0" in text
    assert "NIFTY 50 sessions in 2026-W39: 2026-09-21, 2026-09-22" in text
    assert not root.db.exists() and not root.backups.exists()
    assert isinstance(root.notifier, RecordingNotifier)
    (event,) = root.notifier.events
    assert event.event_type == "weekly_preview" and event.message.startswith("PREVIEW — ")
    assert "fetch failed 0" in event.message


def test_a_tail_refetch_overlaps_ten_sessions_and_merges(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path)
    _warm(root, dhan, W38_END)
    assert _fetch(root, dhan) == EXIT_OK
    for symbol in dhan.prices:
        (request,) = dhan.of(symbol)
        cached_before = [b.session for b in dhan.truth(symbol) if b.session <= W38_END]
        assert (request.start, request.end) == (cached_before[-10], FRIDAY)
        assert root.cache.read(symbol) == dhan.truth(symbol)
        assert root.cache.metadata(symbol)["full_history_at"] == THIS_MONTH  # kept
    assert "tail 4, full 0" in _preview(root).read_text()


def test_an_overlap_mismatch_discards_the_series_and_refetches_it_in_full(
    tmp_path: Path,
) -> None:
    root, dhan = _setup(tmp_path)
    _warm(root, dhan, W38_END)
    # A 1:1 bonus ex 2026-09-21: Dhan now serves everything before it halved.
    dhan.restated.append(("A", date(2026, 9, 21), 0.5))
    assert _fetch(root, dhan) == EXIT_OK
    tail, full = dhan.of("A")
    assert tail.start > FULL_HISTORY_FROM and (full.start, full.end) == (FULL_HISTORY_FROM, FRIDAY)
    assert root.cache.read("A") == dhan.truth("A")
    text = _preview(root).read_text()
    assert "tail 3, full 1 (of which restated 1)" in text
    assert "Restated and refetched in full: A" in text


@pytest.mark.parametrize("stamp", [LAST_MONTH, None])
def test_the_first_fetch_of_a_month_refetches_everything(tmp_path: Path, stamp: str | None) -> None:
    root, dhan = _setup(tmp_path)
    _warm(root, dhan, W38_END, stamp=stamp)  # None: written before Phase 4b-2
    assert _fetch(root, dhan) == EXIT_OK
    assert {(r.start, r.end) for r in dhan.requests} == {(FULL_HISTORY_FROM, FRIDAY)}
    assert (
        root.cache.metadata("A")["full_history_at"]
        == datetime(2026, 9, 25, 20, 0, tzinfo=IST).isoformat()
    )


def test_force_refetch_refetches_everything_even_when_current(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path)
    assert _fetch(root, dhan) == EXIT_OK
    dhan.requests.clear()
    assert _fetch(root, dhan) == EXIT_OK  # current and previewed: nothing
    assert dhan.requests == []
    assert _fetch(root, dhan, "--force-refetch") == EXIT_OK
    assert sorted(r.symbol for r in dhan.requests) == ["A", "B", "C", "NIFTY"]
    assert {r.start for r in dhan.requests} == {FULL_HISTORY_FROM}


# ======================================================== the end-date cap
def test_a_friday_afternoon_fetch_never_requests_today(tmp_path: Path) -> None:
    """15:00 IST on Friday: 2026-W39 is not complete, so the target is W38 and
    every request ends at W38's last session — never at today's unfinished
    candle, which the next tail refetch would see as a restatement."""
    root, dhan = _setup(tmp_path)
    afternoon = datetime.combine(FRIDAY, time(15, 0), IST)
    assert _fetch(root, dhan, now=lambda: afternoon) == EXIT_OK
    assert {r.end for r in dhan.requests} == {W38_END}
    assert all(b.session <= W38_END for b in root.cache.read("NIFTY"))
    assert _preview(root, W38_END).is_file()


def test_a_holiday_friday_week_is_requested_through_its_thursday(tmp_path: Path) -> None:
    """Good Friday 2026-04-03: a Sunday fetch for W14 ends at Thursday 2 April."""
    root, dhan = _setup(tmp_path)
    sunday = datetime(2026, 4, 5, 10, 0, tzinfo=IST)
    assert _fetch(root, dhan, now=lambda: sunday) == EXIT_OK
    assert {r.end for r in dhan.requests} == {date(2026, 4, 2)}
    assert _preview(root, date(2026, 4, 2)).is_file()


# ============================================================== staleness
def test_nifty_not_yet_published_writes_no_preview_and_stops_at_once(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path, missing={"NIFTY": {FRIDAY}})
    assert _fetch(root, dhan) == EXIT_NO_TRADES
    assert [r.symbol for r in dhan.requests] == ["NIFTY"]  # one request, not the universe
    assert not _preview(root).exists()
    failed = (root.reports / f"{FRIDAY}-preview-failed.md").read_text()
    assert "not yet published" in failed and "NIFTY 50 has no session on 2026-09-25" in failed
    # Phase 5 (spec 10.3 v1.3): Friday 20:00 is not the final attempt — the
    # Saturday and Sunday ones follow — so it is reported but not alerted.
    assert root.notifier.events == []  # type: ignore[attr-defined]
    assert "a later fetch attempt is scheduled" in failed
    dhan.missing.clear()  # the next scheduled attempt: published now
    assert _fetch(root, dhan) == EXIT_OK
    assert _preview(root).is_file()


def test_a_deadline_mid_refresh_writes_no_preview_and_lists_what_was_refreshed(
    tmp_path: Path,
) -> None:
    root, dhan = _setup(tmp_path)
    clock = [0.0]

    def expire_after_a(d: FakeDhan) -> None:
        if d.requests[-1].symbol == "A":
            clock[0] = 1e6

    dhan.on_request = expire_after_a
    assert _fetch(root, dhan, monotonic=lambda: clock[0]) == EXIT_DEADLINE
    assert [r.symbol for r in dhan.requests] == ["NIFTY", "A"]
    assert not _preview(root).exists()
    failed = (root.reports / f"{FRIDAY}-preview-failed.md").read_text()
    assert "deadline exceeded" in failed and "Refreshed 2" in failed and "A, NIFTY" in failed


def test_the_throttle_never_allows_more_than_3_requests_in_a_second(tmp_path: Path) -> None:
    prices = _prices()
    prices.update({f"S{i:02d}": wobble(100.0 + i) for i in range(12)})
    root, dhan = _setup(tmp_path)
    dhan.prices = prices
    _universe(root, [f"S{i:02d}" for i in range(12)])
    dhan.failing = {"S03"}  # its retries are throttled too
    assert _fetch(root, dhan) == EXIT_PREVIEW_PARTIAL
    times = sorted(r.at for r in dhan.requests)
    assert len(times) == 4 + 12 + 5  # S03: 3 attempts, then 3 more in the second pass
    assert all(later - earlier >= 1.0 for earlier, later in zip(times, times[3:], strict=False))


# ========================================================== idempotency
def test_a_second_run_with_everything_current_exits_at_once(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path)
    assert _fetch(root, dhan) == EXIT_OK
    preview = _sha(_preview(root))
    dhan.requests.clear()
    calls: list[float] = []
    services = dhan.services(
        auth=lambda m, allow: calls.append(m) or Credentials("C", "T", "cache", 1)
    )
    assert _fetch(root, dhan, fetch_services=services) == EXIT_OK
    assert dhan.requests == [] and calls == []  # no login, no request
    assert root.output[-1].startswith("up to date")
    assert _sha(_preview(root)) == preview


def test_the_preview_never_creates_the_database(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path)
    assert _fetch(root, dhan) == EXIT_OK
    assert not root.db.exists() and not root.backups.exists()
    assert not (root.reports / "journal.csv").exists()


def test_the_preview_leaves_an_existing_database_byte_identical(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path)
    _warm(root, dhan, FRIDAY)
    root.seed_entry()
    before = _sha(root.db)
    assert _fetch(root, dhan) == EXIT_OK
    assert _sha(root.db) == before and not root.backups.exists()
    # The copy decided every week since the seed: W23 onwards.
    assert "weeks processed: 2026-W23," in _preview(root).read_text()


def test_the_lock_is_shared_with_decide(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path)
    locks = root.path / "data" / "runtime" / "locks"
    locks.mkdir(parents=True)
    with FileLock(str(locks / weekly_run.LOCK_NAME)):
        assert _fetch(root, dhan) == EXIT_LOCKED
    assert dhan.requests == [] and not root.reports.exists()


# ===================================================== failed symbols
def test_one_failed_symbol_not_held_is_skipped_listed_and_exits_6(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path, failing={"B"})
    _warm(root, dhan, W38_END)
    assert _fetch(root, dhan) == EXIT_PREVIEW_PARTIAL
    assert len(dhan.of("B")) == 6  # 3 attempts, then the second pass's 3
    text = _preview(root).read_text()
    assert "**Fetch failed (1), treated as stale (spec 6.2):** B" in text
    assert "B: HistoricalDataTransientError" in text
    assert "stale (1 week(s)) — B: last cached session 2026-09-18" in text
    assert "skipped this week" in text
    assert "FETCH FAILED: B" not in text  # not held: no highlight
    assert "preview written; 1 symbol(s) failed: B" in root.output[-1]
    message = root.notifier.events[-1].message  # type: ignore[attr-defined]
    assert "fetch failed 1" in message and "Operator actions: 1 " in message


def test_one_failed_held_symbol_is_highlighted_and_left_undecided(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path)
    _warm(root, dhan, FRIDAY)
    root.seed_entry()
    assert root.run("--as-of", W38_END.isoformat()) == EXIT_OK  # A is held from W23
    _warm(root, dhan, W38_END)
    dhan.failing = {"A"}
    assert _fetch(root, dhan) == EXIT_PREVIEW_PARTIAL
    text = _preview(root).read_text()
    head = text.split("## 0. Fetch")[0]
    assert "**⚠ FETCH FAILED: A — held — its stop cannot be checked this week.**" in head
    positions = text.split("## 4.")[1].split("## 5.")[0]
    assert "| A |" in positions and "no bar 1 week(s)" in positions
    message = root.notifier.events[-1].message  # type: ignore[attr-defined]
    assert "⚠ FETCH FAILED A (held: its stop cannot be checked)" in message


def test_eleven_failed_symbols_are_systemic_and_write_no_preview(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path)
    extra = [f"S{i:02d}" for i in range(11)]
    dhan.prices.update({s: wobble(100.0) for s in extra})
    _universe(root, extra)
    dhan.failing = set(extra)
    assert _fetch(root, dhan) == EXIT_FETCH_FAILED
    assert not _preview(root).exists()
    failed = (root.reports / f"{FRIDAY}-preview-failed.md").read_text()
    assert "11 symbols failed after the second pass (more than 10)" in failed
    assert "Refreshed 4" in failed
    assert root.notifier.events[-1].event_type == "weekly_run_no_trades"  # type: ignore[attr-defined]


def test_ten_failed_symbols_still_write_the_preview(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path)
    extra = [f"S{i:02d}" for i in range(10)]
    dhan.prices.update({s: wobble(100.0) for s in extra})
    _universe(root, extra)
    dhan.failing = set(extra)
    assert _fetch(root, dhan) == EXIT_PREVIEW_PARTIAL
    assert _preview(root).is_file()


def test_nifty_failing_is_systemic(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path, failing={"NIFTY"})
    assert _fetch(root, dhan) == EXIT_FETCH_FAILED
    assert {r.symbol for r in dhan.requests} == {"NIFTY"}
    assert not _preview(root).exists()


def test_a_retry_after_one_failure_refetches_only_that_symbol(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path, failing={"B"})
    _warm(root, dhan, W38_END)
    assert _fetch(root, dhan) == EXIT_PREVIEW_PARTIAL
    dhan.failing.clear()
    dhan.requests.clear()
    assert _fetch(root, dhan) == EXIT_OK
    ((symbol, start),) = {(r.symbol, r.start) for r in dhan.requests}
    assert symbol == "B" and start > FULL_HISTORY_FROM  # a tail
    text = _preview(root).read_text()
    assert "Fetch failed" not in text and "failed 0" in text  # rewritten
    dhan.requests.clear()
    assert _fetch(root, dhan) == EXIT_OK and dhan.requests == []


def test_a_symbol_missing_from_the_scrip_master_is_skipped_and_never_blocks(
    tmp_path: Path,
) -> None:
    root, dhan = _setup(tmp_path, unlisted={"C"})
    assert _fetch(root, dhan) == EXIT_OK
    assert "C" not in {r.symbol for r in dhan.requests}
    text = _preview(root).read_text()
    assert "Not in the scrip master, skipped (spec 5): C" in text
    dhan.requests.clear()
    assert _fetch(root, dhan) == EXIT_OK and dhan.requests == []  # still idempotent


# ============================================================ refusals
def test_a_token_without_enough_life_is_refused_before_any_request(tmp_path: Path) -> None:
    root, dhan = _setup(tmp_path)
    asked: list[float] = []

    def short(minimum: float, allow: bool) -> Credentials:
        asked.append(minimum)
        raise FetchRefused("Cached token has 0.10 h of life left")

    assert _fetch(root, dhan, fetch_services=dhan.services(auth=short)) == EXIT_REFUSED
    assert asked == [20 * 60 + fetch.TOKEN_MARGIN_SECONDS]
    assert dhan.requests == [] and not _preview(root).exists()


def test_fetch_with_dry_run_and_decide_with_force_refetch_are_usage_errors() -> None:
    with pytest.raises(SystemExit):
        weekly_run.parse(["--mode", "fetch", "--dry-run"])
    with pytest.raises(SystemExit):
        weekly_run.parse(["--mode", "decide", "--force-refetch"])


def test_anything_but_paper_is_refused_before_the_network(tmp_path: Path) -> None:
    from runtimes.positional_stocks.run_config import RunConfig

    root, dhan = _setup(tmp_path)
    root.config = RunConfig(mode="live")
    assert _fetch(root, dhan) == EXIT_REFUSED
    assert dhan.requests == []


# ================================================================ helpers
def _universe(root: Root, extra: list[str]) -> None:
    stock = root.path / "config" / "positional_stocks"
    rows = [
        f"{s},INE9{i:08d},{s} Ltd.,Services,false,,hold,2026-07-22" for i, s in enumerate(extra)
    ]
    (stock / "universe.csv").write_text(UNIVERSE + "\n".join(rows) + "\n")
    quality = [f"{s},PASS,2026-01-01,2099-01-01," for s in extra]
    (stock / "quality_gate.csv").write_text(QUALITY + "\n".join(quality) + "\n")


# ======================================= spec 4.14 in the preview (both modes)
def test_the_preview_freezes_a_raw_gap_and_prints_both_resolution_lines(
    tmp_path: Path,
) -> None:
    """A held; Dhan's W39 data carries an unadjusted 1:1 bonus ex Tuesday
    (the raw close halves). Fetch mode runs the same corporate-action checks
    as decide: the preview freezes A and prints the acknowledgement line and
    the corporate-action template."""
    root, dhan = _setup(tmp_path)
    base = wobble(1000.0)
    ex = date(2026, 9, 22)
    dhan.prices["A"] = lambda d: tuple(x / 2 for x in base(d)) if d >= ex else base(d)  # type: ignore[assignment,misc]
    _warm(root, dhan, FRIDAY)
    root.seed_entry()
    assert root.run("--as-of", W38_END.isoformat()) == EXIT_OK
    _warm(root, dhan, W38_END)
    assert _fetch(root, dhan) == EXIT_OK
    section = _preview(root).read_text().split("## 8.")[1].split("## 9.")[0]
    assert "**Frozen positions**" in section and "A (A-2026W23): factor 0.5" in section
    assert f"gap {ex} ratio 0.5" in section
    assert f"A,{ex},0.5" in section and "real move: <why>" in section  # acknowledgement
    assert f"A,{ex},BONUS_SPLIT,<new shares per old share, ~2.00" in section  # template
