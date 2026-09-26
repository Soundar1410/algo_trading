"""Phase 4a-fix3: audit round 3 of the corporate-action handling (spec v1.2l).

End to end through :func:`run_decision_week` on SQLite:

* F1 (HIGH): the checks run again after the run's own fills, so a position
  opened this run, with a bonus going ex later that same week, is frozen
  before any decision (4.14 items 1, 7).
* F2 (HIGH): a re-issued SELL_HALF that rounds to 0 shares follows the
  1-share partial rule instead of crashing the run (item 6).
* F3 (MEDIUM): one unit break is counted once — the unit factor per buy
  fill (item 7).
* F4-F6: the stuck-freeze exit revised (item 8, D103): the 10% tolerance
  when a gap is involved, the row's factor at the fill, "freeze lifted",
  and the row's ex date.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from _wsr1_rules_fixtures import friday, week
from test_stock_accounting import CALENDAR, PARAMS, _inputs, _repo
from test_stock_unadjusted_gaps import (
    EX,
    Cache,
    _ack,
    _equity_row,
    _r1,
    _row,
    _run,
    _through,
    _world,
    monday,
)

from runtimes.positional_stocks.accounting import (
    CLOSED_IN_LIEU,
    FREEZE_LIFTED,
    ONE_SHARE_PARTIAL,
    STUCK_EXIT,
    WAITING_FOR_RESTATEMENT,
    run_decision_week,
)
from runtimes.positional_stocks.repository import StockRepository
from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import shift
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    DailyBar,
    OrderAction,
    PositionState,
)

D = Decimal


@dataclass
class Sessions(Cache):
    """A :class:`Cache` world with per-session close overrides — for an ex
    date in the middle of a week."""

    session_close: dict[date, float] = field(default_factory=dict)

    def daily(self, i: int) -> list[DailyBar]:
        out = []
        for b in super().daily(i):
            close = self.session_close.get(b.session)
            if close is None:
                out.append(b)
                continue
            out.append(
                DailyBar(
                    b.session,
                    b.open,
                    max(b.open, close) + 1.0,
                    min(b.open, close) - 1.0,
                    close,
                    b.volume,
                )
            )
        return out


def _sessions(closes: dict[int, float], session_close: dict[date, float]) -> Sessions:
    world = Sessions(
        close=dict(closes),
        k={207: 15.0, 208: 20.0, 209: 28.0},
        d={207: 18.0, 208: 22.0, 209: 25.0},
        monday_open={210: 1000.0},
        session_close=session_close,
    )
    return world


def _orders(repo: StockRepository, action: str) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in repo.database.connect().execute(
            "SELECT state, quantity, resolution, price_factor FROM stock_pending_orders "
            "WHERE action = ? ORDER BY decided_week",
            (action,),
        )
    ]


# ====================================================================== F1
def test_a_bonus_ex_the_day_after_the_t1_fill_freezes_that_same_run(tmp_path: Path) -> None:
    """The audit repro: T1 fills Monday @ 1,000 (stop 700); a 1:1 bonus goes
    ex on Tuesday; the raw weekly close is 505. Before v1.2l that run decided
    "stop: weekly close 505.00 below stop 700.00" and marked a false 1.98%
    drawdown."""
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _sessions({210: 505.0, 211: 505.0}, {monday(210): 1010.0})
    _run(repo, world, 209)
    outcome = _run(repo, world, 210)
    (position,) = repo.positions().values()
    assert position.buys[0].session == monday(210) and position.stop == D("700.00")
    assert [f.position_id for f in outcome.frozen] == [position.position_id]
    assert outcome.frozen[0].factor == D("0.5")  # Tuesday 505 / Monday 1,010
    assert outcome.decision is not None and outcome.decision.orders == ()
    row = _equity_row(repo, 210)
    # Marked at 505 / 0.5 = 1,010: no false drawdown.
    assert D(str(row["equity"])) == repo.cash() + 40 * D("1010")
    assert row["drawdown_pct"] == "0.00"


def test_a_late_filled_t1_with_a_gap_after_its_fill_is_frozen(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _sessions({210: 505.0, 211: 505.0}, {monday(210): 1010.0})
    _run(repo, world, 209)
    # Run 210: week 210's candles are not in the cache yet.
    missing = replace(_inputs(world, 210), daily={"A": world.daily(209)})
    run_decision_week(repo, missing, calendar=CALENDAR, params=PARAMS)
    assert repo.positions() == {}
    # Run 211: T1 fills late at 210's Monday; the Tuesday gap is after it.
    outcome = _run(repo, world, 211)
    (position,) = repo.positions().values()
    assert position.buys[0].session == monday(210)
    assert len(outcome.frozen) == 1
    assert outcome.decision is not None and outcome.decision.orders == ()


def test_a_gap_on_the_t1_fill_session_itself_does_not_freeze(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    # Friday 209 closes 1,000; Monday 210 — the T1 session — closes 800.
    world = _sessions({210: 800.0, 211: 800.0}, {})
    _run(repo, world, 209)
    outcome = _run(repo, world, 210)
    assert len(repo.positions()) == 1 and outcome.frozen == ()


# ====================================================================== F2
def _consolidation(t1_open: float, pre: float) -> Cache:
    """T1 at ``t1_open``; K/D 95/92 at 211 -> a SELL_HALF for 212's Monday,
    which is also the ex day of a 10:1 consolidation restated from run 212."""
    world = _world(
        {210: pre, 211: pre, **{w: pre * 10 for w in range(212, 216)}},
        restatements=((212, date.min, EX, 10.0),),
    )
    world.monday_open = {210: t1_open}
    world.k[211], world.d[211] = 95.0, 92.0
    return world


def test_a_reissued_sell_half_of_0_shares_follows_the_1_share_rule(tmp_path: Path) -> None:
    """The audit repro: 15 shares, SELL_HALF 7 pending, then a 10:1
    consolidation leaves 1 share — floor(1 / 2) = 0 used to crash the run
    and leave it STARTED for ever."""
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _consolidation(t1_open=2600.0, pre=2600.0)
    _through(repo, world, 211)
    (half,) = repo.pending_orders()
    assert (half.action, half.quantity) == (OrderAction.SELL_HALF, 7)

    outcome = _run(repo, world, 212, (_row("0.1"),))
    assert outcome.status == "COMPLETED"
    assert repo.run_status(friday(212)) == "COMPLETED"
    (position,) = repo.positions().values()
    assert position.state is PositionState.HALF_SOLD and position.shares_held == 1
    # Dated to the original decision (211): the week that SELL_HALF would have filled.
    assert position.half_sold_week == shift(week(211), 1)
    assert repo.pending_orders() == []
    assert _orders(repo, "SELL_HALF") == [("SUPERSEDED", 7, ONE_SHARE_PARTIAL, None)]
    assert outcome.decision is not None
    (review,) = outcome.decision.reviews
    assert review.reason == "hold: trailing"  # the trail has replaced the stop


def test_a_sell_half_on_a_holding_that_floors_to_0_is_skipped_by_the_close(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _consolidation(t1_open=8000.0, pre=8000.0)  # 5 shares; SELL_HALF 2
    _through(repo, world, 211)
    (half,) = repo.pending_orders()
    assert half.quantity == 2
    _run(repo, world, 212, (_row("0.1"),))
    (position,) = repo.positions().values()
    assert position.state is PositionState.CLOSED  # D101: 0.5 share in lieu
    assert _orders(repo, "SELL_HALF") == [("SKIPPED", 2, CLOSED_IN_LIEU, None)]


# ====================================================================== F3
def _partial_boundary() -> Cache:
    """A 1:1 bonus ex 213's Monday. From run 213 Dhan restates only back to
    211's Monday (the boundary). T1 filled at 210 (before it) stays
    unrestated; T2, added at 212's Monday @ 960, is restated by 0.5."""
    ex = monday(213)
    world = _world(
        {210: 900.0, 211: 950.0, 212: 960.0, **{w: 480.0 for w in range(213, 220)}},
        high={210: 940.0, 211: 960.0},
        low={210: 890.0},
        restatements=((213, monday(211), ex, 0.5),),
    )
    return world


def test_one_unit_break_is_counted_once(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _partial_boundary()
    rows = (_row("2", monday(213)),)
    _through(repo, world, 212, rows)
    (position,) = repo.positions().values()
    assert [b.price for b in position.buys] == [D("1000.00"), D("960.00")]
    before = _equity_row(repo, 212)

    outcome = _run(repo, world, 213, rows)
    (freeze,) = outcome.frozen
    # T1's unit factor 950 x 0.5 / 900 = 0.5278 (the boundary gap); T2's 0.5.
    assert freeze.factor == D("0.5") and not freeze.mixed_units
    after = _equity_row(repo, 213)
    # Flat in the position's units (480 / 0.5 = 960): equity is unchanged, and
    # the peak is not raised to a false value (f x R = 0.264 would have been).
    assert after["equity"] == before["equity"]
    assert after["peak"] == before["peak"]

    # F4 on the same case: an eligible, matching row -> the D102 exit on run 3.
    _run(repo, world, 214, rows)
    third = _run(repo, world, 215, rows)
    assert third.frozen[0].runs == 3
    (exit_order,) = repo.pending_orders()
    assert exit_order.reason == STUCK_EXIT and exit_order.price_factor == D("0.5")


# ====================================================================== F4
def _ex_day(ratio: float) -> Cache:
    """R1 with a real ex-day market move: the raw close falls from 1,010 to
    1,010 x ``ratio`` on the ex Monday, not to exactly half."""
    close = round(1010.0 * ratio, 2)
    return _world({210: 1010.0, 211: 1010.0, **{w: close for w in range(212, 220)}})


def test_an_ex_day_move_still_matches_the_row_within_10_percent(tmp_path: Path) -> None:
    for ratio in (0.51, 0.46):
        repo = _repo(tmp_path / f"r{ratio}.db")
        world = _ex_day(ratio)
        rows = (_row("2"),)
        outcomes = _through(repo, world, 214, rows)
        assert [o.frozen[0].runs for o in outcomes[-3:]] == [1, 2, 3]
        (exit_order,) = repo.pending_orders()
        # The row's factor, not the freeze factor (the gap ratio).
        assert exit_order.price_factor == D("0.5")
        filled = _run(repo, world, 215, rows)
        assert filled.frozen == ()  # closed this run: not listed as frozen
        (position,) = repo.positions().values()
        sale = position.sales[-1]
        open_215 = D(str(round(1010.0 * ratio, 2)))
        assert (sale.session, sale.shares, sale.price) == (monday(215), 40, open_215 * 2)


def test_an_ex_day_ratio_more_than_10_percent_off_is_no_exit(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    outcomes = _through(repo, _ex_day(0.44), 216, (_row("2"),))
    assert outcomes[-1].frozen[0].runs == 5
    assert repo.pending_orders() == []


# ====================================================================== F5
def test_an_acknowledgement_before_the_fill_skips_the_exit(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _r1()
    rows = (_row("2"),)
    _through(repo, world, 214, rows)
    (queued,) = repo.pending_orders()
    assert queued.price_factor is not None

    world.extra_acks.append(_ack(EX, "0.5000"))  # the operator: "a real move"
    outcome = _run(repo, world, 215, rows)
    assert outcome.frozen == ()
    stuck = [row for row in _orders(repo, "SELL_ALL") if row[2] == FREEZE_LIFTED]
    assert stuck == [("SKIPPED", 40, FREEZE_LIFTED, "0.5")]
    # Decisions resume in this run: a real close of 505 is below the stop.
    assert outcome.decision is not None
    (order,) = outcome.decision.orders
    assert (order.action, order.price_factor) == (OrderAction.SELL_ALL, None)
    assert order.reason == "stop: weekly close 505.00 below stop 700.00"


def test_a_rescale_before_the_fill_reissues_the_exit_without_a_factor(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _r1(restatements=((215, date.min, EX, 0.5),))
    rows = (_row("2"),)
    _through(repo, world, 214, rows)
    (queued,) = repo.pending_orders()
    assert queued.execute_on_or_after == monday(215)

    outcome = _run(repo, world, 215, rows)
    assert len(outcome.rescaled) == 1
    (position,) = repo.positions().values()
    assert position.state is PositionState.CLOSED
    sale = position.sales[-1]
    # 80 new-unit shares at 215's open of 505: once, with no factor on top.
    assert (sale.shares, sale.price) == (80, D("505.00"))
    states = sorted((row[0], row[3]) for row in _orders(repo, "SELL_ALL"))
    assert states == [("FILLED", None), ("SUPERSEDED", "0.5")]


# ====================================================================== F6
def test_a_row_for_a_future_ex_date_neither_exits_nor_notes_until_it_passes(
    tmp_path: Path,
) -> None:
    """The audit repro: a real -50% crash on 27 Jan 2025 (week 212's Monday)
    and an advance row for ex 28 Apr 2025 (week 225's)."""
    assert date(2025, 1, 27) == EX and monday(225) == date(2025, 4, 28)
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _world({210: 1010.0, 211: 1010.0, **{w: 505.0 for w in range(212, 227)}})
    rows = (_row("2", monday(225)),)
    outcomes = _through(repo, world, 224, rows)
    for outcome in outcomes[-13:]:
        (freeze,) = outcome.frozen
        assert WAITING_FOR_RESTATEMENT not in freeze.detail
    assert repo.pending_orders() == []

    eligible = _run(repo, world, 225, rows)
    assert WAITING_FOR_RESTATEMENT in eligible.frozen[0].detail
    (exit_order,) = repo.pending_orders()
    assert exit_order.reason == STUCK_EXIT


def test_frozen_lists_no_position_that_closed_in_the_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _r1()
    _through(repo, world, 214, (_row("2"),))
    filled = _run(repo, world, 215, (_row("2"),))
    (position,) = repo.positions().values()
    assert position.state is PositionState.CLOSED and filled.frozen == ()
    assert filled.decision is not None and filled.decision.reviews == ()
