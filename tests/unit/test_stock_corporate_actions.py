"""Phase 4a-fix: corporate actions on held positions, end to end through SQLite
(spec 4.14 v1.2j).

The Phase 4a audit reproduced the defect this fixes: 40 shares bought at
1,000 (stop 700), a 1:1 bonus ex in week 212, the stock flat (1,010 before is
505 after). Dhan back-adjusts the history, so the weekly close of 505 fell
under the unadjusted stop 700. The book recorded "stop: weekly close 505.00
below stop 700.00", a SELL_ALL, a net loss of 19,685.44, a cooling-off and a
drawdown, none of them real.

The daily cache here is **restated** the way Dhan's is: from the first run
at or after the ex week, every bar before the ex session is multiplied by the
price factor (0.5 for a 1:1 bonus), while the stored fills keep the prices
they filled at.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from _wsr1_rules_fixtures import friday
from test_stock_accounting import CALENDAR, PARAMS, World, _inputs, _repo

from runtimes.positional_stocks.accounting import REISSUED, WeekOutcome, run_decision_week
from runtimes.positional_stocks.repository import StockRepository
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    CorporateActionKind,
    CorporateActionRow,
    DailyBar,
    OrderAction,
    PositionState,
)

D = Decimal
EX_WEEK = 212
EX_SESSION = friday(EX_WEEK) - timedelta(days=4)  # Monday of week 212
_TRIGGER_K = {207: 15.0, 208: 20.0, 209: 28.0}
_TRIGGER_D = {207: 18.0, 208: 22.0, 209: 25.0}


@dataclass
class Restated(World):
    """A world whose cached history is restated by ``factor`` once the run
    has reached the ex week."""

    factor: float = 0.5
    ex_week: int = EX_WEEK

    def daily(self, i: int) -> list[DailyBar]:
        bars = super().daily(i)
        if i < self.ex_week:
            return bars
        f = self.factor
        return [
            DailyBar(b.session, b.open * f, b.high * f, b.low * f, b.close * f, b.volume)
            if b.session < EX_SESSION
            else b
            for b in bars
        ]


def _bonus_world(**kw: object) -> Restated:
    """T1 40 @ 1,000 at 210's open; flat at 1,010 (505 after a 1:1 bonus).

    Closes stay above P1, so no level is touched and nothing adds."""
    factor = kw.pop("factor", 0.5)
    pre = kw.pop("pre", 1010.0)
    close = {210: pre, 211: pre}
    close.update({w: round(pre * factor, 2) for w in range(EX_WEEK, 216)})  # type: ignore[operator]
    world = Restated(
        close=close,
        k=dict(_TRIGGER_K),
        d=dict(_TRIGGER_D),
        monday_open={210: kw.pop("t1_open", 1000.0)},  # type: ignore[dict-item]
        factor=factor,  # type: ignore[arg-type]
    )
    for key, value in kw.items():
        setattr(world, key, value)
    return world


def _bonus(
    ratio: str, kind: CorporateActionKind = CorporateActionKind.BONUS_SPLIT
) -> CorporateActionRow:
    return CorporateActionRow("A", EX_SESSION, kind, D(ratio), date(2026, 9, 26), "test")


def _run(
    repo: StockRepository, world: World, i: int, rows: tuple[CorporateActionRow, ...] = ()
) -> WeekOutcome:
    inputs = replace(_inputs(world, i), corporate_actions=rows)
    return run_decision_week(repo, inputs, calendar=CALENDAR, params=PARAMS)


def _fills(repo: StockRepository) -> list[tuple[object, ...]]:
    return repo.dump()["stock_fills"]


def _equity(repo: StockRepository, i: int) -> Decimal:
    row = (
        repo.database.connect()
        .execute("SELECT equity FROM stock_equity WHERE week_ending = ?", (friday(i).isoformat(),))
        .fetchone()
    )
    return D(row["equity"])


def _through(repo: StockRepository, world: World, last: int) -> None:
    for i in range(209, last + 1):
        assert _run(repo, world, i).status == "COMPLETED"


# ============================================== the reproduced audit case
def test_a_1_1_bonus_freezes_then_rescales_without_a_false_stop(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _bonus_world()
    _through(repo, world, 211)
    (position,) = repo.positions().values()
    assert (position.shares_held, position.stop, position.p1) == (40, D("700.00"), D("1000.00"))
    cash = repo.cash()

    # Week 212: the history is restated, nothing is confirmed yet -> frozen.
    frozen = _run(repo, world, 212)
    assert frozen.decision is not None
    (review,) = frozen.decision.reviews
    assert review.order is None and "frozen" in review.flags
    assert "f=0.5000" in review.reason
    assert frozen.decision.orders == ()
    assert [f.factor for f in frozen.frozen] == [D("0.5")]
    # Marked at 505 / 0.5 = 1,010 in its own units: no false drop, no drawdown.
    assert _equity(repo, 212) == _equity(repo, 211) == cash + 40 * D("1010")
    assert repo.database.connect().execute("SELECT * FROM stock_cooling_off").fetchall() == []
    # It still holds its slot: the position is in the decision's book.
    assert [p.position_id for p in frozen.decision.positions] == [position.position_id]

    # Week 213: the operator confirmed the 1:1 bonus (ratio 2).
    fills_before = _fills(repo)
    rescaled = _run(repo, world, 213, (_bonus("2"),))
    assert rescaled.decision is not None and rescaled.frozen == ()
    (position,) = repo.positions().values()
    assert position.shares_held == 80
    assert (position.p1, position.l1, position.l2, position.stop) == (
        D("500.00"),
        D("450.00"),
        D("400.00"),
        D("350.00"),
    )
    assert position.state is PositionState.OPEN
    assert rescaled.decision.orders == ()  # close 505 >= P1 500: no exit, no add
    assert repo.cash() == cash  # no fraction, so no cash in lieu
    assert position.buy_value == D("40000.00")  # rupee cost unchanged
    assert _equity(repo, 213) == cash + 80 * D("505")
    # The fill rows were never edited; the rescale is its own record.
    assert _fills(repo) == fills_before
    (record,) = repo.database.connect().execute("SELECT * FROM stock_corporate_actions")
    assert (record["kind"], record["ratio"], record["shares_before"], record["shares_after"]) == (
        "BONUS_SPLIT",
        "2",
        40,
        80,
    )
    # A fresh rebuild from the database agrees, and nothing is detected again.
    again = _run(repo, world, 214, (_bonus("2"),))
    assert again.frozen == () and again.rescaled == ()
    assert again.decision is not None and again.decision.orders == ()


def test_without_a_freeze_the_audit_case_would_have_stopped_out(tmp_path: Path) -> None:
    """The premise, checked: at 212 the restated close is below the stored stop."""
    world = _bonus_world()
    assert world.series(212).series().bars[-1].close == 505.0 < 700.0


# ======================================== a 1:2 bonus with an odd share count
def test_a_1_2_bonus_on_41_shares_pays_cash_in_lieu(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    # T1 40,000 at 975 buys 41 shares. Flat at 1,000 = 666.67 after.
    world = _bonus_world(factor=1 / 1.5, pre=1000.0, t1_open=975.0)
    _through(repo, world, 212)
    (position,) = repo.positions().values()
    assert position.shares_held == 41
    cash = repo.cash()

    outcome = _run(repo, world, 213, (_bonus("1.5"),))
    (position,) = repo.positions().values()
    assert position.shares_held == 61  # floor(41 x 1.5 = 61.5)
    (rescale,) = outcome.rescaled
    # Half a share at the adjusted pre-ex close (1,000 / 1.5).
    assert rescale.reference_close == D(str(1000.0 * (1 / 1.5)))
    assert rescale.adjustment.cash == D("333.33")
    assert repo.cash() == cash + D("333.33")
    assert position.stop == D("455.00")  # 682.50 / 1.5
    assert position.p1 == D("650.00")  # 975 / 1.5
    assert position.net_pnl == D("333.33") - position.buy_value - position.fees


# ================================================================ demerger
def test_a_demerger_credits_the_value_removed(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _bonus_world(factor=0.9, pre=1000.0)
    _through(repo, world, 212)
    cash = repo.cash()
    equity_211 = _equity(repo, 211)

    outcome = _run(repo, world, 213, (_bonus("0.9", CorporateActionKind.DEMERGER),))
    (position,) = repo.positions().values()
    assert position.shares_held == 40  # shares unchanged
    assert (position.p1, position.stop) == (D("900.00"), D("630.00"))
    (rescale,) = outcome.rescaled
    # 40 x actual pre-ex close 1,000 (cached 900 / 0.9) x 0.1.
    assert rescale.adjustment.cash == D("4000.00")
    assert repo.cash() == cash + D("4000.00")
    # 40 x 900 + 4,000 credit = the 40 x 1,000 before: no false drawdown.
    assert _equity(repo, 213) == equity_211
    # Economic P&L: the credit counts, the rupee cost is not apportioned.
    assert position.net_pnl == D("4000.00") - D("40000.00") - position.fees


# ============================================================== mismatch
def test_a_confirmed_ratio_that_does_not_match_stays_frozen(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _bonus_world()  # a real 1:1 bonus: f = 0.5
    _through(repo, world, 212)
    outcome = _run(repo, world, 213, (_bonus("1.5"),))  # the operator typed a 1:2
    assert outcome.rescaled == ()
    (freeze,) = outcome.frozen
    assert "does not match" in freeze.detail and "expects f=0.6667" in freeze.detail
    (position,) = repo.positions().values()
    assert (position.shares_held, position.stop) == (40, D("700.00"))
    assert repo.database.connect().execute("SELECT * FROM stock_corporate_actions").fetchall() == []


# ============================================================ escalation
def test_a_freeze_over_2_runs_escalates(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _bonus_world()
    _through(repo, world, 211)
    runs = [_run(repo, world, i) for i in (212, 213, 214)]
    assert [o.frozen[0].runs for o in runs] == [1, 2, 3]
    for outcome, escalated in zip(runs, (False, False, True), strict=True):
        assert outcome.decision is not None
        (review,) = outcome.decision.reviews
        has_flag = any(f.startswith("operator action: frozen") for f in review.flags)
        has_warning = any("operator action" in w for w in outcome.decision.warnings)
        assert has_flag == has_warning == escalated


# ================================= a pending sell is re-issued in new units
def test_a_stop_decided_before_a_bonus_is_reissued_in_the_new_units(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    # 211 closes 690 < stop 700: SELL_ALL 40, for 212's Monday — the ex day.
    # After the bonus the stock recovers to 720 (360): a fresh review would
    # hold, so a sale proves the original exit was carried, not re-decided.
    world = _bonus_world()
    world.close.update({211: 690.0, 212: 360.0, 213: 360.0, 214: 360.0})
    _through(repo, world, 211)
    (stop,) = repo.pending_orders()
    assert (stop.action, stop.quantity) == (OrderAction.SELL_ALL, 40)

    # 212: restated, unconfirmed -> frozen; the old-unit sell must not fill.
    frozen = _run(repo, world, 212)
    assert frozen.frozen and repo.pending_orders() == [stop]
    assert len(_fills(repo)) == 1

    # 213: confirmed. The 40-share sell is SUPERSEDED by an 80-share one.
    confirmed = _run(repo, world, 213, (_bonus("2"),))
    (reissued,) = repo.pending_orders()
    assert (reissued.action, reissued.quantity) == (OrderAction.SELL_ALL, 80)
    assert reissued.reason == f"{stop.reason}; {REISSUED}"
    assert reissued.execute_on_or_after == friday(214) - timedelta(days=4)
    sells = repo.database.connect().execute(
        "SELECT state, quantity, resolution FROM stock_pending_orders "
        "WHERE action = 'SELL_ALL' ORDER BY decided_week"
    )
    old, new = (tuple(row) for row in sells)
    assert old[:2] == ("SUPERSEDED", 40) and new[:2] == ("PENDING", 80)
    assert str(old[2]).startswith("superseded by ")
    assert confirmed.decision is not None
    (review,) = confirmed.decision.reviews
    assert review.order is None and review.reason == "an earlier order is still unfilled"

    # 214: the re-issued exit fills at the next open, all 80 shares.
    _run(repo, world, 214, (_bonus("2"),))
    (position,) = repo.positions().values()
    assert position.state is PositionState.CLOSED and position.shares_held == 0
    assert position.sales[-1].shares == 80 and position.sales[-1].price == D("360.00")
    assert repo.pending_orders() == []
