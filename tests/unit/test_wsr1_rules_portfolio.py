"""Phase 3: the book — spec 4.12-4.13 and the V1 plan's section 5 example.

Positions A-D of ``WSR1_V1_GOLDEN_CASES.md`` section 5 are built by real fills
and every number in that table is asserted exactly (costs zeroed: the plan's
numbers are before costs). The paper book's 10 positions / 100% committed cap
replace the plan's 8 / 80% (an intentional difference, listed at the top of
that file), so "slots left" and "room" differ from the plan's prose by design.

Also: v1.2f answers C (brake 1) with the allowed and the refused case, D
(brake 2 clearing resets the peak), and G (a decided SELL_ALL frees its slot;
a decided SELL_HALF recomputes committed for the same week's entries).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from _wsr1_rules_fixtures import (
    PARAMS0,
    D,
    Tape,
    book,
    ctx,
    fill,
    friday,
    index_series,
    kd_tape,
    open_position,
    symbol_week,
    universe_row,
    week,
)

from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    BrakeState,
    FunnelStage,
    OrderAction,
    PendingOrder,
    Position,
    RulesParameters,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import (
    SymbolWeek,
    decide_week,
    entries_blocked_by_brakes,
    mark_to_market,
    sizing,
    update_brakes,
)

T = 209
_TRIGGER: list[tuple[float | None, float | None]] = [(15.0, 18.0), (20.0, 22.0), (28.0, 25.0)]


# ====================================== V1 plan section 5: ₹10L example
def _example() -> tuple[dict[str, Position], Decimal]:
    """Positions A-D by real fills from ₹10,00,000; returns them and the cash."""
    cash = D("1000000")
    a = open_position("A", atr_pct=0.06, p1=1000.0, fill_week=190, sector="Finance")
    b = open_position("B", atr_pct=0.08, p1=2400.0, fill_week=190, sector="Finance")
    b = fill(b, OrderAction.BUY_T2, price=2150.0, week_index=195)
    c = open_position("C", atr_pct=0.075, p1=560.0, fill_week=180, sector="Technology")
    c = fill(c, OrderAction.SELL_HALF, price=690.0, week_index=200, quantity=31)
    d = open_position(
        "D", atr_pct=0.09, p1=3200.0, fill_week=195, sector="Utilities", event_risk=True
    )
    for position in (a, b, c, d):
        cash -= position.buy_value
        cash += position.sale_value
    return {"A": a, "B": b, "C": c, "D": d}, cash


def _reserved(position: Position) -> Decimal:
    amounts = position.sizing.tranche_amounts[position.tranches_used : position.max_tranches]
    return sum(amounts, D("0")) if position.next_level is not None else D("0")


def test_section5_how_bs_numbers_come_out() -> None:
    """A = 1L x 10% / 12% = 83,333 -> T1 33,333 -> 13 shares (31,200); L1 2,112,
    L2 1,824, stop 1,536; T2 25,000 -> 11 shares at 2,150 (23,650)."""
    b = _example()[0]["B"]
    assert b.sizing.allocation == D("83333.33")
    assert b.sizing.tranche_amounts == (D("33333.33"), D("25000.00"), D("25000.00"))
    assert [(x.shares, x.value) for x in b.buys] == [(13, D("31200.00")), (11, D("23650.00"))]
    assert (b.l1, b.l2, b.stop) == (D("2112.00"), D("1824.00"), D("1536.00"))


def test_section5_the_snapshot_table_row_by_row() -> None:
    positions, _ = _example()
    a, b, c, d = (positions[s] for s in "ABCD")
    # Shares | Deployed | Reserved | Committed | exit level
    assert [p.shares_held for p in (a, b, c, d)] == [40, 24, 32, 4]
    assert [p.cost_of_held for p in (a, b, c, d)] == [
        D("40000.00"),
        D("54850.00"),
        D("17920.00"),
        D("12800.00"),
    ]
    assert [_reserved(p) for p in (a, b, c, d)] == [
        D("60000.00"),
        D("25000.00"),
        D("0"),
        D("11111.11"),
    ]
    assert [p.committed.quantize(D("1")) for p in (a, b, c, d)] == [
        D("100000"),
        D("83333"),
        D("17920"),
        D("25926"),
    ]
    assert [a.stop, b.stop, d.stop] == [D("700.00"), D("1536.00"), D("1904.00")]
    assert c.sizing.spacing == D("0.1125") and c.sizing.allocation == D("88888.89")


def test_section5_totals() -> None:
    positions, _ = _example()
    held = list(positions.values())
    assert sum((p.cost_of_held for p in held), D("0")) == D("125570.00")
    assert sum((_reserved(p) for p in held), D("0")).quantize(D("1")) == D("96111")
    committed = sum((p.committed for p in held), D("0"))
    assert committed.quantize(D("1")) == D("227179")
    assert (committed / D("1000000") * 100).quantize(D("0.1")) == D("22.7")


def test_section5_if_every_exit_is_hit_now() -> None:
    """A stop -12,000 · B stop -17,986 · C trail at ~650 +2,880 · D stop -5,184 = -32,290."""
    p, _ = _example()
    now = {
        "A": p["A"].stop * 40 - p["A"].buy_value,
        "B": p["B"].stop * 24 - p["B"].buy_value,
        "C": (D("650") - D("560")) * 32,
        "D": p["D"].stop * 4 - p["D"].buy_value,
    }
    assert now == {"A": D("-12000.00"), "B": D("-17986.00"), "C": D("2880"), "D": D("-5184.00")}
    assert sum(now.values(), D("0")) == D("-32290.00")


def test_section5_worst_case_every_remaining_tranche_fills_at_its_level() -> None:
    """A -22,300 · B -21,730 · C +2,880 · D -8,640 -> -49,790."""
    p, _ = _example()
    a = fill(p["A"], OrderAction.BUY_T2, price=900.0, week_index=200)
    a = fill(a, OrderAction.BUY_T3, price=800.0, week_index=201)
    b = fill(p["B"], OrderAction.BUY_T3, price=1824.0, week_index=200)
    d = fill(p["D"], OrderAction.BUY_T2, price=2768.0, week_index=200)
    losses = [x.stop * x.shares_held - x.buy_value for x in (a, b, d)]
    assert losses == [D("-22300.00"), D("-21730.00"), D("-8640.00")]
    assert sum(losses, D("2880")) == D("-49790.00")
    # The per-stock maxima of the limits table: 12,000 T1 only, ~18,600 T1 + T2.
    t1_t2 = fill(p["A"], OrderAction.BUY_T2, price=900.0, week_index=200)
    assert t1_t2.stop * t1_t2.shares_held - t1_t2.buy_value == D("-18600.00")


def test_section5_cash_equity_and_realised() -> None:
    """Realised +4,030; cash 8,78,460; positions 1,29,480; equity 10,07,940."""
    positions, cash = _example()
    c = positions["C"]
    assert c.sale_value - c.average_cost * 31 == D("4030")
    assert cash == D("878460.00")
    closes = {"A": 1060.0, "B": 2180.0, "C": 705.0, "D": 3050.0}
    held = book(*positions.values(), cash=cash)
    assert mark_to_market(held, closes) == D("1007940.00")
    assert mark_to_market(held, closes) - cash == D("129480.00")


def _example_week(extra: dict[str, SymbolWeek]) -> tuple[dict[str, SymbolWeek], object]:
    positions, cash = _example()
    closes = {"A": 1060.0, "B": 2180.0, "C": 705.0, "D": 3050.0}
    symbols = {
        s: symbol_week(s, Tape(close=closes[s], ema10=650.0).series(), industry=positions[s].sector)
        for s in closes
    }
    symbols.update(extra)
    return symbols, book(*positions.values(), cash=cash)


def test_section5_finance_is_full_technology_has_room() -> None:
    """A new bank/NBFC signal is skipped (Finance 2 of 2); Technology has a slot left."""
    trigger = kd_tape(_TRIGGER).series()
    symbols, held = _example_week(
        {
            "FIN": symbol_week("FIN", trigger, industry="Finance"),
            "TEC": symbol_week("TEC", trigger, industry="Technology"),
        }
    )
    decision = decide_week(ctx(T), held, symbols, index_series(), PARAMS0)  # type: ignore[arg-type]
    funnel = {e.symbol: e for e in decision.funnel}
    assert funnel["FIN"].stage is FunnelStage.NOT_TAKEN
    assert funnel["FIN"].reason == "sector full: Finance"
    assert funnel["TEC"].stage is FunnelStage.TAKEN
    assert [o.symbol for o in decision.orders] == ["TEC"]
    # Paper book: 4 of 10 slots used; committed room is 10L - 2,27,179.26.
    assert PARAMS0.max_positions - 4 == 6
    assert PARAMS0.committed_cap - D("227179.26") == D("772820.74")


# ============================= v1.2f answer G: this week's exits count
def _ten_positions(stop_one: bool) -> tuple[dict[str, SymbolWeek], object]:
    positions = [
        open_position(f"H{i}", atr_pct=0.06, p1=1000.0, fill_week=200, sector=f"S{i}")
        for i in range(10)
    ]
    symbols = {
        p.symbol: symbol_week(
            p.symbol, Tape(close=650.0 if stop_one and i == 0 else 1000.0).series()
        )
        for i, p in enumerate(positions)
    }
    symbols["NEW"] = symbol_week("NEW", kd_tape(_TRIGGER).series(), industry="S-new")
    return symbols, book(*positions, cash=D("600000"))


def test_g_a_decided_sell_all_frees_its_slot_for_this_weeks_entries() -> None:
    symbols, full = _ten_positions(stop_one=False)
    refused = decide_week(ctx(T), full, symbols, index_series(), PARAMS0)  # type: ignore[arg-type]
    (new,) = [e for e in refused.funnel if e.symbol == "NEW"]
    assert new.stage is FunnelStage.NOT_TAKEN and "no free position slot" in new.reason

    symbols, full = _ten_positions(stop_one=True)
    allowed = decide_week(ctx(T), full, symbols, index_series(), PARAMS0)  # type: ignore[arg-type]
    actions = sorted((o.symbol, o.action) for o in allowed.orders)
    assert actions == [("H0", OrderAction.SELL_ALL), ("NEW", OrderAction.BUY_T1)]


def test_g_a_decided_sell_half_recomputes_committed() -> None:
    """Cap 320,000: three OPEN positions commit 300,000, so a 100,000 entry does
    not fit — until one decides a partial sale, which commits only the cost of
    the 20 shares that remain (20,000)."""
    params = RulesParameters(
        committed_cap_pct=D("32"),
        cost_bps_buy=D("0"),
        cost_bps_sell=D("0"),
        fixed_cost_per_sell_rs=D("0"),
    )

    def run(partial: bool) -> FunnelStage:
        positions = [
            open_position(f"H{i}", atr_pct=0.06, p1=1000.0, fill_week=200, sector=f"S{i}")
            for i in range(3)
        ]
        symbols = {
            p.symbol: symbol_week(
                p.symbol,
                Tape(
                    k=95.0 if partial and i == 0 else 50.0, d=95.0 if partial and i == 0 else 50.0
                ).series(),
            )
            for i, p in enumerate(positions)
        }
        symbols["NEW"] = symbol_week("NEW", kd_tape(_TRIGGER).series(), industry="S-new")
        decision = decide_week(ctx(T), book(*positions), symbols, index_series(), params)
        (new,) = [e for e in decision.funnel if e.symbol == "NEW"]
        return new.stage

    assert run(partial=False) is FunnelStage.NOT_TAKEN
    assert run(partial=True) is FunnelStage.TAKEN


def test_sale_proceeds_are_not_cash_until_filled() -> None:
    """A position stopping out this week frees its slot but not its cash."""
    stopping = open_position("H0", atr_pct=0.06, p1=1000.0, fill_week=200, sector="S0")
    symbols = {
        "H0": symbol_week("H0", Tape(close=650.0).series()),
        "NEW": symbol_week("NEW", kd_tape(_TRIGGER).series(), industry="S-new"),
    }
    decision = decide_week(ctx(T), book(stopping, cash=D("100")), symbols, index_series(), PARAMS0)
    (new,) = [e for e in decision.funnel if e.symbol == "NEW"]
    assert new.reason == "not enough cash"


# ======================================================= limits
def test_one_position_per_promoter_group() -> None:
    trigger = kd_tape(_TRIGGER).series()
    grouped = {
        s: SymbolWeek(
            s,
            trigger,
            universe_row(s, f"I-{s}", group="ADANI"),
            symbol_week(s, trigger).quality,
            (),
            3e8,
        )
        for s in ("ADA1", "ADA2")
    }
    decision = decide_week(ctx(T), book(), grouped, index_series(), PARAMS0)
    assert len(decision.orders) == 1
    refused = [e for e in decision.funnel if e.stage is FunnelStage.NOT_TAKEN]
    assert [e.reason for e in refused] == ["promoter group full: ADANI"]


def test_the_committed_cap_and_the_buffer() -> None:
    trigger = kd_tape(_TRIGGER).series()
    params = RulesParameters(committed_cap_pct=D("5"))
    decision = decide_week(ctx(T), book(), {"S": symbol_week("S", trigger)}, index_series(), params)
    assert decision.funnel[0].reason == "committed-capital cap"
    buffered = RulesParameters(buffer_pct=D("99"))
    decision = decide_week(
        ctx(T), book(), {"S": symbol_week("S", trigger)}, index_series(), buffered
    )
    assert decision.funnel[0].reason == "not enough cash"


def test_a_buy_reserves_its_amount_plus_costs() -> None:
    trigger = kd_tape(_TRIGGER).series()
    exact = D("40000")  # T1 amount, but not the 12 bps on top
    decision = decide_week(
        ctx(T),
        book(cash=exact),
        {"S": symbol_week("S", trigger)},
        index_series(),
        RulesParameters(),
    )
    assert decision.funnel[0].reason == "not enough cash"
    decision = decide_week(
        ctx(T),
        book(cash=D("40048")),
        {"S": symbol_week("S", trigger)},
        index_series(),
        RulesParameters(),
    )
    assert decision.orders and decision.orders[0].amount == D("40000.00")


# ============================== v1.2f answer C: brake 1, both cases
PEAK = D("1000000")


def _brakes(equities: list[str], start: int = 100) -> list[tuple[BrakeState, str | None]]:
    state = BrakeState(peak=PEAK)
    out = []
    for i, equity in enumerate(equities):
        w = start + i
        state = update_brakes(state, D(equity), week(w), friday(w), PARAMS0)
        out.append((state, entries_blocked_by_brakes(state, week(w))))
    return out


def test_c_brake_1_pauses_four_decision_weeks_then_entries_resume_while_still_down() -> None:
    """Allowed: after 4 paused weeks, entries resume although equity is still <= 90%."""
    run = _brakes(["890000", "885000", "880000", "880000", "880000", "880000"])
    blocked = [why is not None for _, why in run]
    assert blocked == [True, True, True, True, False, False]
    assert all(state.peak == PEAK for state, _ in run)


def test_c_brake_1_fires_again_only_after_equity_closed_above_90_percent() -> None:
    """Refused: after recovering above 90% once, a new fall to <= 90% pauses again."""
    run = _brakes(["890000"] * 5 + ["910000", "895000"])
    blocked = [why is not None for _, why in run]
    assert blocked == [True, True, True, True, False, False, True]
    assert "brake 1" in (run[-1][1] or "")


def test_c_brake_1_at_decide_week_level() -> None:
    trigger = kd_tape(_TRIGGER).series()
    fell = book(cash=D("890000"), brakes=BrakeState(peak=PEAK))
    decision = decide_week(ctx(T), fell, {"S": symbol_week("S", trigger)}, index_series(), PARAMS0)
    assert decision.orders == ()
    assert decision.entries_blocked is not None and "brake 1" in decision.entries_blocked
    assert decision.brakes.brake1_until == week(T + 3)


# ============================================ v1.2f answer D: brake 2
def test_d_brake_2_holds_until_cleared_and_clearing_resets_the_peak() -> None:
    fired = update_brakes(BrakeState(peak=PEAK), D("790000"), week(100), friday(100), PARAMS0)
    assert fired.brake2_active and fired.brake2_fired_on == friday(100)
    later = update_brakes(fired, D("800000"), week(110), friday(110), PARAMS0)
    assert entries_blocked_by_brakes(later, week(110)) is not None and later.brake2_active

    cleared_params = RulesParameters(brake_2_cleared_on=friday(110))
    cleared = update_brakes(fired, D("800000"), week(110), friday(110), cleared_params)
    assert not cleared.brake2_active
    assert cleared.peak == D("800000")  # reset: 800,000 is not a drawdown any more
    assert entries_blocked_by_brakes(cleared, week(110)) is None


def test_d_a_clearance_dated_before_the_firing_does_not_clear_it() -> None:
    params = RulesParameters(brake_2_cleared_on=date(2020, 1, 1))
    fired = update_brakes(BrakeState(peak=PEAK), D("790000"), week(100), friday(100), params)
    later = update_brakes(fired, D("800000"), week(101), friday(101), params)
    assert later.brake2_active


def test_the_peak_is_the_running_maximum_of_equity() -> None:
    state = BrakeState()
    for i, equity in enumerate(["1000000", "1100000", "1050000"]):
        state = update_brakes(state, D(equity), week(i + 100), friday(i + 100), PARAMS0)
    assert state.peak == D("1100000")
    assert not state.brake2_active and state.brake1_until is None


# ============ v1.2g fix 1: unfilled BUY orders hold capacity as if filled
def _pending_t1(symbol: str, *, group: str, sector: str) -> PendingOrder:
    size = sizing(0.06, False, PARAMS0)
    return PendingOrder(
        OrderAction.BUY_T1,
        symbol,
        week(T - 1),
        friday(T - 1),
        "entry",
        amount=size.tranche_amounts[0],
        sizing=size,
        sector=sector,
        group=group,
    )


def test_pending_buy_t1_holds_its_promoter_group() -> None:
    pending = _pending_t1("ADA1", group="ADANI", sector="Power")
    second = SymbolWeek(
        "ADA2",
        kd_tape(_TRIGGER).series(),
        universe_row("ADA2", "Ports", group="ADANI"),
        symbol_week("ADA2", kd_tape(_TRIGGER).series()).quality,
        (),
        3e8,
    )
    decision = decide_week(
        ctx(T), book(pending=(pending,)), {"ADA2": second}, index_series(), PARAMS0
    )
    assert decision.orders == ()
    assert decision.funnel[0].reason == "promoter group full: ADANI"


def test_pending_buy_t1_holds_its_cash() -> None:
    # Cash covers exactly one T1 (40,000 + 12 bps); the pending one takes it.
    pending = _pending_t1("P", group="P", sector="S-p")
    decision = decide_week(
        ctx(T),
        book(cash=D("40048"), pending=(pending,)),
        {"S": symbol_week("S", kd_tape(_TRIGGER).series())},
        index_series(),
        RulesParameters(),
    )
    assert decision.orders == ()
    assert decision.funnel[0].reason == "not enough cash"


def test_nine_held_and_one_pending_fill_all_ten_slots() -> None:
    positions = [
        open_position(f"H{i}", atr_pct=0.06, p1=1000.0, fill_week=200, sector=f"S{i}")
        for i in range(9)
    ]
    symbols = {p.symbol: symbol_week(p.symbol, Tape().series()) for p in positions}
    symbols["NEW"] = symbol_week("NEW", kd_tape(_TRIGGER).series(), industry="S-new")
    pending = _pending_t1("P", group="P", sector="S-p")
    decision = decide_week(
        ctx(T),
        book(*positions, cash=D("640000"), pending=(pending,)),
        symbols,
        index_series(),
        PARAMS0,
    )
    (new,) = [e for e in decision.funnel if e.symbol == "NEW"]
    assert new.stage is FunnelStage.NOT_TAKEN
    assert "no free position slot" in new.reason


def test_pending_add_reserves_its_cash() -> None:
    held = open_position("H", atr_pct=0.06, p1=1000.0, fill_week=200, sector="S-h")
    add = PendingOrder(
        OrderAction.BUY_T2,
        "H",
        week(T - 1),
        friday(T - 1),
        "add",
        position_id=held.position_id,
        amount=D("30000.00"),
    )
    symbols = {
        "H": symbol_week("H", Tape().series()),
        "S": symbol_week("S", kd_tape(_TRIGGER).series(), industry="S-new"),
    }
    # 60,000 covers the new T1 (40,000) but not after the pending add's 30,000.
    decision = decide_week(
        ctx(T), book(held, cash=D("60000"), pending=(add,)), symbols, index_series(), PARAMS0
    )
    (s,) = [e for e in decision.funnel if e.symbol == "S"]
    assert s.reason == "not enough cash"


# ================= v1.2g fix 5: clearing brake 2 ends a brake-1 pause
def test_clearing_brake_2_also_ends_a_running_brake_1_pause() -> None:
    both = BrakeState(
        peak=PEAK,
        brake1_until=week(112),
        brake1_can_fire=False,
        brake2_fired_on=friday(109),
    )
    assert entries_blocked_by_brakes(both, week(110)) is not None
    params = RulesParameters(brake_2_cleared_on=friday(110))
    cleared = update_brakes(both, D("790000"), week(110), friday(110), params)
    assert not cleared.brake2_active
    assert cleared.brake1_until is None
    assert entries_blocked_by_brakes(cleared, week(110)) is None
    # The peak was reset to 790,000, so this week is no drawdown at all.
    assert cleared.peak == D("790000")


# ======================= v1.2h: book consistency and over-limit books
def test_the_book_rejects_a_pending_entry_for_a_held_symbol() -> None:
    held = open_position("H", atr_pct=0.06, p1=1000.0, fill_week=200, sector="S-h")
    with pytest.raises(ValueError, match="pending BUY_T1 for held"):
        book(held, pending=(_pending_t1("H", group="H", sector="S-h"),))


@pytest.mark.parametrize(
    "action", [OrderAction.BUY_T2, OrderAction.SELL_HALF, OrderAction.SELL_ALL]
)
def test_the_book_rejects_a_pending_add_or_sell_for_a_position_not_held(
    action: OrderAction,
) -> None:
    orphan = PendingOrder(
        action,
        "GONE",
        week(T - 1),
        friday(T - 1),
        "stale",
        position_id="GONE-2025W01",
        amount=D("30000") if action.is_buy else None,
        quantity=None if action.is_buy else 10,
    )
    with pytest.raises(ValueError, match="not held"):
        book(pending=(orphan,))


def test_a_book_over_the_slot_limit_is_reported_and_takes_no_entry() -> None:
    """v1.2h: an exit that failed to fill can leave 11 positions held; that is
    accepted, reported, and no entry is taken while over the limit."""
    positions = [
        open_position(f"H{i}", atr_pct=0.06, p1=1000.0, fill_week=200, sector=f"S{i}")
        for i in range(11)
    ]
    exiting = PendingOrder(
        OrderAction.SELL_ALL,
        "H0",
        week(T - 1),
        friday(T - 1),
        "stop",
        position_id=positions[0].position_id,
        quantity=positions[0].shares_held,
    )
    symbols = {p.symbol: symbol_week(p.symbol, Tape().series()) for p in positions}
    symbols["NEW"] = symbol_week("NEW", kd_tape(_TRIGGER).series(), industry="S-new")
    decision = decide_week(
        ctx(T),
        book(*positions, cash=D("560000"), pending=(exiting,)),
        symbols,
        index_series(),
        PARAMS0,
    )
    assert any("11 positions held, over the limit of 10" in w for w in decision.warnings)
    (new,) = [e for e in decision.funnel if e.symbol == "NEW"]
    assert new.stage is FunnelStage.NOT_TAKEN
