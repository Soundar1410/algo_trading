"""Phase 4b-1 Part A: the two v1.2m corporate-action follow-ups, end to end.

* N2 (4.14 items 2, 8): a PRICE_CORRECTION changes no units. Once an eligible
  PRICE_CORRECTION row matches a freeze, the position is marked at the close
  and its stuck-freeze exit fills at the open (factor 1). The audit repro — a
  0.95 correction over [Mon 212, Mon 216), T1 before it, T2 inside it — used
  to mark at close / 0.95 (a false peak) and fill the exit at 1,000 on an open
  of 950 x (1 / 0.95): +3,546.10 of false P&L.
* N1 (4.14 item 7): a freeze that lifts with neither an acknowledgement nor a
  rescale is recorded and warned about. The lift itself is unchanged.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from test_stock_accounting import _repo
from test_stock_unadjusted_gaps import Cache, _ack, _equity_row, _run, _through, _world, monday

from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    CorporateActionKind,
    CorporateActionRow,
    PositionState,
)

D = Decimal


# ====================================================================== N2
def _correction_world() -> Cache:
    """T1 @ 1,000 at 210 (touches L1 at 210's low of 890); 211's close of 950
    above 210's high of 940 adds T2 at 212's Monday open of 1,000. From run
    216 Dhan corrects [Mon 212, Mon 216) by 0.95: T2's session is inside the
    range, T1's is before it. Every boundary move is under 15%."""
    return _world(
        {210: 900.0, 211: 950.0, **{w: 1000.0 for w in range(212, 222)}},
        high={210: 940.0, 211: 960.0},
        low={210: 890.0},
        restatements=((216, monday(212), monday(216), 0.95),),
    )


def _correction(ratio: str = "0.95") -> CorporateActionRow:
    return CorporateActionRow(
        "A",
        monday(212),
        CorporateActionKind.PRICE_CORRECTION,
        D(ratio),
        date(2026, 9, 26),
        "Dhan data correction",
    )


def test_a_matching_price_correction_marks_at_the_close_and_exits_at_the_open(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _correction_world()
    rows = (_correction(),)
    _through(repo, world, 215, rows)
    (position,) = repo.positions().values()
    assert [b.price for b in position.buys] == [D("1000.00"), D("1000.00")]
    before = _equity_row(repo, 215)

    frozen = _run(repo, world, 216, rows)
    (freeze,) = frozen.frozen
    # T2 restated by 0.95, T1 not: item 1 cannot apply the row, so it freezes —
    # but a correction changes no units: the mark is the close itself.
    assert freeze.factor == D("1") and freeze.price_corrected
    assert "PRICE_CORRECTION 0.95" in freeze.detail
    after = _equity_row(repo, 216)
    assert after["equity"] == before["equity"] and after["peak"] == before["peak"]

    _run(repo, world, 217, rows)
    third = _run(repo, world, 218, rows)
    assert third.frozen[0].runs == 3
    (exit_order,) = repo.pending_orders()
    assert exit_order.price_factor == D("1")

    _run(repo, world, 219, rows)
    (position,) = repo.positions().values()
    assert position.state is PositionState.CLOSED
    # At the open of 1,000 — not 1,000 / 0.95 = 1,052.63.
    assert position.sales[-1].price == D("1000.00")


def test_without_the_rule_the_mark_would_have_been_close_over_f(tmp_path: Path) -> None:
    """The premise: the same freeze without a PRICE_CORRECTION row keeps its
    item-1 factor, 0.95 — which is what marked a false peak before v1.2m."""
    repo = _repo(tmp_path / "positional_stocks.db")
    _through(repo, _correction_world(), 215)
    frozen = _run(repo, _correction_world(), 216)
    (freeze,) = frozen.frozen
    assert freeze.factor == D("0.95") and not freeze.price_corrected


# ====================================================================== N1
def _bonus_1_5() -> Cache:
    """A 1:5 bonus (ratio 1.2) ex 214's Monday: the raw close falls from
    1,035 to 862.50 (-16.7%) and the position is frozen at runs 214-215. From
    run 216 Dhan restates only back to 212's Monday; the boundary gap it
    leaves (1,010 -> 862.50) is -14.6%, under the 15% threshold."""
    return _world(
        {
            210: 1010.0,
            211: 1010.0,
            212: 1035.0,
            213: 1035.0,
            **{w: 862.5 for w in range(214, 222)},
        },
        restatements=((216, monday(212), monday(214), 1 / 1.2),),
    )


def test_a_freeze_lifted_by_a_partial_restatement_is_flagged(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _bonus_1_5()
    outcomes = _through(repo, world, 215)
    assert [len(o.frozen) for o in outcomes[-2:]] == [1, 1]
    lifted = _run(repo, world, 216)
    assert lifted.frozen == () and lifted.rescaled == ()
    (lift,) = lifted.silent_lifts
    assert lift.kind == "silent_lift" and lift.symbol == "A"
    assert lifted.decision is not None
    assert any("freeze lifted silently" in w for w in lifted.decision.warnings)
    # Reported only: the next run, not frozen last time, is not flagged again.
    assert _run(repo, world, 217).silent_lifts == ()


def test_a_freeze_lifted_by_an_acknowledgement_is_not_flagged(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _world({210: 1010.0, 211: 1010.0, **{w: 505.0 for w in range(212, 220)}})
    _through(repo, world, 213)
    world.extra_acks.append(_ack(monday(212), "0.5000"))
    lifted = _run(repo, world, 214)
    assert lifted.frozen == () and lifted.silent_lifts == ()


def test_a_freeze_lifted_by_a_rescale_is_not_flagged(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "positional_stocks.db")
    world = _world(
        {210: 1010.0, 211: 1010.0, **{w: 505.0 for w in range(212, 220)}},
        restatements=((214, date.min, monday(212), 0.5),),
    )
    row = CorporateActionRow(
        "A", monday(212), CorporateActionKind.BONUS_SPLIT, D("2"), date(2026, 9, 26)
    )
    _through(repo, world, 213, (row,))
    rescaled = _run(repo, world, 214, (row,))
    assert len(rescaled.rescaled) == 1 and rescaled.silent_lifts == ()
    assert [e.kind for e in rescaled.events][:1] == ["rescale"]
