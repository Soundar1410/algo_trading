"""Phase 4a: a paper book driven end to end through persistence and the rules.

One symbol, ``A``, over nine decision weeks (bars 209-217 of the shared rules
fixtures), each week a real :func:`run_decision_week` call on a real SQLite
file: every fill, decision and bookkeeping field goes through the database
between weeks. The weekly series (what the rules read) and the daily bars
(where fills happen) are set independently: each says exactly what the step
under test needs.

=====  =====================================================  =================
Bar    Weekly close (what the rules see)                      Fill at Monday open
=====  =====================================================  =================
209    trigger (K 28 > D 25 after an oversold close)          -
210    low 890 touches L1 900 in the T1 fill week             T1 40 @ 1000
211    close 950 > prior high 940: reversal -> add T2         -
212                                                           T2 32 @ 930
213    K 95 / D 92 -> partial sale of 36                      -
214    close 1080 >= 10W EMA 1000: hold                       sell 36 @ 1100
215    close 820 < 10W EMA 900: trail exit                    -
216                                                           sell 36 @ 800
217    a fresh trigger, refused: 26-week cooling-off          -
=====  =====================================================  =================

The trade loses net of costs (buys 69,760; sales 68,400), so a cooling-off
row is written and week 217's new trigger is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from _wsr1_rules_fixtures import Tape, ctx, friday, index_series, symbol_week, universe_row, week

from runtimes.positional_stocks.accounting import (
    RunOrderError,
    WeekInputs,
    WeekOutcome,
    run_decision_week,
)
from runtimes.positional_stocks.database import open_stock_database
from runtimes.positional_stocks.repository import StockRepository, week_text
from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import shift
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    DailyBar,
    FunnelStage,
    GapAcknowledgement,
    OrderAction,
    PositionState,
    RulesParameters,
    WeekDecision,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar

D = Decimal
STRATEGY = "wsr1_weekly_stochrsi"
PARAMS = RulesParameters()  # spec 8's costs are on: this is the real accounting
CALENDAR = TradingCalendar.from_holidays([])
FIRST, LAST = 209, 217


@dataclass
class World:
    """Per-bar overrides for A's weekly series, and Monday opens for fills."""

    close: dict[int, float] = field(default_factory=dict)
    high: dict[int, float] = field(default_factory=dict)
    low: dict[int, float] = field(default_factory=dict)
    k: dict[int, float] = field(default_factory=dict)
    d: dict[int, float] = field(default_factory=dict)
    ema10: dict[int, float] = field(default_factory=dict)
    monday_open: dict[int, float] = field(default_factory=dict)
    #: Weeks in which A has no daily bars at all (it did not trade).
    silent: set[int] = field(default_factory=set)
    #: Gap acknowledgements for real moves (spec 4.14 item 7 v1.2k freezes a
    #: held position on an unacknowledged close-to-close gap of 15% or more).
    acknowledgements: tuple[GapAcknowledgement, ...] = ()

    def series(self, i: int) -> Tape:
        def upto(column: dict[int, float]) -> dict[int, float | None]:
            return {j: v for j, v in column.items() if j <= i}

        return Tape(
            n=i + 1,
            close=upto(self.close),
            high=upto(self.high),
            low=upto(self.low),
            k=upto(self.k),
            d=upto(self.d),
            ema10={**{j: 950.0 for j in range(i + 1)}, **upto(self.ema10)},
        )

    def daily(self, i: int) -> list[DailyBar]:
        bars = []
        for w in range(195, i + 1):
            if w in self.silent:
                continue
            close = self.close.get(w, 1000.0)
            monday = friday(w) - timedelta(days=4)
            for offset in range(5):
                open_ = self.monday_open.get(w, close) if offset == 0 else close
                bars.append(
                    DailyBar(
                        monday + timedelta(days=offset),
                        open_,
                        max(open_, close) + 1.0,
                        min(open_, close) - 1.0,
                        close,
                        1_000_000.0,
                    )
                )
        return bars


def _trade_world() -> World:
    # The trail exit is a real -24% week (1,080 -> 820 at 215's Monday) and
    # the stock recovers +22% (820 -> 1,000 at 216's). Both are genuine moves,
    # acknowledged as an operator would; without that, item 7 (v1.2k) would
    # freeze the position until they were.
    acks = tuple(
        GapAcknowledgement("A", friday(w) - timedelta(days=4), D(r), date(2026, 9, 26), "real move")
        for w, r in ((215, "0.7593"), (216, "1.2195"))
    )
    return World(
        acknowledgements=acks,
        close={210: 900.0, 211: 950.0, 212: 960.0, 214: 1080.0, 215: 820.0},
        high={210: 940.0, 211: 960.0, 212: 970.0},
        low={210: 890.0},
        k={207: 15.0, 208: 20.0, 209: 28.0, 213: 95.0, 216: 12.0, 217: 18.0},
        d={207: 18.0, 208: 22.0, 209: 25.0, 213: 92.0, 216: 16.0, 217: 16.0},
        ema10={214: 1000.0, 215: 900.0},
        monday_open={210: 1000.0, 212: 930.0, 214: 1100.0, 216: 800.0},
    )


def _inputs(world: World, i: int, symbols: tuple[str, ...] = ("A",)) -> WeekInputs:
    rows = [universe_row(s, f"Sector-{s}") for s in symbols]
    return WeekInputs(
        ctx=ctx(i),
        symbols={
            s: symbol_week(s, world.series(i).series(), industry=f"Sector-{s}") for s in symbols
        },
        index=index_series(n=i + 1),
        daily={s: world.daily(i) for s in symbols},
        universe_rows=rows,
        acknowledgements=world.acknowledgements,
        fingerprint=f"fp-{i}",
    )


def _repo(path: Path) -> StockRepository:
    return StockRepository(open_stock_database(path), STRATEGY, PARAMS.capital)


def _run(repo: StockRepository, world: World, i: int) -> WeekOutcome:
    return run_decision_week(repo, _inputs(world, i), calendar=CALENDAR, params=PARAMS)


def _decided(repo: StockRepository, world: World, i: int) -> WeekDecision:
    outcome = _run(repo, world, i)
    assert outcome.decision is not None
    return outcome.decision


def _run_through(repo: StockRepository, world: World, last: int) -> None:
    for i in range(FIRST, last + 1):
        assert _run(repo, world, i).status == "COMPLETED"


# ================================================= end to end, week by week
def test_a_multi_week_book_end_to_end(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _trade_world()

    entry = _decided(repo, world, 209)
    assert [(o.symbol, o.action) for o in entry.orders] == [("A", OrderAction.BUY_T1)]

    _run(repo, world, 210)  # T1 fills at Monday's open, 40 x 1000 + 12 bps
    (position,) = repo.positions().values()
    assert position.shares_held == 40 and position.p1 == D("1000.00")
    assert position.buys[0].at_week_open
    assert position.touch_week == week(210)  # persisted touch memory
    assert repo.cash() == D("1000000") - D("40000") - D("48.00")

    add = _decided(repo, world, 211)
    assert [o.action for o in add.orders] == [OrderAction.BUY_T2]

    _run(repo, world, 212)
    (position,) = repo.positions().values()
    assert [b.shares for b in position.buys] == [40, 32]
    assert position.touch_week is None  # consumed by the fill

    partial = _decided(repo, world, 213)
    (order,) = partial.orders
    assert (order.action, order.quantity) == (OrderAction.SELL_HALF, 36)

    _run(repo, world, 214)
    (position,) = repo.positions().values()
    assert position.state is PositionState.HALF_SOLD and position.shares_held == 36

    trail = _decided(repo, world, 215)
    (order,) = trail.orders
    assert (order.action, order.quantity) == (OrderAction.SELL_ALL, 36)

    _run(repo, world, 216)
    (position,) = repo.positions().values()
    assert position.state is PositionState.CLOSED
    # Buys 69,760; sales 68,400; fees: 48.00 + 35.71 + (43.56 + 15) + (31.68 + 15).
    assert position.net_pnl == D("-1360") - D("48.00") - D("35.71") - D("58.56") - D("46.68")
    cooling = repo.database.connect().execute("SELECT * FROM stock_cooling_off").fetchone()
    assert cooling["until_week"] == week_text(shift(week(216), 26))

    refused = _decided(repo, world, 217)
    (funnel,) = refused.funnel
    assert funnel.stage is FunnelStage.FILTERED and "cooling-off" in funnel.reason
    assert refused.orders == ()

    # Cash is capital plus every fill; the last equity row agrees.
    assert repo.cash() == PARAMS.capital + position.net_pnl
    last = (
        repo.database.connect()
        .execute("SELECT cash FROM stock_equity ORDER BY week_ending DESC LIMIT 1")
        .fetchone()
    )
    assert D(last["cash"]) == repo.cash()

    # The persisted trigger history: A triggered at 209 and 217, not between.
    triggered = {
        row["week_ending"]: row["triggered"]
        for row in repo.database.connect().execute(
            "SELECT week_ending, triggered FROM stock_signals"
        )
    }
    assert [w for w, t in sorted(triggered.items()) if t] == [
        friday(209).isoformat(),
        friday(217).isoformat(),
    ]
    assert repo.database.foreign_key_check() == []


# ================================== an unfilled order is carried, not lost
def test_an_order_with_no_session_stays_pending_and_holds_its_place(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _trade_world()
    world.silent = {210}  # A did not trade at all in week 210
    _run(repo, world, 209)
    carried = _decided(repo, world, 210)
    assert repo.positions() == {}
    (still,) = repo.pending_orders()
    assert still.action is OrderAction.BUY_T1 and still.sizing is not None
    assert carried.orders == ()  # nothing stacked on the unfilled order

    _run(repo, world, 211)  # fills at 211's Monday open, flagged
    (position,) = repo.positions().values()
    assert position.buys[0].session == friday(211) - timedelta(days=4)
    flag = (
        repo.database.connect()
        .execute("SELECT not_traded_on_execution_session, at_week_open FROM stock_fills")
        .fetchone()
    )
    assert (flag[0], flag[1]) == (1, 1)
    assert repo.pending_orders() == []


# ============================================================ idempotency
def test_the_same_week_twice_changes_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _trade_world()
    _run_through(repo, world, 212)
    before = repo.dump()
    again = _run(repo, world, 212)
    assert again.status == "ALREADY_COMPLETED" and again.decision is None
    assert repo.dump() == before


def test_an_earlier_week_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _trade_world()
    _run_through(repo, world, 212)
    assert _run(repo, world, 211).status == "ALREADY_COMPLETED"
    # Week 208 was never run, and it is earlier than the latest run (212).
    with pytest.raises(RunOrderError, match="already been run"):
        _run(repo, world, 208)


# ============================================================ crash-safety
def test_a_crash_mid_run_leaves_only_started_and_resumes_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _trade_world()
    reference = _repo(tmp_path / "reference.db")
    _run_through(reference, world, 212)

    repo = _repo(tmp_path / "crashed.db")
    _run_through(repo, world, 211)
    before = repo.dump()

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("power cut after the fills, before the decision was saved")

    monkeypatch.setattr(StockRepository, "save_decision", boom)
    with pytest.raises(RuntimeError, match="power cut"):
        _run(repo, world, 212)
    after_crash = repo.dump()
    # Only the STARTED row survived: week 212's T2 fill was rolled back too.
    assert {k: v for k, v in after_crash.items() if k != "stock_weekly_runs"} == {
        k: v for k, v in before.items() if k != "stock_weekly_runs"
    }
    assert repo.run_status(friday(212)) == "STARTED"
    # A later week is refused until the interrupted one is finished.
    monkeypatch.undo()
    with pytest.raises(RunOrderError, match="started and not finished"):
        _run(repo, world, 213)

    resumed = _run(repo, world, 212)
    assert resumed.status == "COMPLETED" and resumed.resumed
    assert repo.dump() == reference.dump()


def test_a_rerun_after_a_crash_fills_each_order_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _trade_world()
    _run_through(repo, world, 209)
    monkeypatch.setattr(
        StockRepository, "mark_completed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    with pytest.raises(RuntimeError):
        _run(repo, world, 210)
    monkeypatch.undo()
    _run(repo, world, 210)
    fills = repo.database.connect().execute("SELECT COUNT(*) FROM stock_fills").fetchone()[0]
    assert fills == 1
