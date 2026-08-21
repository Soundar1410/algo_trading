"""Phase 3: warm-up trust, restart, adoption and exit-state recovery for
``supertrend_buy_1_1p2`` — through a real ``TradingEngine`` over a **real
ExecutionRepository on a temporary database**.

No persisted row here is fabricated. The ``positions`` row, the contract record a
restart needs, the order intents, realised P&L and the exit-state snapshot are all
written by production code (``LifecycleGateway`` / ``OrderLifecycle`` /
``ExecutionRepository`` / ``merge_payload``), and read back by the production
recovery readers (``recover_position`` / ``recover_exit_state`` /
``recover_daily_risk``). A "restart" is two sequential engines over one database.

See ``_supertrend_buy_1_1p2_fixtures.py`` for the harness and for why the engine is
assembled there rather than by calling ``run_worker`` (a worker-level run would build
a real Dhan history client; these tests make no network call).

Spec section 18.5 (warm-up, restart and state) and the restart half of 18.6.
"""

from __future__ import annotations

from pathlib import Path

from _supertrend_buy_1_1p2_fixtures import (
    LOT_SIZE,
    LOTS,
    NEXT_TRADING_DAY,
    TRADING_DATE,
    Stack,
    build_stack,
    contract_id,
    dt,
    session_config,
    tick,
    underlying_ticks,
    warmup_candles,
    warmup_series,
    worker_config,
)

from common.engine.models import ExitReason, OptionType
from common.engine.session import MarketSession
from common.models import PositionStatus

#: The PE the first run opens: the 09:15-09:20 bar closes at 19500 and its own
#: trailing tick (also 19500) is the spot the strike resolves from.
OPEN_PE = contract_id(19500.0, "PE")


def _first_run(tmp_path: Path, **config_kwargs) -> Stack:
    """One run that opens a PE, drives its premium to a 125 completed close (arming
    the trail well past 4%), and stops with the position still open.

    Its one completed premium candle closes at 125 **with a low of 125** — the entry
    fill itself is not part of it, because ``TradingEngine._open`` retargets the
    premium builder at the fill, so the bucket contains only the ticks after it. That
    high low is what makes the re-priming test below a real test: a post-restart
    candle closing under 125 would fire the momentum leg if the comparison were
    allowed to span the process gap.
    """
    config = worker_config(tmp_path, **config_kwargs)
    ticks = underlying_ticks([19500.0], start=dt(9, 15))
    ticks.insert(2, tick(OPEN_PE, 100.0, dt(9, 21, 10)))
    ticks += [
        tick(OPEN_PE, 125.0, dt(9, 23)),  # the 09:20-09:25 premium bucket's only tick
        tick(OPEN_PE, 130.0, dt(9, 27)),  # closes it at 125 (low 125); opens 09:25-09:30
    ]
    stack = build_stack(config, ticks)
    stack.engine.run()
    return stack


def _restart(
    tmp_path: Path,
    ticks,
    *,
    today_closes=(19500.0, 20500.0, 20600.0, 20700.0, 20800.0),
    now=None,
    **config_kwargs,
) -> Stack:
    """A second engine over the same database, warmed the way a genuine mid-session
    restart is: the previous session(s) **plus** the part of today the dead process
    already saw."""
    config = worker_config(tmp_path, **config_kwargs)
    clock = now or dt(9, 41)
    session = MarketSession(session_config())
    stack = build_stack(
        config,
        ticks,
        warmup=warmup_series(session, today_closes=list(today_closes), now=clock),
        clock_at=clock,
    )
    stack.engine.run()
    return stack


# --------------------------------------------------- the persisted baseline
def test_the_first_run_persists_a_real_open_position_and_its_exit_state(tmp_path: Path):
    """Everything the restart tests below depend on is written by production code."""
    stack = _first_run(tmp_path)

    (position,) = stack.positions.positions
    assert position.contract.security_id == OPEN_PE
    assert position.quantity == LOTS * LOT_SIZE

    (row,) = stack.open_position_rows()
    assert row.security_id == OPEN_PE
    assert row.status is PositionStatus.OPEN
    assert row.quantity == LOTS * LOT_SIZE
    assert stack.order_intent_count("BUY") == 1
    assert stack.order_intent_count("SELL") == 0

    payload = stack.payload()
    assert set(payload) == {"open_position", "exit_state"}
    snapshot = stack.exit_state()
    assert snapshot is not None
    assert snapshot["security_id"] == OPEN_PE
    assert snapshot["state"]["trail"] == {"extreme": 125.0, "activated": True}
    assert snapshot["state"]["highest_close"] == 125.0


# ----------------------------------------------- 18.5 adoption without re-entry
def test_a_restarted_engine_adopts_the_position_without_re_entering(tmp_path: Path):
    """Spec 18.5: "Restart with open position | Position adopted, no duplicate entry".

    The second run genuinely signals — its replay ends in an uptrend and its first
    live candle is a real DOWN flip — so this proves the engine refused a duplicate,
    not that nothing happened. A run that never signalled would prove nothing about
    the case that doubles exposure.
    """
    _first_run(tmp_path)

    ticks = underlying_ticks([19000.0], start=dt(9, 40))
    stack = _restart(tmp_path, ticks)

    assert stack.positions.has_position(), "the open position was not adopted"
    (position,) = stack.positions.positions
    assert position.contract.security_id == OPEN_PE, "the adopted leg was replaced"
    assert position.entry_price == 100.1, "the adopted entry price was not preserved"

    # The live candle really did produce a fresh PE signal...
    assert stack.strategy._supertrend.state.flipped is True
    assert stack.strategy._supertrend.state.trend == -1
    # ...and it opened nothing: still one BUY intent, still one positions row.
    assert stack.order_intent_count("BUY") == 1
    assert stack.order_intent_count("SELL") == 0
    assert len(stack.open_position_rows()) == 1


def test_the_adopted_leg_is_kept_even_though_the_new_signal_resolves_a_new_strike(
    tmp_path: Path,
):
    """The dedupe is on option type and side, not strike — so a same-side flip at a
    very different spot still opens nothing. Worth pinning: the new signal here would
    have resolved a 19000 strike, not the adopted 19500 one."""
    _first_run(tmp_path)
    stack = _restart(tmp_path, underlying_ticks([19000.0], start=dt(9, 40)))

    (position,) = stack.positions.positions
    assert position.contract.strike == 19500.0
    assert contract_id(19000.0, "PE") != OPEN_PE


# ------------------------------------------- 18.5 exit-state restoration
def test_a_restart_restores_the_highest_premium_close_and_trail_activation(
    tmp_path: Path,
):
    """Spec 18.5: "Restart after trail activation | Activation and best close
    restored"."""
    _first_run(tmp_path)
    stack = _restart(tmp_path, underlying_ticks([20900.0], start=dt(9, 40)))

    assert stack.strategy._exit.extreme_close == 125.0
    assert stack.strategy._exit.trail_activated is True
    assert stack.strategy._exit.highest_close == 125.0


def test_the_restored_trail_fires_at_exactly_eight_percent_from_the_restored_extreme(
    tmp_path: Path,
):
    """The restoration is load-bearing, not decorative: the exit that follows is
    measured from the *previous process's* peak. 125 -> 115 is exactly -8.00%
    (chosen because it is exact in IEEE-754, unlike a prettier 104 -> 95.68)."""
    _first_run(tmp_path)

    ticks = underlying_ticks([20900.0], start=dt(9, 40))
    ticks += [
        # First post-restart premium candle: closes 120 with a deep wick to 100, so
        # the *next* candle's momentum comparison cannot fire and the trail is
        # isolated. Its own retracement is 4%, inside the 8% threshold.
        tick(OPEN_PE, 120.0, dt(9, 47)),
        tick(OPEN_PE, 100.0, dt(9, 48)),
        tick(OPEN_PE, 120.0, dt(9, 49)),
        tick(OPEN_PE, 115.0, dt(9, 52)),  # closes it; opens the next bucket at 115
        tick(OPEN_PE, 115.0, dt(9, 57)),  # closes that one at 115 -> exactly -8%
        tick(OPEN_PE, 115.0, dt(10, 2)),
    ]
    stack = _restart(tmp_path, ticks)

    assert (125.0 - 115.0) / 125.0 * 100 == 8.0
    (trade,) = stack.positions.trades
    assert trade.exit_reason is ExitReason.HIGHEST_CLOSE_TRAIL
    assert trade.candle_structure_triggered is False
    assert stack.positions.positions == []
    assert stack.order_intent_count("SELL") == 1


def test_the_momentum_comparison_is_re_primed_after_a_restart(tmp_path: Path):
    """Spec 18.5: "First premium candle after restart | Previous-candle momentum
    comparison re-primed".

    The previous process's last completed premium candle had a low of 125. The first
    post-restart candle closes at 120 — under that low — and must **not** exit: that
    comparison would span the process gap. Its own trail retracement (125 -> 120, 4%)
    is inside the threshold, so nothing else can mask the result.

    The *second* post-restart candle then closes at 119, under the first one's low of
    120, and does exit on momentum — proving the comparison was re-primed rather than
    disabled. Its retracement (125 -> 119, 4.8%) is still inside 8%, so the reason is
    momentum alone.
    """
    _first_run(tmp_path)

    ticks = underlying_ticks([20900.0], start=dt(9, 40))
    ticks += [
        tick(OPEN_PE, 120.0, dt(9, 47)),  # candle A: close 120, low 120
        tick(OPEN_PE, 119.0, dt(9, 52)),  # closes A; opens candle B at 119
        tick(OPEN_PE, 119.0, dt(9, 57)),  # closes B: 119 < A's low of 120 -> momentum
        tick(OPEN_PE, 119.0, dt(10, 2)),
    ]
    stack = _restart(tmp_path, ticks)

    (trade,) = stack.positions.trades
    assert trade.exit_reason is ExitReason.MOMENTUM_LOW
    assert trade.trail_triggered is False
    assert trade.exit_time == dt(9, 57), (
        "the exit landed on the first post-restart candle, so the momentum comparison "
        "spanned the process gap instead of being re-primed"
    )


def test_a_restart_never_duplicates_an_already_confirmed_exit(tmp_path: Path):
    """Spec section 11: "Never duplicate an already-confirmed exit or reversal."

    The first run exits on momentum and stops flat; the second run must find nothing
    to adopt and must not re-close anything.
    """
    config = worker_config(tmp_path)
    ticks = underlying_ticks([19500.0], start=dt(9, 15))
    ticks.insert(2, tick(OPEN_PE, 100.0, dt(9, 21, 10)))
    ticks += [
        tick(OPEN_PE, 105.0, dt(9, 23)),
        tick(OPEN_PE, 98.0, dt(9, 27)),  # closes 09:20-09:25 at 105 (low 100)
        tick(OPEN_PE, 98.0, dt(9, 32)),  # closes 09:25-09:30 at 98 < 100 -> momentum
        tick(OPEN_PE, 98.0, dt(9, 37)),  # (105 -> 98 is 6.7%, inside the 8% trail)
    ]
    first = build_stack(config, ticks)
    first.engine.run()
    assert [t.exit_reason for t in first.positions.trades] == [ExitReason.MOMENTUM_LOW]
    assert first.open_position_rows() == []
    assert first.exit_state() is None, "the exit-state key was not cleared on close"

    second = _restart(tmp_path, underlying_ticks([20900.0], start=dt(9, 40)))

    assert second.positions.positions == []
    assert second.positions.trades == []
    assert second.order_intent_count("SELL") == 1, "the confirmed exit was repeated"


# ---------------------------------------------------- 18.5 warm-up trust
def test_a_mid_session_restart_whose_replay_stops_short_blocks_entries(tmp_path: Path):
    """Spec 18.5: "Stale/partial/gapped warm-up | Entries blocked".

    The same restart, warmed only from the previous sessions — complete history that
    simply stops before today. That is stale, not merely short, and a latched
    indicator replayed to a stale point looks warm while sitting mid-path.
    """
    _first_run(tmp_path)

    config = worker_config(tmp_path)
    session = MarketSession(session_config())
    stack = build_stack(
        config,
        underlying_ticks([19000.0], start=dt(9, 40)),
        warmup=warmup_candles(session),  # prior sessions only, nothing from today
        clock_at=dt(9, 41),
    )
    stack.engine.run()

    assert stack.engine.entries_blocked is not None
    assert "PARTIAL" in stack.engine.entries_blocked
    assert stack.order_intent_count("BUY") == 1  # still just the first run's entry


def test_entries_are_blocked_but_the_adopted_position_is_still_managed(tmp_path: Path):
    """Spec section 7: "Exit management and hard square-off must remain available even
    when entry context is untrusted." An entry block must never trap a position."""
    _first_run(tmp_path)

    config = worker_config(tmp_path)
    session = MarketSession(session_config())
    ticks = underlying_ticks([19000.0], start=dt(9, 40))
    ticks += [
        tick(OPEN_PE, 120.0, dt(9, 47)),
        tick(OPEN_PE, 115.0, dt(9, 52)),
        tick(OPEN_PE, 115.0, dt(9, 57)),
        tick(OPEN_PE, 115.0, dt(10, 2)),
    ]
    stack = build_stack(
        config, ticks, warmup=warmup_candles(session), clock_at=dt(9, 41)
    )
    stack.engine.run()

    assert stack.engine.entries_blocked is not None
    (trade,) = stack.positions.trades
    assert trade.trail_triggered is True
    assert trade.exit_reason in {
        ExitReason.HIGHEST_CLOSE_TRAIL,
        ExitReason.MOMENTUM_AND_TRAIL,
    }
    assert stack.positions.positions == []


def test_a_trusted_mid_session_restart_permits_a_later_entry(tmp_path: Path):
    """The positive control for the two tests above: with today's own buckets in the
    replay the restart is WARMED, entries are not blocked, and a flat book really does
    take the next genuine flip."""
    config = worker_config(tmp_path)
    session = MarketSession(session_config())
    ticks = underlying_ticks([19000.0], start=dt(9, 40))
    ticks.insert(2, tick(contract_id(19000.0, "PE"), 90.0, dt(9, 46, 10)))
    stack = build_stack(
        config,
        ticks,
        warmup=warmup_series(
            session, today_closes=[19500.0, 20500.0, 20600.0, 20700.0, 20800.0], now=dt(9, 41)
        ),
        clock_at=dt(9, 41),
    )
    stack.engine.run()

    assert stack.engine.entries_blocked is None
    (position,) = stack.positions.positions
    assert position.contract.option_type is OptionType.PE
    assert position.quantity == LOTS * LOT_SIZE
    assert stack.order_intent_count("BUY") == 1


def test_the_warmup_replay_of_a_restart_still_places_no_order(tmp_path: Path):
    """Spec 18.5: "Warm-up replay contains historical flips | No historical order".

    The restart's replay contains two genuine flips of its own (today's 19500 bar
    down, then the 20500 bar back up). Neither reaches the order path.
    """
    config = worker_config(tmp_path)
    session = MarketSession(session_config())
    stack = build_stack(
        config,
        [tick("INDEX", 20800.0, dt(9, 42))],  # one tick, no bar ever closes
        warmup=warmup_series(
            session, today_closes=[19500.0, 20500.0, 20600.0, 20700.0, 20800.0], now=dt(9, 41)
        ),
        clock_at=dt(9, 41),
    )
    stack.engine.run()

    assert stack.strategy._candles_seen == 75
    assert stack.order_intent_count("BUY") == 0
    assert stack.positions.positions == []
    assert stack.positions.trades == []
    assert stack.engine.entries_blocked is None


# ------------------------------------------------ 18.5/18.6 daily-loss latch
def _run_into_the_daily_cap(tmp_path: Path) -> Stack:
    """One run whose open PE loses more than Rs 30,000 on live MTM.

    Entry 100.10 on 750 units; a mark at 60.0 is (60 - 100.10) * 750 = -30,075,
    which is past the inclusive -30,000 cap.
    """
    config = worker_config(tmp_path)
    ticks = underlying_ticks([19500.0], start=dt(9, 15))
    ticks.insert(2, tick(OPEN_PE, 100.0, dt(9, 21, 10)))
    ticks.append(tick(OPEN_PE, 60.0, dt(9, 23)))
    stack = build_stack(config, ticks)
    stack.engine.run()
    return stack


def test_the_daily_cap_squares_off_and_latches_within_the_run(tmp_path: Path):
    stack = _run_into_the_daily_cap(tmp_path)

    (trade,) = stack.positions.trades
    assert trade.exit_reason is ExitReason.DAILY_LOSS_LIMIT
    assert stack.positions.positions == []
    assert stack.engine._daily_guard is not None
    assert stack.engine._daily_guard.halted is True


def test_a_same_day_restart_preserves_the_daily_loss_block(tmp_path: Path):
    """Spec 18.5: "Same-day restart after daily loss breach | Entry remains blocked".

    The realised loss is read back through the production ``recover_daily_risk``,
    off the ``strategy_state`` row the first run's own closing fill wrote — not a
    value handed to the second engine by the test.
    """
    first = _run_into_the_daily_cap(tmp_path)
    realised = first.positions.trades[0].net_pnl
    assert realised <= -30_000.0

    second = _restart(tmp_path, underlying_ticks([19000.0], start=dt(9, 40)))

    assert second.engine._daily_guard is not None
    assert second.engine._daily_guard.halted is True
    # The second run genuinely signalled a fresh DOWN flip and was still refused.
    assert second.strategy._supertrend.state.flipped is True
    assert second.positions.positions == []
    assert second.order_intent_count("BUY") == 1  # only the first run's entry


def test_the_next_trading_day_resets_the_block_through_the_normal_lifecycle(
    tmp_path: Path,
):
    """Spec 18.5: "New trading day | Daily guard resets through normal lifecycle".

    Nothing is cleared by hand: ``strategy_state`` is keyed by trading date, so a new
    date simply finds no row and the guard starts at its fresh zero.
    """
    _run_into_the_daily_cap(tmp_path)

    config = worker_config(tmp_path, trading_date=NEXT_TRADING_DAY.isoformat())
    session = MarketSession(session_config())
    ticks = underlying_ticks([19000.0], start=dt(9, 15, day=NEXT_TRADING_DAY))
    ticks.insert(2, tick(contract_id(19000.0, "PE"), 90.0, dt(9, 21, 10, day=NEXT_TRADING_DAY)))
    stack = build_stack(
        config,
        ticks,
        warmup=warmup_candles(
            session, days=(NEXT_TRADING_DAY.replace(day=19), NEXT_TRADING_DAY.replace(day=20))
        ),
        clock_at=dt(9, 15, day=NEXT_TRADING_DAY),
    )
    stack.engine.run()

    assert stack.engine._daily_guard is not None
    assert stack.engine._daily_guard.halted is False
    assert stack.engine.entries_blocked is None
    (position,) = stack.positions.positions
    assert position.contract.option_type is OptionType.PE


def test_the_previous_days_position_row_does_not_leak_into_the_new_day(tmp_path: Path):
    """The other half of the day boundary: yesterday's closed position must not be
    adopted, and yesterday's realised P&L must not seed today's cap."""
    _run_into_the_daily_cap(tmp_path)

    config = worker_config(tmp_path, trading_date=NEXT_TRADING_DAY.isoformat())
    session = MarketSession(session_config())
    stack = build_stack(
        config,
        [tick("INDEX", 20000.0, dt(9, 16, day=NEXT_TRADING_DAY))],
        warmup=warmup_candles(
            session, days=(NEXT_TRADING_DAY.replace(day=19), NEXT_TRADING_DAY.replace(day=20))
        ),
        clock_at=dt(9, 15, day=NEXT_TRADING_DAY),
    )
    stack.engine.run()

    assert stack.positions.positions == []
    assert stack.repository.open_positions(
        strategy_id=config.strategy_id,
        execution_mode=config.execution_mode,
        trading_date=TRADING_DATE,
    ) == []
