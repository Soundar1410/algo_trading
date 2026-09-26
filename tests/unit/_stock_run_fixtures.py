"""A synthetic project root for the weekly-run tests (Phase 4b-1).

The real ``config/global.yaml`` (so the calendar is the verified NSE one), the
operator CSVs, and a warm symbol-keyed daily cache written through
:class:`DailyBarCache` — NIFTY plus three symbols. Nothing touches the real
``data/`` tree.

An entry cannot be engineered from synthetic prices through the real Stoch
RSI, so a trade is **seeded**: week W0 is recorded COMPLETED with a pending
BUY_T1 for W1's first session, exactly as a real W0 decision would leave it.
From there every fill, stop and exit is the real run's own.
"""

from __future__ import annotations

import math
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from common.notifications.base import Notifier, RecordingNotifier
from runtimes.positional_stocks.database import open_stock_database
from runtimes.positional_stocks.repository import StockRepository
from runtimes.positional_stocks.run_config import RunConfig
from runtimes.positional_stocks.weekly_run import RunEnvironment, main
from strategies.positional_stocks.wsr1_weekly_stochrsi.daily_cache import DailyBarCache
from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import WeekKey, shift
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    DailyBar,
    OrderAction,
    PendingOrder,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import sizing
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar

REPO = Path(__file__).resolve().parents[2]
IST = ZoneInfo("Asia/Kolkata")
STRATEGY = "wsr1_weekly_stochrsi"
#: The seeded entry's week; W1 = the week its T1 fills.
W0: WeekKey = (2026, 22)
HISTORY_START = date(2024, 1, 1)
HISTORY_END = date(2026, 9, 25)

UNIVERSE = (
    "symbol,isin,company,industry,nifty100,group,on_exit,as_of\n"
    "A,INE000000001,Alpha Ltd.,Capital Goods,true,,hold,2026-07-22\n"
    "B,INE000000002,Beta Ltd.,Chemicals,true,,hold,2026-07-22\n"
    "C,INE000000003,Gamma Ltd.,Services,false,,hold,2026-07-22\n"
)
QUALITY = (
    "symbol,status,checked_on,valid_until,notes\n"
    "A,PASS,2026-01-01,2099-01-01,\n"
    "B,PASS,2026-01-01,2099-01-01,\n"
    "C,PASS,2026-01-01,2099-01-01,\n"
)


def week(n: int) -> WeekKey:
    """W0 + n."""
    return shift(W0, n)


PriceFn = Callable[[date], tuple[float, float]]


def wobble(base: float, amplitude: float = 0.004) -> PriceFn:
    """A gently moving series around ``base`` (open, close), never gapping."""

    def fn(day: date) -> tuple[float, float]:
        x = day.toordinal()
        close = base * (1 + amplitude * math.sin(x / 3.0))
        open_ = base * (1 + amplitude * math.sin((x - 1) / 3.0))
        return round(open_, 2), round(close, 2)

    return fn


@dataclass
class Root:
    path: Path
    calendar: TradingCalendar
    notifier: Notifier = field(default_factory=RecordingNotifier)
    output: list[str] = field(default_factory=list)
    config: RunConfig = field(default_factory=RunConfig)

    @classmethod
    def create(cls, tmp: Path) -> Root:
        (tmp / "config" / "positional_stocks").mkdir(parents=True)
        shutil.copy(REPO / "config" / "global.yaml", tmp / "config" / "global.yaml")
        stock = tmp / "config" / "positional_stocks"
        (stock / "universe.csv").write_text(UNIVERSE)
        (stock / "quality_gate.csv").write_text(QUALITY)
        (stock / "results_calendar.csv").write_text("symbol,results_date\n")
        (stock / "gap_acknowledgements.csv").write_text(
            "symbol,gap_session,ratio,acknowledged_on,note\n"
        )
        (stock / "corporate_actions.csv").write_text(
            "symbol,ex_session,kind,ratio,confirmed_on,note\n"
        )
        return cls(tmp, TradingCalendar.from_config(tmp / "config"))

    # -------------------------------------------------------------- data
    @property
    def cache(self) -> DailyBarCache:
        return DailyBarCache.under(self.path / "data" / "cache")

    @property
    def db(self) -> Path:
        return self.path / "data" / "operational" / "positional_stocks.db"

    @property
    def reports(self) -> Path:
        return self.path / "data" / "reports" / "positional_stocks"

    @property
    def backups(self) -> Path:
        return self.path / "data" / "backups"

    def sessions(self, until: date = HISTORY_END) -> list[date]:
        day, out = HISTORY_START, []
        while day <= until:
            if self.calendar.is_trading_day(day):
                out.append(day)
            day += timedelta(days=1)
        return out

    def write(
        self, symbol: str, fn: PriceFn, *, until: date = HISTORY_END, skip: set[date] | None = None
    ) -> None:
        bars = []
        for day in self.sessions(until):
            if skip and day in skip:
                continue
            open_, close = fn(day)
            bars.append(
                DailyBar(
                    day, open_, max(open_, close) * 1.002, min(open_, close) * 0.998, close, 5e6
                )
            )
        self.cache.write(symbol, bars, fetched_at=datetime(2026, 9, 26, tzinfo=UTC))

    def standard_cache(self, a: PriceFn | None = None) -> None:
        self.write("NIFTY", lambda d: (20000.0 + (d.toordinal() % 7), 20000.0 + d.toordinal() % 5))
        self.write("A", a or wobble(1000.0))
        self.write("B", wobble(800.0))
        self.write("C", wobble(500.0))

    # ------------------------------------------------------------ weeks
    def last_session(self, n: int) -> date:
        return self.calendar.expected_last_session(week(n))

    def first_session(self, n: int) -> date:
        return self.calendar.first_session(week(n))

    def as_of(self, n: int) -> str:
        return self.last_session(n).isoformat()

    # ----------------------------------------------------------- seeding
    def seed_entry(
        self,
        symbol: str = "A",
        *,
        industry: str = "Capital Goods",
        execute_on: date | None = None,
        mark_week: bool = True,
    ) -> None:
        """W0 COMPLETED, with a BUY_T1 of ``symbol`` for W1's first session
        (or ``execute_on``)."""
        self.db.parent.mkdir(parents=True, exist_ok=True)
        database = open_stock_database(self.db)
        repo = StockRepository(database, STRATEGY, self.config.params.capital)
        ending = self.last_session(0)
        if mark_week:
            repo.mark_started(ending, W0, "seed")
        size = sizing(0.06, False, self.config.params)
        order = PendingOrder(
            action=OrderAction.BUY_T1,
            symbol=symbol,
            decided_week=W0,
            execute_on_or_after=execute_on or self.calendar.next_session_after(ending),
            reason="entry: seeded",
            amount=size.tranche_amounts[0],
            sizing=size,
            sector=industry,
            group=symbol,
        )
        with database.transaction(immediate=True) as conn:
            repo.save_order(conn, order)
            if mark_week:
                repo.mark_completed(conn, ending)
        database.close()

    def repo(self) -> StockRepository:
        return StockRepository(open_stock_database(self.db), STRATEGY, self.config.params.capital)

    # --------------------------------------------------------------- run
    def env(self, **overrides: object) -> RunEnvironment:
        env = RunEnvironment(
            project_root=self.path,
            now=lambda: datetime.combine(HISTORY_END, time(20, 0), IST),
            notifier=self.notifier,
            preflight=lambda: [],
            out=self.output.append,
            config=self.config,
        )
        for key, value in overrides.items():
            setattr(env, key, value)
        return env

    def run(self, *argv: str, **overrides: object) -> int:
        return main(["--mode", "decide", *argv], self.env(**overrides))

    def report(self, n: int, *, dry_run: bool = False) -> str:
        suffix = "-dry-run" if dry_run else ""
        return (self.reports / f"{self.last_session(n)}{suffix}.md").read_text()


def falling(start: date, *, rate: float = 0.04, base: float = 1000.0) -> PriceFn:
    """Flat at ``base`` until ``start``, then down ``rate`` per weekday — a
    smooth fall, never a 15% close-to-close gap."""

    def fn(day: date) -> tuple[float, float]:
        if day < start:
            return base, base
        n = _weekdays(start, day)
        return round(base * (1 - rate) ** (n - 1), 2), round(base * (1 - rate) ** n, 2)

    return fn


def _weekdays(start: date, day: date) -> int:
    """Weekdays from ``start`` to ``day``, inclusive."""
    n, d = 0, start
    while d <= day:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n
