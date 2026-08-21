"""Phase 3: daily risk, session boundaries, premium/underlying gaps and fail-closed
exit behaviour for ``supertrend_buy_1_1p2`` — through a real ``TradingEngine`` over a
real ``ExecutionRepository`` on a temporary database.

Companion to ``test_supertrend_buy_1_1p2_recovery.py``; the shared harness and the
reason the engine is assembled rather than run through ``run_worker`` are documented
in ``_supertrend_buy_1_1p2_fixtures.py``.

Spec sections 18.4 (premium exit), 18.6 (risk and square-off) and 14 (gap handling).
Every price sequence below was walked through the real engine before being
hard-coded, and the two premium-candle facts that surprise are stated where they
matter: the entry fill is **not** part of the first premium candle (``_open``
retargets the builder at the fill), and a premium gap is detected on a strictly
greater-than-one-interval hole between consecutive option ticks.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _supertrend_buy_1_1p2_fixtures import (
    EXACT_FILL_PAPER_EXECUTION,
    LOT_SIZE,
    LOTS,
    Stack,
    build_stack,
    contract_id,
    dt,
    session_config,
    tick,
    underlying_ticks,
    warmup_candles,
    worker_config,
)

from common.engine.gateway import GatewayExecutionError
from common.engine.models import ExitReason, OptionType
from common.engine.session import MarketSession

#: The PE opened by a lone 19500 bar (its own trailing tick is the 19500 spot).
PE = contract_id(19500.0, "PE")
#: The correlation ids the paper broker can be told to reject. The sequence is
#: per strategy and trading date and starts at 0001 on a fresh database, so the
#: entry is 0001, a reversal's closing SELL is 0002 and a forced square-off retry
#: is 0003 — verified against the ids the repository actually wrote.
ENTRY_CORRELATION = "p_io_supe_20260820_0001"
CLOSE_CORRELATION = "p_io_supe_20260820_0002"
SQUARE_OFF_CORRELATION = "p_io_supe_20260820_0003"


def _open_pe(tmp_path: Path, premium: list, **kwargs) -> Stack:
    """A run that opens the PE at a 100.0 tick and then applies ``premium`` ticks."""
    config = worker_config(tmp_path, **kwargs)
    ticks = underlying_ticks([19500.0], start=dt(9, 15))
    ticks.insert(2, tick(PE, 100.0, dt(9, 21, 10)))
    stack = build_stack(config, ticks + premium)
    stack.engine.run()
    return stack


# ------------------------------------------------------- 18.6 daily loss cap
def test_a_loss_just_short_of_the_cap_does_not_trigger_it(tmp_path: Path):
    """Spec 18.6: "Daily P&L just above -Rs 30,000 | Guard not triggered".

    Entry fills at exactly 100.00 here (see ``EXACT_FILL_PAPER_EXECUTION``); a mark of
    60.05 on 750 units is -Rs 29,962.50, inside the cap.
    """
    stack = _open_pe(
        tmp_path,
        [tick(PE, 60.05, dt(9, 23))],
        paper_execution=dict(EXACT_FILL_PAPER_EXECUTION),
    )

    (position,) = stack.positions.positions
    assert position.entry_price == 100.0
    assert position.quantity == LOTS * LOT_SIZE
    assert position.unrealised_pnl > -30_000.0
    assert stack.engine._daily_guard is not None
    assert stack.engine._daily_guard.halted is False
    assert stack.positions.trades == []


def test_a_loss_of_exactly_thirty_thousand_squares_off_and_blocks_entries(
    tmp_path: Path,
):
    """Spec 18.6: "Daily P&L exactly -Rs 30,000 | Square off and block entries" — the
    trip is inclusive. ``(60.00 - 100.00) * 750`` is exactly -30,000.
    """
    stack = _open_pe(
        tmp_path,
        [tick(PE, 60.0, dt(9, 23))],
        paper_execution=dict(EXACT_FILL_PAPER_EXECUTION),
    )

    assert (60.0 - 100.0) * (LOTS * LOT_SIZE) == -30_000.0
    (trade,) = stack.positions.trades
    assert trade.exit_reason is ExitReason.DAILY_LOSS_LIMIT
    assert stack.positions.positions == []
    assert stack.engine._daily_guard is not None
    assert stack.engine._daily_guard.halted is True
    assert stack.open_position_rows() == []


def test_realised_plus_unrealised_crosses_the_threshold(tmp_path: Path):
    """Spec 18.6: "Realised + unrealised crosses threshold | Square off and block
    entries".

    Neither half breaches on its own: the PE is closed by a reversal for roughly
    -Rs 15,075 realised, and the replacement CE is then marked to a further
    ~-Rs 15,075 unrealised. Only the sum trips the cap.
    """
    config = worker_config(tmp_path)
    # Both bars resolve their strike from the tick that closed them, and the tape's
    # trailing tick repeats the last close — so both legs sit at the 20200 strike.
    pe = contract_id(20200.0, "PE")
    ce = contract_id(20200.0, "CE")
    ticks = underlying_ticks([19500.0, 20200.0], start=dt(9, 15))
    ticks.insert(2, tick(pe, 100.0, dt(9, 21, 10)))
    ticks.insert(3, tick(pe, 80.0, dt(9, 23)))  # -15,075 unrealised: not a breach
    ticks.append(tick(ce, 100.0, dt(9, 26, 10)))  # the reversal's replacement fills
    ticks.append(tick(ce, 80.0, dt(9, 28)))  # ...and then loses the same again
    stack = build_stack(config, ticks)
    stack.engine.run()

    reasons = [t.exit_reason for t in stack.positions.trades]
    assert reasons == [ExitReason.OPPOSITE_SIGNAL, ExitReason.DAILY_LOSS_LIMIT]
    first, second = stack.positions.trades
    assert first.net_pnl > -30_000.0, "the first leg alone must not breach the cap"
    assert first.net_pnl + second.net_pnl <= -30_000.0
    assert stack.engine._daily_guard is not None
    assert stack.engine._daily_guard.halted is True
    assert stack.positions.positions == []


def test_a_halted_guard_refuses_a_later_genuine_flip(tmp_path: Path):
    """The latch is entry-side and lasts the rest of the day: a fresh flip after the
    breach resolves a contract and still opens nothing."""
    config = worker_config(tmp_path, paper_execution=dict(EXACT_FILL_PAPER_EXECUTION))
    ticks = underlying_ticks([19500.0, 20200.0, 20300.0], start=dt(9, 15))
    # The 19500 bar closes on the 09:21 tick, whose own price (20200) is the spot.
    first_pe = contract_id(20200.0, "PE")
    ticks.insert(2, tick(first_pe, 100.0, dt(9, 21, 10)))
    ticks.insert(3, tick(first_pe, 60.0, dt(9, 23)))  # breaches on the very next tick
    # The 20200 bar is a genuine UP flip that would otherwise buy a CE.
    ticks.append(tick(contract_id(20300.0, "CE"), 100.0, dt(9, 31, 10)))
    stack = build_stack(config, ticks)
    stack.engine.run()

    assert stack.strategy._supertrend.state.trend == 1  # the UP flip really happened
    assert stack.order_intent_count("BUY") == 1  # ...and bought nothing
    assert stack.positions.positions == []


# ------------------------------------------------- 18.6 hard square-off
def test_the_hard_square_off_runs_without_indicator_or_premium_data(tmp_path: Path):
    """Spec 18.6: "Missing SuperTrend/premium data at 15:20 | Hard square-off still
    executes".

    The second run is as data-starved as it gets: its warm-up is stale so entries are
    latched off for the day, and not one premium tick for the open contract ever
    arrives. The only tick it sees is an underlying one past 15:20 — and the adopted
    position is still closed.
    """
    first = _open_pe(tmp_path, [tick(PE, 125.0, dt(9, 23)), tick(PE, 130.0, dt(9, 27))])
    assert first.positions.has_position()

    config = worker_config(tmp_path)
    session = MarketSession(session_config())
    stack = build_stack(
        config,
        [tick("INDEX", 19000.0, dt(15, 20, 1))],
        warmup=warmup_candles(session),  # prior sessions only -> PARTIAL
        clock_at=dt(15, 20, 1),
    )
    stack.engine.run()

    assert stack.engine.entries_blocked is not None
    (trade,) = stack.positions.trades
    assert trade.exit_reason is ExitReason.SQUARE_OFF
    assert stack.positions.positions == []
    assert stack.open_position_rows() == []


def test_the_square_off_closes_a_position_the_same_run_opened(tmp_path: Path):
    """The simpler in-run case, with a live indicator: a tick past 15:20 forces the
    close regardless of what the strategy would have wanted."""
    config = worker_config(tmp_path)
    ticks = underlying_ticks([19500.0], start=dt(15, 0))
    ticks.insert(2, tick(contract_id(19500.0, "PE"), 100.0, dt(15, 6, 10)))
    ticks.append(tick("INDEX", 19400.0, dt(15, 20, 1)))
    # The worker started at the open and ran on; only the tape is late, which is why
    # the warm-up clock stays at 09:15 (the default) rather than moving with it.
    stack = build_stack(config, ticks)
    stack.engine.run()

    (trade,) = stack.positions.trades
    assert trade.exit_reason is ExitReason.SQUARE_OFF
    assert stack.order_intent_count("SELL") == 1


# ------------------------------------------- 18.3 fail-closed reversal
def test_a_rejected_close_prevents_the_replacement_entry(tmp_path: Path):
    """Spec 18.3: "Close fails or outcome unknown | No replacement entry".

    The reversal's closing SELL is rejected by the broker. The engine must not open
    the replacement leg — and it does not, because ``PositionManager.close`` raises
    before ``_enter`` is ever reached. The failure is loud: the exception escapes
    ``run()`` after the engine's own safety net has forced a square-off.
    """
    config = worker_config(tmp_path)
    config.paper_execution["reject_correlation_ids"] = [CLOSE_CORRELATION]
    pe = contract_id(20200.0, "PE")
    ce = contract_id(20200.0, "CE")
    ticks = underlying_ticks([19500.0, 20200.0], start=dt(9, 15))
    ticks.insert(2, tick(pe, 120.0, dt(9, 21, 10)))
    ticks.append(tick(ce, 140.0, dt(9, 32)))  # would have filled the replacement
    stack = build_stack(config, ticks)

    with pytest.raises(GatewayExecutionError, match="did not trade"):
        stack.engine.run()

    # Exactly one BUY ever reached the broker: the original PE. The CE replacement
    # was never queued, never filled, never persisted.
    assert stack.order_intent_count("BUY") == 1
    assert not any(
        p.contract.option_type is OptionType.CE for p in stack.positions.positions
    )
    assert not any(t.contract.security_id == ce for t in stack.positions.trades)


def test_an_unresolvable_exit_leaves_the_position_open_and_fails_loudly(
    tmp_path: Path,
):
    """Spec 18.6: "Position remains unresolved after close attempt | Critical/
    unresolved state; no new entry".

    Both the reversal's close and the safety-net square-off that follows it are
    rejected. Nothing is invented: the position stays open in the database, no
    replacement is opened, and the failure propagates rather than being swallowed
    into a false "flat" state.
    """
    config = worker_config(tmp_path)
    config.paper_execution["reject_correlation_ids"] = [
        CLOSE_CORRELATION,
        SQUARE_OFF_CORRELATION,
    ]
    pe = contract_id(20200.0, "PE")
    ticks = underlying_ticks([19500.0, 20200.0], start=dt(9, 15))
    ticks.insert(2, tick(pe, 120.0, dt(9, 21, 10)))
    ticks.append(tick(contract_id(20200.0, "CE"), 140.0, dt(9, 32)))
    stack = build_stack(config, ticks)

    with pytest.raises(GatewayExecutionError):
        stack.engine.run()

    assert stack.positions.trades == []
    (row,) = stack.open_position_rows()
    assert row.security_id == pe
    assert stack.order_intent_count("BUY") == 1


# ------------------------------------------------------ 18.4 premium exit
def test_momentum_exits_on_a_strict_break_of_the_previous_premium_low(
    tmp_path: Path,
):
    """Spec 10.1: ``current completed close < previous completed low`` — strict.

    The premium candles are 105 (low 105) then 98: 98 < 105 fires, and the 6.7%
    retracement from the 105 extreme is inside the 8% trail, so the reason is
    momentum alone.
    """
    stack = _open_pe(
        tmp_path,
        [
            tick(PE, 105.0, dt(9, 23)),
            tick(PE, 98.0, dt(9, 27)),
            tick(PE, 98.0, dt(9, 32)),
            tick(PE, 98.0, dt(9, 37)),
        ],
    )

    (trade,) = stack.positions.trades
    assert trade.exit_reason is ExitReason.MOMENTUM_LOW
    assert trade.candle_structure_triggered is True
    assert trade.trail_triggered is False


def test_a_close_exactly_at_the_previous_premium_low_holds(tmp_path: Path):
    """The other side of the strict inequality: equality is not a break."""
    stack = _open_pe(
        tmp_path,
        [
            tick(PE, 105.0, dt(9, 23)),
            tick(PE, 105.0, dt(9, 27)),  # closes candle 1 at 105, low 105
            tick(PE, 105.0, dt(9, 32)),  # closes candle 2 at exactly 105
            tick(PE, 105.0, dt(9, 37)),
        ],
    )

    assert stack.positions.trades == []
    assert stack.positions.has_position()


def test_both_legs_firing_together_report_one_combined_exit(tmp_path: Path):
    """Spec 18.4: "Both conditions fire | One exit with combined/granular reason"."""
    stack = _open_pe(
        tmp_path,
        [
            tick(PE, 125.0, dt(9, 23)),
            tick(PE, 110.0, dt(9, 27)),  # closes candle 1 at 125, low 125
            tick(PE, 110.0, dt(9, 32)),  # closes candle 2 at 110: < 125 and -12%
            tick(PE, 110.0, dt(9, 37)),
        ],
    )

    (trade,) = stack.positions.trades
    assert trade.exit_reason is ExitReason.MOMENTUM_AND_TRAIL
    assert trade.candle_structure_triggered is True
    assert trade.trail_triggered is True
    assert stack.order_intent_count("SELL") == 1, "one exit, not two"


# ----------------------------------------------------- 14/18.4 premium gaps
def test_a_premium_gap_suppresses_the_first_post_gap_candle_then_resumes(
    tmp_path: Path,
):
    """Spec 18.4: the first completed post-gap premium candle cannot fire momentum;
    the second consecutive one can.

    The option tick stream stops after 09:27 and resumes at 09:40 — a 13-minute hole,
    comfortably over the one-interval threshold. Candle A (close 120) is under the
    pre-gap low of 125 and must not exit; candle B (close 119) is under A's own low
    and does.
    """
    stack = _open_pe(
        tmp_path,
        [
            tick(PE, 125.0, dt(9, 23)),
            tick(PE, 130.0, dt(9, 27)),  # closes the pre-gap candle at 125, low 125
            # --- 13-minute hole in the option stream ---
            tick(PE, 120.0, dt(9, 40)),  # gap detected; candle A opens
            tick(PE, 119.0, dt(9, 45)),  # closes A at 120 -> suppressed, no exit
            tick(PE, 119.0, dt(9, 50)),  # closes B at 119 < A's low of 120 -> exit
            tick(PE, 119.0, dt(9, 55)),
        ],
    )

    (trade,) = stack.positions.trades
    assert trade.exit_reason is ExitReason.MOMENTUM_LOW
    assert trade.exit_time == dt(9, 50), (
        "the exit landed on the first post-gap candle, so its momentum comparison "
        "was not suppressed"
    )


def test_the_trail_survives_a_premium_gap_and_still_fires_from_the_pre_gap_peak(
    tmp_path: Path,
):
    """Spec 18.4: "Premium gap occurs after trail activation | Trail/best-close state
    preserved". A data hole does not undo favourable progress already made, and only
    the momentum leg is suppressed — the trail is measured from the pre-gap peak of
    125 straight through it.
    """
    stack = _open_pe(
        tmp_path,
        [
            tick(PE, 125.0, dt(9, 23)),
            tick(PE, 130.0, dt(9, 27)),  # pre-gap candle closes at 125 -> extreme 125
            # --- 13-minute hole ---
            tick(PE, 115.0, dt(9, 40)),
            tick(PE, 115.0, dt(9, 45)),  # first post-gap candle closes at 115
            tick(PE, 115.0, dt(9, 50)),
        ],
    )

    (trade,) = stack.positions.trades
    assert (125.0 - 115.0) / 125.0 * 100 == 8.0
    assert trade.exit_reason is ExitReason.HIGHEST_CLOSE_TRAIL
    assert trade.candle_structure_triggered is False  # momentum was suppressed
    assert trade.trail_activated is True
    assert trade.best_favourable_close == 125.0


# -------------------------------------------------- 14 underlying candle gap
def test_an_underlying_candle_gap_is_not_traded_and_does_not_reset_the_indicator(
    tmp_path: Path,
):
    """Spec section 14: an underlying gap is handled separately from a premium one.

    A bar stitched across a hole in the *underlying* tick stream is never fed to the
    indicator and produces no signal — even when its close would have been a genuine
    DOWN flip. The SuperTrend is deliberately left alone rather than reset: it is
    session-spanning, and resetting it would throw away far more history than the
    hole cost.
    """
    config = worker_config(tmp_path)
    stack = build_stack(
        config,
        [
            tick("INDEX", 20000.0, dt(9, 16)),
            tick("INDEX", 19000.0, dt(9, 21)),  # this bar is open when the hole starts
            tick("INDEX", 19000.0, dt(9, 46)),  # ...and is stitched across it
        ],
    )
    stack.engine.run()

    assert stack.strategy._candles_seen == 76, "the stitched bar reached the indicator"
    assert stack.strategy._supertrend.state.trend == 1, "the indicator was reset or fed"
    assert stack.order_intent_count("BUY") == 0
    assert stack.positions.positions == []


def test_the_same_closes_delivered_contiguously_do_produce_the_flip(tmp_path: Path):
    """The control for the test above: without the hole the identical closes are fed
    and the flip is taken. Otherwise "no entry" would prove nothing about gaps."""
    config = worker_config(tmp_path)
    stack = build_stack(
        config,
        [
            tick("INDEX", 20000.0, dt(9, 16)),
            tick("INDEX", 19000.0, dt(9, 21)),
            tick("INDEX", 19000.0, dt(9, 26)),
        ],
    )
    stack.engine.run()

    assert stack.strategy._candles_seen == 77
    assert stack.strategy._supertrend.state.trend == -1
    assert stack.strategy._supertrend.state.flipped is True
