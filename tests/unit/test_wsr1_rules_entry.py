"""Phase 3: entries — spec 4.5-4.7 and the V1 plan's section 7 table.

Every row of ``WSR1_V1_GOLDEN_CASES.md`` section 7 is one test here, named
after the row, plus spec 14's K >= 50 case and the v1.2f answers B (most recent
earlier trigger), E (regime Unknown), F (undefined K/D) and J (fresh arm after
an exit), each with an allowed and a refused case.

K/D tails are written oldest-first and end at the decision bar ``t``; bars
before the tail sit at K = D = 50, which neither arms nor crosses.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from _wsr1_rules_fixtures import (
    PARAMS,
    N,
    Tape,
    book,
    ctx,
    index_series,
    kd_tape,
    open_position,
    quality,
    symbol_week,
    universe_row,
    week,
)

from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    ClosedTrade,
    FunnelEntry,
    FunnelStage,
    OrderAction,
    QualityStatus,
    Regime,
    RulesParameters,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import (
    SymbolWeek,
    decide_week,
    regime,
    trigger,
)

T = N - 1  # the decision bar


def _entry(
    tape: Tape,
    *,
    held: bool = False,
    closed: tuple[ClosedTrade, ...] = (),
    params: RulesParameters = PARAMS,
    **inputs: Any,
) -> tuple[FunnelEntry, bool]:
    """Run the week for one symbol "S"; return its funnel entry and whether a BUY_T1 was made."""
    series = tape.series()
    last = tape.n - 1
    positions = (open_position("S", atr_pct=0.06, p1=1000.0, fill_week=last - 5),) if held else ()
    decision = decide_week(
        ctx(last),
        book(*positions, closed=closed),
        {"S": symbol_week("S", series, **inputs)},
        index_series(n=tape.n),
        params,
    )
    (entry,) = [e for e in decision.funnel if e.symbol == "S"]
    bought = any(o.action is OrderAction.BUY_T1 and o.symbol == "S" for o in decision.orders)
    return entry, bought


def _taken(entry: tuple[FunnelEntry, bool]) -> bool:
    funnel, bought = entry
    assert (funnel.stage is FunnelStage.TAKEN) == bought
    return bought


# ------------------------------------------------ section 7, row by row
def test_k_and_d_below_20_without_a_cross_is_not_yet() -> None:
    """Row 1: "K and D below 20, but K has not crossed above D" -> NO (not yet)."""
    funnel, bought = _entry(kd_tape([(12, 16), (15, 18)]))
    assert not bought
    assert funnel.stage is FunnelStage.ARMED


def test_a_cross_without_an_oversold_close_in_8_weeks_is_not_armed() -> None:
    """Row 2: cross, but K and D never both below 20 in the last 8 closes -> NO."""
    funnel, bought = _entry(kd_tape([(30, 35)] * 9 + [(40, 35)]))
    assert not bought
    assert funnel.stage is FunnelStage.NOT_ARMED


def test_the_window_restarts_on_every_oversold_close() -> None:
    """Row 3: first oversold 10 weeks ago, but an oversold close within the last 8 -> YES."""
    tail = [(10, 15)] + [(22, 25)] * 6 + [(15, 18), (22, 25), (24, 26), (30, 27)]
    assert len(tail) == 11
    assert _taken(_entry(kd_tape(tail)))


def test_an_expired_window_is_not_armed() -> None:
    """Row 4: latest both-below-20 close more than 7 weeks ago -> NO."""
    tail = [(10, 15)] + [(25, 30)] * 9 + [(35, 30)]
    funnel, bought = _entry(kd_tape(tail))
    assert not bought
    assert funnel.stage is FunnelStage.NOT_ARMED


#: Two full signal episodes, then a third trigger at the decision bar. In
#: each episode the oversold close is followed by a cross two bars in, then
#: K drifts back below D without arming again.
_EPISODE = [(10, 15), (12, 16), (22, 18), (40, 35), (50, 45), (45, 48), (30, 35), (25, 30)]
_FINAL = [(10, 15), (12, 16), (22, 18)]
#: Bars (from the end) of the three triggers: t-16, t-8 and t.
_TRIGGERS = (-17, -9, -1)


def _repeat_tape(closes: dict[int, float], prior_high: float) -> Tape:
    tail = _EPISODE + _EPISODE + _FINAL
    # The prior week closes below its own high (a bar's high is never below its close).
    close: dict[int, float | None] = {-2: prior_high - 20.0, **closes}
    return kd_tape(tail, close=close, high={-2: prior_high})


def test_repeat_signal_at_or_above_the_previous_triggers_close() -> None:
    """Row 5: another signal within 6 months, close >= the previous trigger's -> YES."""
    tape = _repeat_tape({-9: 950.0, -1: 1000.0}, prior_high=1010.0)
    series = tape.series()
    assert [trigger(series, N + j, PARAMS).triggered for j in _TRIGGERS] == [True, True, True]
    assert _taken(_entry(tape))


def test_repeat_signal_below_the_previous_close_needs_a_close_above_the_prior_high() -> None:
    """Row 6: close < the previous trigger's close -> YES only above the prior week's high."""
    above = _repeat_tape({-9: 1100.0, -1: 1000.0}, prior_high=990.0)
    assert _taken(_entry(above))
    below = _repeat_tape({-9: 1100.0, -1: 1000.0}, prior_high=1010.0)
    funnel, bought = _entry(below)
    assert not bought
    assert funnel.stage is FunnelStage.FILTERED
    assert "repeat signal" in funnel.reason


def test_a_signal_while_holding_the_stock_is_refused() -> None:
    """Row 7: another signal while you still hold the stock -> NO (one open trade)."""
    funnel, bought = _entry(kd_tape([(15, 18), (20, 22), (28, 25)]), held=True)
    assert not bought
    assert funnel.stage is FunnelStage.SKIPPED
    assert funnel.reason == "already held"


def test_a_signal_within_26_weeks_of_a_loss_exit_is_cooling_off() -> None:
    """Row 8: another signal within 26 weeks of a loss exit -> NO."""
    loss = ClosedTrade("S", "S-old", week(T - 10), Decimal("-5000"))
    funnel, bought = _entry(kd_tape([(15, 18), (20, 22), (28, 25)]), closed=(loss,))
    assert not bought
    assert "cooling-off" in funnel.reason


def test_below_the_200w_ema_an_entry_is_still_allowed() -> None:
    """Row 9: price below the 200W EMA, other filters pass -> entry YES.
    (Adds NO below the EMA200: test_wsr1_rules_positions.py.)"""
    assert _taken(_entry(kd_tape([(15, 18), (20, 22), (28, 25)], ema200=1200.0)))


@pytest.mark.parametrize(
    "row",
    [
        quality("S", QualityStatus.FAIL),
        quality("S", QualityStatus.PASS, valid_until=date(2020, 1, 1)),
        None,
    ],
    ids=["fail", "expired", "missing"],
)
def test_failing_the_quality_filter_is_no(row: object) -> None:
    """Row 10: passes the Stoch RSI rules but fails the quality filter -> NO."""
    funnel, bought = _entry(kd_tape([(15, 18), (20, 22), (28, 25)]), quality_row=row or False)
    assert not bought
    assert "needs quality check" in funnel.reason


def test_a_midweek_cross_that_is_gone_at_the_close_is_no() -> None:
    """Row 11: K crossed above D midweek but K <= D at Friday's close -> NO.
    Only the weekly close exists here, so this is K below D at the close."""
    funnel, bought = _entry(kd_tape([(15, 18), (17, 19), (19, 20)]))
    assert not bought
    assert funnel.stage is FunnelStage.ARMED


def test_k_equal_to_d_at_the_close_is_no() -> None:
    """Row 12: K = D exactly at the close -> NO (needs K > D)."""
    funnel, bought = _entry(kd_tape([(15, 18), (17, 19), (21, 21)]))
    assert not bought
    assert "K = D" in funnel.reason


def test_a_cross_at_k_28_while_armed_is_yes() -> None:
    """Row 13: the cross happens with K at 28 while armed -> YES."""
    assert _taken(_entry(kd_tape([(15, 18), (20, 22), (28, 25)])))


def test_results_in_the_order_week_is_no() -> None:
    """Row 14: valid trigger, but results due in the order week -> NO."""
    execution = ctx().execution_date
    funnel, bought = _entry(
        kd_tape([(15, 18), (20, 22), (28, 25)]),
        results_dates=(execution + timedelta(days=3),),
    )
    assert not bought
    assert "results in the execution week" in funnel.reason


def test_results_date_unknown_proceeds_and_is_flagged() -> None:
    """Spec 4.6 item 6 (paper): no calendar row -> proceed, flag it."""
    funnel, bought = _entry(kd_tape([(15, 18), (20, 22), (28, 25)]), results_dates=None)
    assert bought
    assert "results date unknown" in funnel.flags


# ------------------------------------------------------ spec 14 extras
def test_k_at_or_above_50_at_the_cross_is_no() -> None:
    funnel, bought = _entry(kd_tape([(15, 18), (20, 22), (50, 45)]))
    assert not bought
    assert ">= 50" in funnel.reason


def test_only_the_first_cross_since_the_latest_oversold_close_triggers() -> None:
    # Oversold at t-3 and t-2 (t-1 is not: D = 21); K crossed D at t-2 already,
    # so the cross at t is a second one.
    funnel, bought = _entry(kd_tape([(10, 15), (18, 16), (17, 21), (25, 20)]))
    assert not bought
    assert "not the first cross" in funnel.reason


def test_a_cross_in_the_arm_week_itself_counts() -> None:
    # K < 20 and D < 20 at the close, and K > D after K <= D: armed and crossed.
    assert _taken(_entry(kd_tape([(12, 16), (18, 16)])))


# ------------------------------------ v1.2f answer B: most recent trigger
def test_b_only_the_most_recent_earlier_trigger_is_compared() -> None:
    """Allowed: the older trigger closed higher, the most recent lower. Under an
    "any earlier trigger" reading this would be refused (prior high 1010)."""
    tape = _repeat_tape({-17: 1200.0, -9: 900.0, -1: 1000.0}, prior_high=1010.0)
    assert _taken(_entry(tape))


def test_b_the_most_recent_trigger_above_this_close_refuses_without_the_prior_high() -> None:
    """Refused: the most recent trigger closed higher; this close is not above
    the prior week's high."""
    tape = _repeat_tape({-17: 900.0, -9: 1100.0, -1: 1000.0}, prior_high=1010.0)
    funnel, bought = _entry(tape)
    assert not bought
    assert "repeat signal" in funnel.reason


def test_b_a_trigger_older_than_26_bars_is_ignored() -> None:
    tail = _EPISODE + [(30, 35)] * 20 + _FINAL
    close: dict[int, float | None] = {-(len(tail) - 2): 1500.0, -1: 1000.0}
    assert _taken(_entry(kd_tape(tail, close=close, high={-2: 1010.0})))


# ----------------------------------- v1.2f answer J: fresh arm after exit
_REARM = [(10, 15), (12, 16), (18, 20), (25, 22)]  # oversold t-3, t-2; cross at t


def test_j_after_a_profit_an_arm_from_before_the_exit_does_not_count() -> None:
    """Refused: the oversold closes (t-3, t-2) fall on or before the exit fill week (t-2)."""
    profit = ClosedTrade("S", "S-old", week(T - 2), Decimal("5000"))
    funnel, bought = _entry(kd_tape(_REARM), closed=(profit,))
    assert not bought
    assert "no fresh arm" in funnel.reason


def test_j_after_a_profit_a_fresh_arm_and_trigger_is_eligible() -> None:
    """Allowed: the exit filled in week t-4, before both oversold closes."""
    profit = ClosedTrade("S", "S-old", week(T - 4), Decimal("5000"))
    assert _taken(_entry(kd_tape(_REARM), closed=(profit,)))


def test_j_after_a_loss_the_cooling_off_ends_at_x_plus_26_and_needs_ema50() -> None:
    loss_25 = ClosedTrade("S", "S-old", week(T - 25), Decimal("-1"))
    loss_26 = ClosedTrade("S", "S-old", week(T - 26), Decimal("-1"))
    tape = kd_tape(_REARM)
    assert not _entry(tape, closed=(loss_25,))[1]
    assert _taken(_entry(tape, closed=(loss_26,)))
    # After the cooling-off, RS > 0 alone is not enough: close must beat EMA50.
    below_ema50 = kd_tape(_REARM, ema50=1100.0)
    funnel, bought = _entry(below_ema50, closed=(loss_26,))
    assert not bought
    assert "50W EMA" in funnel.reason


def test_j_net_of_costs_a_tiny_gross_profit_can_be_a_loss() -> None:
    """v1.2f: P&L is net of costs, so a net-negative trade starts the cooling-off."""
    net_loss = ClosedTrade("S", "S-old", week(T - 4), Decimal("-0.01"))
    funnel, bought = _entry(kd_tape(_REARM), closed=(net_loss,))
    assert not bought
    assert "cooling-off" in funnel.reason


# ------------------------------------ v1.2f E and F: fail closed
def test_e_regime_unknown_blocks_every_entry() -> None:
    series = kd_tape([(15, 18), (20, 22), (28, 25)]).series()
    index = index_series(ema40={-1: None})
    assert regime(index, week(T), PARAMS) is Regime.UNKNOWN
    decision = decide_week(ctx(), book(), {"S": symbol_week("S", series)}, index, PARAMS)
    assert not decision.orders
    assert decision.entries_blocked is not None and "regime unknown" in decision.entries_blocked
    (entry,) = decision.funnel
    assert entry.stage is FunnelStage.NOT_TAKEN


def test_e_regime_red_and_normal() -> None:
    red = index_series(close=18000.0, ema40={-5: 19500.0, -1: 19000.0})
    assert regime(red, week(T), PARAMS) is Regime.RED
    rising = index_series(close=18000.0, ema40={-5: 18500.0, -1: 19000.0})
    assert regime(rising, week(T), PARAMS) is Regime.NORMAL
    assert regime(index_series(), week(T - 1), PARAMS) is Regime.UNKNOWN  # no bar that week


def test_e_red_regime_allows_one_nifty100_entry() -> None:
    red = index_series(close=18000.0, ema40={-5: 19500.0, -1: 19000.0})
    tape = kd_tape([(15, 18), (20, 22), (28, 25)])
    symbols: dict[str, SymbolWeek] = {
        s: symbol_week(s, tape.series(), industry=f"I-{s}") for s in ("A", "B")
    }
    symbols["C"] = symbol_week("C", tape.series(), row=universe_row("C", "I-C", nifty100=False))
    decision = decide_week(ctx(), book(), symbols, red, PARAMS)
    assert len([o for o in decision.orders if o.action is OrderAction.BUY_T1]) == 1
    stages = {e.symbol: e for e in decision.funnel}
    assert "NIFTY 100 only" in stages["C"].reason
    assert "cap reached" in (
        stages["B"].reason if stages["A"].stage is FunnelStage.TAKEN else stages["A"].reason
    )


def test_f_undefined_k_in_the_arm_window_is_no_trigger_and_flagged() -> None:
    tape = kd_tape([(15, 18), (None, 22), (28, 25)])
    funnel, bought = _entry(tape)
    assert not bought
    assert funnel.stage is FunnelStage.UNDEFINED
    assert "flagged" in funnel.flags


def test_f_a_flat_stoch_range_is_reported_as_such() -> None:
    tape = kd_tape([(15, 18), (None, None), (28, 25)], flat=(-2,))
    funnel, _ = _entry(tape)
    assert "flat stoch range" in funnel.reason


# ------------------------------------- None never satisfies a filter
_TRIGGER: list[tuple[float | None, float | None]] = [(15, 18), (20, 22), (28, 25)]


@pytest.mark.parametrize(
    ("tape", "inputs", "expected"),
    [
        (kd_tape(_TRIGGER, ema50=None), {}, "50W EMA"),  # and RS is 0 here, not > 0
        (kd_tape(_TRIGGER, high_52w={-1: None}), {}, "52-week high"),
        (kd_tape(_TRIGGER, atr_pct={-1: None}), {}, "ATR%"),
        (kd_tape(_TRIGGER), {"traded_value": None}, "liquidity"),
    ],
    ids=["ema50", "high52", "atr", "liquidity"],
)
def test_none_never_satisfies_an_entry_filter(
    tape: Tape, inputs: dict[str, Any], expected: str
) -> None:
    funnel, bought = _entry(tape, **inputs)
    assert not bought
    assert expected in funnel.reason


def test_rs_undefined_is_never_positive() -> None:
    """The index ends a week early, so the stock's RS for week t is undefined:
    with the close below EMA50 there is nothing left to pass filter 2 on."""
    from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import screen

    series = kd_tape(_TRIGGER, ema50=1100.0).series()
    result = screen(symbol_week("S", series), index_series(n=N - 1), book(), ctx(), PARAMS)
    assert isinstance(result, FunnelEntry)
    assert result.rs is None
    assert "RS not > 0" in result.reason


def test_candidates_are_ranked_by_rs_and_capped_at_two_in_normal() -> None:
    """Spec 4.7: highest RS first; Normal takes at most 2; the rest are reported."""
    symbols = {
        # Close 26 weeks earlier sets each stock's 6M performance; the index is flat.
        s: symbol_week(s, kd_tape(_TRIGGER, close={-27: start}).series(), industry=f"I-{s}")
        for s, start in (("LOW", 990.0), ("MID", 950.0), ("TOP", 800.0))
    }
    decision = decide_week(ctx(), book(), symbols, index_series(), PARAMS)
    taken = [o.symbol for o in decision.orders]
    assert taken == ["TOP", "MID"]
    (low,) = [e for e in decision.funnel if e.symbol == "LOW"]
    assert low.stage is FunnelStage.NOT_TAKEN
    assert "weekly entry cap" in low.reason


# ---------------------------------------------------- other 4.6 filters
def test_the_gap_block_refuses_an_entry() -> None:
    funnel, bought = _entry(kd_tape(_TRIGGER), gap_blocked=True)
    assert not bought
    assert "gap" in funnel.reason


def test_history_below_200_weekly_bars_is_refused() -> None:
    funnel, bought = _entry(kd_tape(_TRIGGER, n=199))
    assert not bought
    assert "history 199" in funnel.reason


def test_close_below_60_percent_of_the_52_week_high_is_refused() -> None:
    funnel, bought = _entry(kd_tape(_TRIGGER, high_52w=1700.0))
    assert not bought
    assert "60%" in funnel.reason
    assert _taken(_entry(kd_tape(_TRIGGER, high_52w=1000.0 / 0.6)))


def test_atr_above_12_percent_is_refused() -> None:
    assert not _entry(kd_tape(_TRIGGER, atr_pct=0.1201))[1]
    assert _taken(_entry(kd_tape(_TRIGGER, atr_pct=0.12)))


def test_close_not_above_ema50_passes_on_rs_alone() -> None:
    # RS > 0: the stock rose 10% over 26 weeks while the index was flat.
    tape = kd_tape(_TRIGGER, ema50=1100.0, close={-27: 909.0})
    assert _taken(_entry(tape))
    flat = kd_tape(_TRIGGER, ema50=1100.0)
    assert not _entry(flat)[1]


# ============ v1.2g fix 3: undefined K/D blocks only in the bars read
def test_undefined_kd_before_the_window_does_not_block_when_j0_is_later() -> None:
    """K/D undefined 8 bars back (the bar before the window). j0 is 2 bars back,
    so the no-earlier-cross check reads from 3 bars back: nothing undefined is
    read, and this is a valid trigger."""
    tail: list[tuple[float | None, float | None]] = [(None, None)] + [(50, 50)] * 5
    tail += [(15, 18), (20, 22), (28, 25)]
    assert len(tail) == 9
    assert _taken(_entry(kd_tape(tail)))


def test_undefined_kd_in_the_bar_before_j0_blocks() -> None:
    """j0 is the window's first bar (7 back), so the cross check needs the bar
    before it (8 back), which is undefined: no trigger, flagged."""
    tail: list[tuple[float | None, float | None]] = [(None, None), (15, 18)]
    tail += [(22, 25)] * 6 + [(30, 27)]
    assert len(tail) == 9
    funnel, bought = _entry(kd_tape(tail))
    assert not bought
    assert funnel.stage is FunnelStage.UNDEFINED


def test_undefined_kd_inside_the_window_still_blocks() -> None:
    tail: list[tuple[float | None, float | None]] = [(15, 18), (None, None)]
    tail += [(20, 22)] * 5 + [(28, 25)]
    funnel, bought = _entry(kd_tape(tail))
    assert not bought
    assert funnel.stage is FunnelStage.UNDEFINED
