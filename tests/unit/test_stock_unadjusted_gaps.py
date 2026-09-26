"""Phase 4a-fix2: unadjusted corporate actions freeze held positions (spec 4.14
item 7, v1.2k), and a freeze that no restatement can resolve is exited (D102).

Detection by restatement (item 1) only sees history Dhan has back-adjusted. The
audit's round-2 repro R1: 40 shares at 1,000 (stop 700), a 1:1 bonus ex in
week 212, the cache **not** restated — the raw close halves from 1,010 to
505, the stop fires, a false -19,685.44, a cooling-off. Now an unacknowledged
close-to-close gap of 15% or more after the first fill freezes the position
instead, marked at close / r.

Every test runs real weeks through :func:`run_decision_week` on SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from _wsr1_rules_fixtures import ctx, friday, index_series, symbol_week, universe_row
from test_stock_accounting import CALENDAR, PARAMS, World, _inputs, _repo

from runtimes.positional_stocks.accounting import (
    STUCK_EXIT,
    WAITING_FOR_RESTATEMENT,
    WeekInputs,
    WeekOutcome,
    run_decision_week,
)
from runtimes.positional_stocks.repository import StockRepository
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    CorporateActionKind,
    CorporateActionRow,
    DailyBar,
    GapAcknowledgement,
    OrderAction,
    PositionState,
)

D = Decimal
_TRIGGER_K = {207: 15.0, 208: 20.0, 209: 28.0}
_TRIGGER_D = {207: 18.0, 208: 22.0, 209: 25.0}


def monday(i: int) -> date:
    return friday(i) - timedelta(days=4)


EX = monday(212)


@dataclass
class Cache(World):
    """A world whose cache Dhan restates, fully or partly, from a given run.

    Each restatement is ``(from_run, start, end, factor)``: from that run on,
    every bar with ``start <= session < end`` is multiplied by ``factor``.
    Dhan's usual back-adjustment starts at ``date.min``; MOTHERSON's stopped
    at a boundary (spec D94).
    """

    restatements: tuple[tuple[int, date, date, float], ...] = ()
    extra_acks: list[GapAcknowledgement] = field(default_factory=list)

    def daily(self, i: int) -> list[DailyBar]:
        bars = super().daily(i)
        for from_run, start, end, f in self.restatements:
            if i >= from_run:
                bars = [
                    DailyBar(b.session, b.open * f, b.high * f, b.low * f, b.close * f, b.volume)
                    if start <= b.session < end
                    else b
                    for b in bars
                ]
        return bars


def _world(closes: dict[int, float], **kw: object) -> Cache:
    """T1 40 @ 1,000 at 210's Monday open (P1 1,000, L1 900, stop 700)."""
    world = Cache(
        close=dict(closes),
        k=dict(_TRIGGER_K),
        d=dict(_TRIGGER_D),
        monday_open={210: 1000.0},
    )
    for key, value in kw.items():
        setattr(world, key, value)
    return world


def _r1(**kw: object) -> Cache:
    """R1: flat at 1,010, a raw 1:1 bonus at 212's Monday (505 after)."""
    return _world({210: 1010.0, 211: 1010.0, **{w: 505.0 for w in range(212, 220)}}, **kw)


def _ack(session: date, ratio: str) -> GapAcknowledgement:
    return GapAcknowledgement("A", session, D(ratio), date(2026, 9, 26), "real move")


def _row(ratio: str, ex: date = EX) -> CorporateActionRow:
    return CorporateActionRow(
        "A", ex, CorporateActionKind.BONUS_SPLIT, D(ratio), date(2026, 9, 26), "test"
    )


def _run(
    repo: StockRepository,
    world: Cache,
    i: int,
    rows: tuple[CorporateActionRow, ...] = (),
) -> WeekOutcome:
    inputs = replace(
        _inputs(world, i), corporate_actions=rows, acknowledgements=tuple(world.extra_acks)
    )
    return run_decision_week(repo, inputs, calendar=CALENDAR, params=PARAMS)


def _through(
    repo: StockRepository, world: Cache, last: int, rows: tuple[CorporateActionRow, ...] = ()
) -> list[WeekOutcome]:
    return [_run(repo, world, i, rows) for i in range(209, last + 1)]


def _equity_row(repo: StockRepository, i: int) -> dict[str, object]:
    row = (
        repo.database.connect()
        .execute("SELECT * FROM stock_equity WHERE week_ending = ?", (friday(i).isoformat(),))
        .fetchone()
    )
    return dict(row)


def _sells(outcome: WeekOutcome) -> list[OrderAction]:
    assert outcome.decision is not None
    return [o.action for o in outcome.decision.orders if not o.action.is_buy]


# ==================================================================== (a)
def test_r1_an_unrestated_bonus_freezes_instead_of_stopping_out(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _r1()
    *_, frozen = _through(repo, world, 212)
    assert frozen.decision is not None
    (review,) = frozen.decision.reviews
    assert review.order is None and "frozen" in review.flags
    assert f"unadjusted gap {EX} ratio 0.5000" in review.reason
    assert _sells(frozen) == []
    assert repo.database.connect().execute("SELECT * FROM stock_cooling_off").fetchall() == []
    before, after = _equity_row(repo, 211), _equity_row(repo, 212)
    # Marked at 505 / 0.5 = 1,010: no false drop; the brakes see nothing.
    for column in ("equity", "peak", "drawdown_pct", "brake1_until", "brake1_can_fire"):
        assert after[column] == before[column], column
    assert after["brake2_fired_on"] is None


# ==================================================================== (b)
def test_r1_then_dhan_restates_and_a_row_rescales_and_a_real_stop_still_fires(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    # Dhan restates from the 213 run on; then a genuine, gradual fall to 340
    # (each week under 15%) closes below the rescaled stop of 350.
    world = _r1(restatements=((213, date.min, EX, 0.5),))
    world.close.update({214: 440.0, 215: 380.0, 216: 340.0})
    rows = (_row("2"),)
    *_, waiting = _through(repo, world, 212, rows)
    assert waiting.frozen and WAITING_FOR_RESTATEMENT in waiting.frozen[0].detail
    (position,) = repo.positions().values()
    assert position.shares_held == 40  # a row alone never rescales

    rescaled = _run(repo, world, 213, rows)
    assert rescaled.frozen == () and len(rescaled.rescaled) == 1
    (position,) = repo.positions().values()
    assert (position.shares_held, position.stop) == (80, D("350.00"))
    assert _sells(rescaled) == []

    _run(repo, world, 214, rows)
    _run(repo, world, 215, rows)
    stop = _run(repo, world, 216, rows)
    assert stop.decision is not None
    (order,) = stop.decision.orders
    assert (order.action, order.quantity) == (OrderAction.SELL_ALL, 80)
    assert order.reason == "stop: weekly close 340.00 below stop 350.00"


# ==================================================================== (c)
def test_a_real_20_percent_crash_waits_for_its_acknowledgement(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _world({210: 860.0, 211: 860.0, **{w: 688.0 for w in range(212, 216)}})
    *_, frozen = _through(repo, world, 212)
    assert frozen.frozen and _sells(frozen) == []  # 688 < 700, but frozen
    world.extra_acks.append(_ack(EX, "0.8000"))  # the operator: a real move
    fired = _run(repo, world, 213)
    assert fired.frozen == ()
    assert _sells(fired) == [OrderAction.SELL_ALL]


# ==================================================================== (d)
def test_a_real_18_percent_jump_pauses_the_partial_until_acknowledged(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _world({210: 1010.0, 211: 1010.0, 212: 1191.8, 213: 1191.8})
    world.k.update({212: 95.0, 213: 95.0})
    world.d.update({212: 92.0, 213: 92.0})
    *_, frozen = _through(repo, world, 212)
    assert frozen.frozen and frozen.decision is not None
    assert frozen.decision.orders == ()  # K/D > 90 would have sold half
    world.extra_acks.append(_ack(EX, "1.1800"))
    lifted = _run(repo, world, 213)
    assert lifted.frozen == () and _sells(lifted) == [OrderAction.SELL_HALF]


# ==================================================================== (e)
def test_a_gap_on_or_before_the_t1_fill_session_never_freezes(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    # 205: +20% and 206: -16.7% (before the T1); 210's Monday, the T1 fill
    # session itself: 1,000 -> 800 (-20%). None of them counts.
    world = _world({205: 1200.0, 210: 800.0, 211: 800.0, 212: 800.0})
    outcomes = _through(repo, world, 212)
    assert all(o.frozen == () for o in outcomes)
    (position,) = repo.positions().values()
    assert position.buys[0].session == monday(210)


# ==================================================================== (f)
def test_a_12_percent_move_does_not_freeze(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _world({210: 1010.0, 211: 1010.0, 212: 888.8})
    *_, outcome = _through(repo, world, 212)
    assert outcome.frozen == ()


# ==================================================================== (g)
def test_an_acknowledgement_with_another_ratio_does_not_lift_it(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _r1()
    world.extra_acks.append(_ack(EX, "0.5100"))
    *_, outcome = _through(repo, world, 212)
    assert outcome.frozen and _sells(outcome) == []


# ==================================================================== (h)
def _multi(worlds: dict[str, Cache], i: int) -> WeekInputs:
    return WeekInputs(
        ctx=ctx(i),
        symbols={
            s: symbol_week(s, w.series(i).series(), industry=f"Sector-{s}")
            for s, w in worlds.items()
        },
        index=index_series(n=i + 1),
        daily={s: w.daily(i) for s, w in worlds.items()},
        universe_rows=[universe_row(s, f"Sector-{s}") for s in worlds],
        fingerprint=f"fp-{i}",
    )


def test_only_the_gapped_symbol_freezes_and_entries_elsewhere_go_on(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    fresh = _world({})
    fresh.k, fresh.d = {210: 15.0, 211: 20.0, 212: 28.0}, {210: 18.0, 211: 22.0, 212: 25.0}
    worlds = {"A": _r1(), "B": _world({210: 1010.0, 211: 1010.0, 212: 1010.0}), "C": fresh}
    for i in range(209, 213):
        outcome = run_decision_week(repo, _multi(worlds, i), calendar=CALENDAR, params=PARAMS)
    positions = {p.symbol: p for p in repo.positions().values()}
    assert set(positions) == {"A", "B"}
    assert [f.position_id for f in outcome.frozen] == [positions["A"].position_id]
    assert outcome.decision is not None
    reviews = {r.symbol: r for r in outcome.decision.reviews}
    assert "frozen" in reviews["A"].flags and "frozen" not in reviews["B"].flags
    assert [(o.symbol, o.action) for o in outcome.decision.orders] == [("C", OrderAction.BUY_T1)]


# ==================================================================== (i)
def test_a_t2_held_through_the_freeze_fills_on_the_gap_session_after_the_rescale(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    # 210 touches L1 (low 890); 211's close of 950 above 210's high of 940 is
    # the reversal: T2 for 212's Monday — the ex day of a raw 1:1 bonus.
    world = _world(
        {210: 900.0, 211: 950.0, **{w: 475.0 for w in range(212, 216)}},
        high={210: 940.0, 211: 960.0},
        low={210: 890.0},
        restatements=((213, date.min, EX, 0.5),),
    )
    _through(repo, world, 211)
    (t2,) = repo.pending_orders()
    assert t2.action is OrderAction.BUY_T2 and t2.execute_on_or_after == EX

    frozen = _run(repo, world, 212, (_row("2"),))
    assert frozen.frozen and repo.pending_orders() == [t2]  # held, not filled

    rescaled = _run(repo, world, 213, (_row("2"),))
    (rescale,) = rescaled.rescaled
    assert rescale.adjustment.applies_to == (OrderAction.BUY_T1,)
    (position,) = repo.positions().values()
    t2_fill = position.buys[1]
    assert (t2_fill.session, t2_fill.price) == (EX, D("475.00"))
    assert position.shares_held == 80 + t2_fill.shares
    late = (
        repo.database.connect()
        .execute("SELECT late_fill FROM stock_fills WHERE action = 'BUY_T2'")
        .fetchone()
    )
    assert late["late_fill"] == 1


# ========================================================= D102: stuck exits
def test_a_bonus_dhan_never_restates_is_exited_on_the_third_frozen_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _r1()
    rows = (_row("2"),)
    outcomes = _through(repo, world, 214, rows)
    assert [o.frozen[0].runs for o in outcomes[-3:]] == [1, 2, 3]
    assert all(o.decision is not None and o.decision.orders == () for o in outcomes[-3:])
    (exit_order,) = repo.pending_orders()
    assert (exit_order.action, exit_order.quantity) == (OrderAction.SELL_ALL, 40)
    assert exit_order.reason == STUCK_EXIT and exit_order.price_factor == D("0.5")
    assert exit_order.execute_on_or_after == monday(215)
    assert outcomes[-1].decision is not None
    assert any(STUCK_EXIT in w for w in outcomes[-1].decision.warnings)

    _run(repo, world, 215, rows)
    (position,) = repo.positions().values()
    assert position.state is PositionState.CLOSED
    sale = position.sales[-1]
    # 215's open of 505 / 0.5 = 1,010, in the position's own units.
    assert (sale.session, sale.shares, sale.price) == (monday(215), 40, D("1010.00"))
    assert sale.fees == D("44.44") + D("15")  # normal sell costs
    assert repo.pending_orders() == []


def test_a_motherson_style_partial_restatement_is_exited_the_same_way(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    # A 1:1 bonus ex 213's Monday. From the 213 run Dhan restates only back to
    # 211's Monday: the T1 fill (210) stays unrestated, and a raw 0.5 gap
    # appears at the restatement boundary.
    ex = monday(213)
    world = _world(
        {210: 1010.0, 211: 1010.0, 212: 1010.0, **{w: 505.0 for w in range(213, 220)}},
        restatements=((213, monday(211), ex, 0.5),),
    )
    rows = (_row("2", ex),)
    outcomes = _through(repo, world, 215, rows)
    first = outcomes[-3]
    assert first.frozen and f"unadjusted gap {monday(211)} ratio 0.5000" in first.frozen[0].detail
    assert first.rescaled == ()  # the row can never apply: the T1 is not restated
    (exit_order,) = repo.pending_orders()
    assert exit_order.reason == STUCK_EXIT and exit_order.price_factor == D("0.5")
    _run(repo, world, 216, rows)
    (position,) = repo.positions().values()
    assert position.state is PositionState.CLOSED
    assert (position.sales[-1].shares, position.sales[-1].price) == (40, D("1010.00"))


def test_a_real_crash_with_no_row_is_not_exited_and_stays_escalated(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _world({210: 860.0, 211: 860.0, **{w: 688.0 for w in range(212, 216)}})
    outcomes = _through(repo, world, 214)
    last = outcomes[-1]
    assert last.frozen[0].runs == 3 and last.decision is not None
    assert any("operator action" in w for w in last.decision.warnings)
    assert repo.pending_orders() == []


def test_a_row_that_does_not_explain_the_factor_is_not_an_exit(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    outcomes = _through(repo, _r1(), 214, (_row("1.5"),))  # f 0.5 is not 1 / 1.5
    assert outcomes[-1].frozen[0].runs == 3
    assert repo.pending_orders() == []
