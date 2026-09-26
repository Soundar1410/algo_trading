"""Phase 4a-fix: the pure side of spec 4.14 v1.2j — detection, matching, the
rescale arithmetic, a frozen position in the rules, and the fail-closed
``corporate_actions.csv`` loader."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from _wsr1_rules_fixtures import (
    PARAMS0,
    Tape,
    book,
    ctx,
    fill,
    friday,
    index_series,
    kd_tape,
    monday_after,
    open_position,
    symbol_week,
    week,
)

from strategies.positional_stocks.wsr1_weekly_stochrsi.corporate_actions import (
    Rescale,
    detect,
    gap_detail,
    gap_factor,
    rescale_levels,
    resolve,
    stuck_exit_row,
    unadjusted_gaps,
    unverifiable,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.inputs import (
    InputFileError,
    load_corporate_actions,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    CorporateActionKind,
    CorporateActionRow,
    DailyBar,
    Freeze,
    FunnelStage,
    GapAcknowledgement,
    OrderAction,
    Position,
    PositionState,
    ShareAdjustment,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import decide_week

D = Decimal
BONUS = CorporateActionKind.BONUS_SPLIT
DEMERGER = CorporateActionKind.DEMERGER
PRICE_CORRECTION = CorporateActionKind.PRICE_CORRECTION
CONFIG = Path(__file__).resolve().parents[2] / "config" / "positional_stocks"


def _bars(opens: dict[date, float], last: date, default: float = 1000.0) -> list[DailyBar]:
    """Weekday bars from 20 sessions before ``last``; open = close unless set."""
    bars = []
    day = last - timedelta(days=40)
    while day <= last:
        if day.weekday() < 5:
            price = opens.get(day, default)
            bars.append(DailyBar(day, price, price + 1, price - 1, price, 1e6))
        day += timedelta(days=1)
    return bars


def _row(ratio: str, ex: date, kind: CorporateActionKind = BONUS) -> CorporateActionRow:
    return CorporateActionRow("A", ex, kind, D(ratio), date(2026, 9, 26))


# ------------------------------------------------------------- detection
def test_matching_fills_are_not_a_restatement() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    assert detect(position, _bars({}, friday(203))) is None


def test_half_a_percent_is_tolerated_and_more_is_not() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    t1 = position.buys[0].session
    assert detect(position, _bars({t1: 1005.0}, friday(203))) is None
    found = detect(position, _bars({t1: 1005.1}, friday(203)))
    assert found is not None and found.factor == D("1.0051")


def test_a_fill_session_missing_from_the_cache_is_unverifiable_not_frozen() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    late = [b for b in _bars({}, friday(210)) if b.session > friday(205)]
    assert detect(position, late) is None and unverifiable(position, late)


def test_two_stacked_actions_are_inconsistent_and_never_resolved() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    position = fill(position, OrderAction.BUY_T2, price=900.0, week_index=202)
    opens = {position.buys[0].session: 500.0, position.buys[1].session: 600.0}
    found = detect(position, _bars(opens, friday(205)))
    assert found is not None and not found.consistent
    result = resolve(position, found, [_row("2", monday_after(203))], _bars(opens, friday(205)))
    assert isinstance(result, str) and "disagree" in result


# -------------------------------------------------------------- matching
def test_an_ex_session_that_splits_the_fills_wrongly_is_refused() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    position = fill(position, OrderAction.BUY_T2, price=900.0, week_index=202)
    # Only T1 was restated: the ex session lies between T1 and T2.
    opens = {position.buys[0].session: 500.0, position.buys[1].session: 900.0}
    daily = _bars(opens, friday(205))
    found = detect(position, daily)
    assert found is not None
    after_both = resolve(position, found, [_row("2", monday_after(203))], daily)
    assert isinstance(after_both, str) and "before the ex session, was not restated" in after_both
    between = resolve(position, found, [_row("2", monday_after(201))], daily)
    assert isinstance(between, Rescale)
    assert between.adjustment.applies_to == (OrderAction.BUY_T1,)
    # 40 old shares become 80; T2's 32 were bought in the new units already.
    assert (between.shares_before, between.shares_after) == (40, 80)


def test_a_row_for_another_symbol_or_before_the_fill_is_ignored() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    daily = _bars({position.buys[0].session: 500.0}, friday(205))
    found = detect(position, daily)
    assert found is not None
    other = CorporateActionRow("B", monday_after(202), BONUS, D("2"), date(2026, 9, 26))
    early = _row("2", monday_after(190))
    result = resolve(position, found, [other, early], daily)
    assert isinstance(result, str) and "no confirmed row" in result


def test_a_rescaled_position_is_not_detected_again() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    daily = _bars({position.buys[0].session: 500.0}, friday(205), default=500.0)
    found = detect(position, daily)
    assert found is not None
    rescale = resolve(position, found, [_row("2", monday_after(202))], daily)
    assert isinstance(rescale, Rescale)
    adjusted = _with(position, rescale.adjustment)
    assert detect(adjusted, daily) is None
    assert adjusted.shares_held == 80 and adjusted.current_price(adjusted.buys[0]) == D("500")


def _with(position: Position, adjustment: ShareAdjustment) -> Position:
    return replace(
        position,
        adjustments=(adjustment,),
        p1=rescale_levels(position.p1, (adjustment,)),
        l1=rescale_levels(position.l1, (adjustment,)),
        l2=rescale_levels(position.l2, (adjustment,)),
        stop=rescale_levels(position.stop, (adjustment,)),
    )


# ------------------------------------------------- position in new units
def test_a_bonus_after_a_partial_sale_rescales_only_what_is_held() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    position = fill(position, OrderAction.BUY_T2, price=930.0, week_index=201)
    position = fill(position, OrderAction.SELL_HALF, price=1120.0, week_index=203, quantity=36)
    adjustment = ShareAdjustment(
        date(2026, 1, 1),
        BONUS,
        D("2"),
        (OrderAction.BUY_T1, OrderAction.BUY_T2, OrderAction.SELL_HALF),
        shares_delta=36,
    )
    adjusted = _with(position, adjustment)
    assert adjusted.shares_held == 72
    # Cost of the 72 held = the cost of the 36 old ones: rupees unchanged.
    assert adjusted.cost_of_held == position.cost_of_held
    assert adjusted.net_pnl == position.net_pnl


def test_adjustment_validation() -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        ShareAdjustment(date(2026, 1, 1), DEMERGER, D("1.1"), (OrderAction.BUY_T1,))
    with pytest.raises(ValueError, match="unchanged"):
        ShareAdjustment(date(2026, 1, 1), DEMERGER, D("0.9"), (OrderAction.BUY_T1,), 3)
    # v1.2k: below 1 is a consolidation, and valid; 1, 0 and negatives are not.
    CorporateActionRow("A", date(2026, 1, 1), BONUS, D("0.5"), date(2026, 1, 2))
    for bad in ("1", "0", "-2"):
        for kind in (BONUS, PRICE_CORRECTION):
            with pytest.raises(ValueError, match="positive and not 1"):
                CorporateActionRow("A", date(2026, 1, 1), kind, D(bad), date(2026, 1, 2))
    with pytest.raises(ValueError, match="moves the share count with its ratio"):
        ShareAdjustment(date(2026, 1, 1), BONUS, D("0.1"), (OrderAction.BUY_T1,), 3)
    with pytest.raises(ValueError, match="no cash"):
        ShareAdjustment(
            date(2026, 1, 1), PRICE_CORRECTION, D("0.99"), (OrderAction.BUY_T1,), 0, D("1")
        )


# ------------------------------------------------ a frozen position, rules
def test_a_frozen_position_counts_toward_the_limits_and_decides_nothing() -> None:
    params = replace(PARAMS0, max_positions=1)
    held = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    # A's weekly close of 500 is far below its stored stop of 700.
    a = symbol_week("A", Tape(close=500.0).series())
    trigger = [(15.0, 18.0), (20.0, 22.0), (28.0, 25.0)]
    b = symbol_week("B", kd_tape(trigger).series())
    freeze = Freeze(held.position_id, D("0.5"), "restated", runs=1)
    decision = decide_week(
        ctx(),
        book(held),
        {"A": a, "B": b},
        index_series(),
        params,
        frozen={held.position_id: freeze},
    )
    (review,) = decision.reviews
    assert review.order is None and review.flags == ("frozen",)
    (entry,) = [f for f in decision.funnel if f.symbol == "B"]
    assert entry.stage is FunnelStage.NOT_TAKEN and "no free position slot" in entry.reason
    # Marked at 500 / 0.5 = 1,000: the equity the stop never saw.
    assert decision.equity == book(held).cash + 40 * D("1000")


# ---------------------------------------------------------------- loader
def _csv(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "corporate_actions.csv"
    path.write_text("\n".join(("symbol,ex_session,kind,ratio,confirmed_on,note", *rows)) + "\n")
    return path


def test_the_committed_file_is_header_only() -> None:
    assert load_corporate_actions(CONFIG / "corporate_actions.csv") == ()


def test_rows_parse(tmp_path: Path) -> None:
    rows = load_corporate_actions(
        _csv(
            tmp_path,
            "reliance,2024-10-28,bonus_split,2,2026-09-26,1:1 bonus",
            "ITC,2025-01-06,DEMERGER,0.96,2026-09-26,hotels",
            "X,2025-02-03,BONUS_SPLIT,0.1,2026-09-26,10:1 consolidation",
            "Y,2025-02-03,price_correction,1.02,2026-09-26,Dhan fix",
        )
    )
    assert [(r.symbol, r.kind, r.ratio) for r in rows] == [
        ("RELIANCE", BONUS, D("2")),
        ("ITC", DEMERGER, D("0.96")),
        ("X", BONUS, D("0.1")),
        ("Y", PRICE_CORRECTION, D("1.02")),
    ]


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("A,2026-01-05,SPLIT,2,2026-09-26,", "BONUS_SPLIT, DEMERGER or PRICE_CORRECTION"),
        ("A,2026-01-05,BONUS_SPLIT,1,2026-09-26,", "positive and not 1"),
        ("A,2026-01-05,BONUS_SPLIT,0,2026-09-26,", "positive and not 1"),
        ("A,2026-01-05,BONUS_SPLIT,-0.1,2026-09-26,", "positive and not 1"),
        ("A,2026-01-05,PRICE_CORRECTION,1,2026-09-26,", "positive and not 1"),
        ("A,2026-01-05,DEMERGER,1.2,2026-09-26,", "between 0 and 1"),
        ("A,2026-01-05,BONUS_SPLIT,two,2026-09-26,", "expected a number"),
        ("A,05/01/2026,BONUS_SPLIT,2,2026-09-26,", "ISO date"),
        ("A,2026-01-05,BONUS_SPLIT,2,,", "confirmed_on is blank"),
    ],
)
def test_a_bad_row_fails_closed(tmp_path: Path, row: str, message: str) -> None:
    with pytest.raises(InputFileError, match=message):
        load_corporate_actions(_csv(tmp_path, row))


def test_a_duplicate_fails_closed(tmp_path: Path) -> None:
    path = _csv(
        tmp_path, "A,2026-01-05,BONUS_SPLIT,2,2026-09-26,", "a,2026-01-05,BONUS_SPLIT,2,2026-09-26,"
    )
    with pytest.raises(InputFileError, match="confirmed twice"):
        load_corporate_actions(path)


def test_a_missing_file_or_wrong_header_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(InputFileError, match="does not exist"):
        load_corporate_actions(tmp_path / "missing.csv")
    bad = tmp_path / "bad.csv"
    bad.write_text("symbol,ex_session,kind,ratio\n")
    with pytest.raises(InputFileError, match="header is wrong"):
        load_corporate_actions(bad)


# ================================================= v1.2k: consolidations
def test_a_10_1_consolidation_of_95_shares() -> None:
    position = _shares(95, 100.0)
    daily = _bars({position.buys[0].session: 1000.0}, friday(205), default=1000.0)
    found = detect(position, daily)
    assert found is not None and found.factor == D("10")
    rescale = resolve(position, found, [_row("0.1", monday_after(202))], daily)
    assert isinstance(rescale, Rescale)
    assert (rescale.shares_before, rescale.shares_after) == (95, 9)
    # Half a new share in lieu, at the adjusted (x10) pre-ex close of 1,000.
    assert rescale.adjustment.cash == D("500.00")
    adjusted = _with(position, rescale.adjustment)
    assert adjusted.shares_held == 9
    assert adjusted.buy_value == position.buy_value  # rupee cost unchanged
    assert (adjusted.p1, adjusted.stop) == (position.p1 * 10, position.stop * 10)


def test_a_consolidation_that_floors_to_zero_pays_everything_in_lieu() -> None:
    position = _shares(1, 100.0)
    daily = _bars({position.buys[0].session: 1000.0}, friday(205), default=1000.0)
    found = detect(position, daily)
    assert found is not None
    rescale = resolve(position, found, [_row("0.1", monday_after(202))], daily)
    assert isinstance(rescale, Rescale)
    assert rescale.shares_after == 0 and rescale.adjustment.shares_delta == -1
    assert rescale.adjustment.cash == D("100.00")  # 0.1 share x 1,000
    closed = replace(position, adjustments=(rescale.adjustment,), state=PositionState.CLOSED)
    assert closed.shares_held == 0
    # D101: dated to the ex session's week; no sell costs.
    assert closed.exit_week == week(203)
    assert closed.net_pnl == D("100.00") - closed.buy_value - closed.fees


def _shares(n: int, price: float) -> Position:
    """A T1 of exactly ``n`` shares at ``price`` (zero costs)."""
    position = open_position("A", atr_pct=0.06, p1=price, fill_week=200)
    buy = replace(position.buys[0], shares=n, fees=D("0"))
    return replace(position, buys=(buy,))


# =============================================== v1.2k: price corrections
def test_a_downward_price_correction_credits_nothing() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    daily = _bars({position.buys[0].session: 990.0}, friday(205), default=990.0)
    found = detect(position, daily)
    assert found is not None and found.factor == D("0.99")
    rescale = resolve(position, found, [_row("0.99", monday_after(202), PRICE_CORRECTION)], daily)
    assert isinstance(rescale, Rescale)
    assert rescale.adjustment.cash == 0 and rescale.shares_after == 40
    adjusted = _with(position, rescale.adjustment)
    assert (adjusted.p1, adjusted.stop) == (D("990.00"), D("693.00"))


def test_an_upward_price_correction_resolves() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    daily = _bars({position.buys[0].session: 1020.0}, friday(205), default=1020.0)
    found = detect(position, daily)
    assert found is not None and found.factor == D("1.02")
    rescale = resolve(position, found, [_row("1.02", monday_after(202), PRICE_CORRECTION)], daily)
    assert isinstance(rescale, Rescale) and rescale.adjustment.cash == 0
    assert _with(position, rescale.adjustment).stop == D("714.00")


def test_the_same_factor_confirmed_as_a_demerger_still_credits_cash() -> None:
    # The audit's case: f = 0.99 as DEMERGER credits 40 x 1,000 x 0.01 (the
    # pre-ex close here is 990 cached, 1,000 actual) = 400.00.
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    daily = _bars({position.buys[0].session: 990.0}, friday(205), default=990.0)
    found = detect(position, daily)
    assert found is not None
    rescale = resolve(position, found, [_row("0.99", monday_after(202), DEMERGER)], daily)
    assert isinstance(rescale, Rescale) and rescale.adjustment.cash == D("400.00")


# ================================================ v1.2k item 7: raw gaps
def _gap_bars(position: Position, closes: dict[date, float], last: date) -> list[DailyBar]:
    """Bars at 1,000 up to each date in ``closes``, then that close onwards."""
    bars = _bars({}, last)
    out = []
    level = 1000.0
    for bar in bars:
        level = closes.get(bar.session, level)
        out.append(DailyBar(bar.session, level, level + 1, level - 1, level, 1e6))
    return out


def test_a_raw_gap_after_the_first_fill_is_unadjusted() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    daily = _gap_bars(position, {monday_after(201): 500.0}, friday(203))
    (gap,) = unadjusted_gaps(position, daily, ())
    assert gap.session == monday_after(201) and gap.ratio == D("0.5000")
    assert gap_factor([gap]) == D("0.5")
    assert gap_detail([gap]) == f"unadjusted gap {monday_after(201)} ratio 0.5000"


def test_a_gap_on_or_before_the_first_fill_session_does_not_count() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    t1 = position.buys[0].session
    for day in (t1, t1 - timedelta(days=7)):
        assert unadjusted_gaps(position, _gap_bars(position, {day: 500.0}, friday(203)), ()) == ()


def test_a_12_percent_move_and_an_acknowledged_gap_do_not_count() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    assert (
        unadjusted_gaps(position, _gap_bars(position, {monday_after(201): 880.0}, friday(203)), ())
        == ()
    )
    daily = _gap_bars(position, {monday_after(201): 800.0}, friday(203))
    ack = GapAcknowledgement("A", monday_after(201), D("0.8000"), date(2026, 9, 26))
    assert unadjusted_gaps(position, daily, (ack,)) == ()
    wrong = GapAcknowledgement("A", monday_after(201), D("0.7900"), date(2026, 9, 26))
    assert len(unadjusted_gaps(position, daily, (wrong,))) == 1


def test_two_gaps_multiply() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    daily = _gap_bars(position, {monday_after(201): 500.0, monday_after(202): 600.0}, friday(203))
    assert gap_factor(unadjusted_gaps(position, daily, ())) == D("0.6")


def test_t2_on_the_gap_session_then_restated_rescales_t1_only() -> None:
    """Test (i), pure: T1 before the gap, T2 filled on the gap session (new units)."""
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    ex = monday_after(201)
    position = fill(position, OrderAction.BUY_T2, price=450.0, week_index=202)
    assert position.buys[1].session == ex
    raw = _gap_bars(position, {ex: 450.0}, friday(203))
    assert len(unadjusted_gaps(position, raw, ())) == 1  # frozen while unrestated
    restated = [
        DailyBar(b.session, b.open * 0.5, b.high * 0.5, b.low * 0.5, b.close * 0.5, b.volume)
        if b.session < ex
        else b
        for b in raw
    ]
    assert unadjusted_gaps(position, restated, ()) == ()
    found = detect(position, restated)
    assert found is not None
    rescale = resolve(position, found, [_row("2", ex)], restated)
    assert isinstance(rescale, Rescale)
    assert rescale.adjustment.applies_to == (OrderAction.BUY_T1,)
    assert (rescale.shares_before, rescale.shares_after) == (40, 80)
    assert _with(position, rescale.adjustment).shares_held == 80 + position.buys[1].shares


# ============================================ D102: the stuck-exit row
def test_the_stuck_exit_row_must_explain_the_freeze_factor() -> None:
    position = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=200)
    bonus = _row("2", monday_after(202))
    assert stuck_exit_row(position, [bonus], D("0.5")) == bonus
    assert stuck_exit_row(position, [bonus], D("0.6")) is None
    assert (
        stuck_exit_row(position, [_row("0.9", monday_after(202), DEMERGER)], D("0.9")) is not None
    )
    # A row for the T1 fill's own session or earlier cannot be the cause.
    assert stuck_exit_row(position, [_row("2", position.buys[0].session)], D("0.5")) is None
