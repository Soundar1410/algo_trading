"""Behaviour-level proofs for ``EmaCross9x21BuyStrategy`` (Phase 9).

Full spec: strategies/intraday_options/ema_cross_9_21_buy/ema_cross_9_21_buy_spec.md.

These exercise the strategy class directly (no ``TradingEngine``) so the
fresh-crossover invariant, the premium-exit wiring and per-trade state reset
can be pinned with exact, hand-verified numbers. Timing, reversal, sizing,
the daily cap and restart recovery — everything the *engine* owns rather than
the strategy — are proven end to end in
``tests/integration/test_ema_cross_9_21_buy_engine.py`` instead.

Every EMA-dependent test below uses ``ema_fast=2, ema_slow=3`` (an explicit
constructor override) rather than the production 9/21 defaults, purely so a
crossover can be reached in a handful of candles; a dedicated test pins that
the *defaults* really are 9 and 21. Every numeric candle sequence here was
verified against the real ``EMA``/``ConfirmedCrossover``/``CombinedCandleExit``
classes before being hard-coded (not hand-derived), and premium candles
always carry a wick (``low = close - 5``) precisely so a momentum-break
comparison (``close < previous candle's low``) and a best-close retracement
can be independently controlled in the same walk.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.engine.models import (
    ExitReason,
    Moneyness,
    OpenPosition,
    OptionContract,
    OptionType,
    OrderSide,
    SignalAction,
    Trade,
)
from common.engine.strategy import available_strategies, get_strategy
from common.indicators.base import OHLC
from common.indicators.ema import EMA
from strategies.intraday_options.ema_cross_9_21_buy.strategy import EmaCross9x21BuyStrategy

IST = ZoneInfo("Asia/Kolkata")


def _ts(i: int) -> datetime:
    return datetime(2026, 8, 7, 9, 20, tzinfo=IST) + timedelta(minutes=5 * i)


def _candle(close: float) -> OHLC:
    """A flat underlying candle (high == low == close) — fine for EMA/crossover
    tests, which only ever read ``.close``."""
    return OHLC(high=close, low=close, close=close)


def _premium(close: float, *, low_pad: float = 5.0, high_pad: float = 2.0) -> OHLC:
    """A premium candle with a wick, so momentum (close < previous LOW) and the
    best-close trail (close vs. the running extreme CLOSE) can be driven
    independently — see the module docstring."""
    return OHLC(high=close + high_pad, low=close - low_pad, close=close)


def _strategy(**kwargs) -> EmaCross9x21BuyStrategy:
    kwargs.setdefault("ema_fast", 2)
    kwargs.setdefault("ema_slow", 3)
    kwargs.setdefault("lots_per_trade", 1)
    return EmaCross9x21BuyStrategy(**kwargs)


def _open_position(entry: float = 100.0, security_id: str = "CE1") -> OpenPosition:
    contract = OptionContract(
        symbol=security_id,
        security_id=security_id,
        strike=100,
        option_type=OptionType.CE,
        expiry="WEEKLY",
        lot_size=75,
    )
    return OpenPosition(
        contract=contract, side=OrderSide.BUY, lots=1, entry_price=entry, entry_time=_ts(0)
    )


def _close_trade(strategy: EmaCross9x21BuyStrategy, position: OpenPosition, exit_price: float,
                  exit_reason: ExitReason = ExitReason.STRATEGY_EXIT) -> Trade:
    trade = Trade(
        contract=position.contract,
        side=position.side,
        lots=position.lots,
        quantity=position.quantity,
        entry_price=position.entry_price,
        exit_price=exit_price,
        entry_time=position.entry_time,
        exit_time=_ts(50),
        exit_reason=exit_reason,
        gross_pnl=(exit_price - position.entry_price) * position.quantity,
        charges=0.0,
    )
    strategy.on_position_closed(trade)
    return trade


# ------------------------------------------------------------- 1. registration
def test_strategy_registers_as_ema_cross_9_21_buy():
    assert "ema_cross_9_21_buy" in available_strategies()
    assert EmaCross9x21BuyStrategy.name == "ema_cross_9_21_buy"


def test_get_strategy_by_name_builds_from_cfg_parameters():
    class _Cfg:
        def __init__(self) -> None:
            self.parameters = {"lots_per_trade": 5}

    strategy = get_strategy("ema_cross_9_21_buy", _Cfg())
    assert isinstance(strategy, EmaCross9x21BuyStrategy)
    assert strategy.quantity_lots == 5
    # Defaults still apply for everything not in cfg.parameters.
    assert strategy._ema_fast.period == 9
    assert strategy._ema_slow.period == 21


# ------------------------------------------------------------------ 2. EMA periods
def test_default_periods_are_9_and_21():
    strategy = EmaCross9x21BuyStrategy()
    assert strategy._ema_fast.period == 9
    assert strategy._ema_slow.period == 21


def test_ema_slow_must_be_greater_than_ema_fast():
    with pytest.raises(ValueError, match="ema_slow"):
        EmaCross9x21BuyStrategy(ema_fast=21, ema_slow=9)
    with pytest.raises(ValueError, match="ema_slow"):
        EmaCross9x21BuyStrategy(ema_fast=9, ema_slow=9)


# --------------------------------------------------------- 3. warm-up readiness
def test_no_entry_before_both_emas_are_ready():
    """Cold start: no signal at all until the slow EMA has seen `ema_slow` bars."""
    strategy = _strategy()
    strategy.reset()
    # ema_slow=3: the first two candles cannot possibly produce a signal.
    for c in (100.0, 100.0):
        assert strategy.on_candle(_candle(c), _ts(0)) is None
        assert not strategy._ema_slow.is_ready


# ------------------------------------------------ 4/5. fresh-crossover invariant
#
# Rev 3 (spec section 4.3): the most recently completed relationship survives
# on_warmup_complete(context_trusted=True) as crossover context -- it is no
# longer wiped a second time. An entry requires a genuine current-day closed
# candle to flip *against* that context; a candle that merely continues it
# produces no entry. These four tests are acceptance-matrix rows 1-4.
def test_context_bullish_continuation_does_not_produce_ce():
    """Row 3: context bullish, next closed candle still bullish -> NO ENTRY."""
    strategy = _strategy()
    strategy.reset()
    for i, c in enumerate((100.0, 100.0, 100.0, 110.0, 120.0, 130.0)):
        strategy.on_candle(_candle(c), _ts(i))
    assert strategy._crossover.confirmed_label == "bullish"
    fast_before = strategy._ema_fast.state.value
    slow_before = strategy._ema_slow.state.value

    strategy.on_warmup_complete(context_trusted=True)

    # EMAs untouched, and -- Rev 3 -- the context itself is now PRESERVED,
    # not reset a second time.
    assert strategy._ema_fast.state.value == fast_before
    assert strategy._ema_slow.state.value == slow_before
    assert strategy._crossover.confirmed_side == 1

    # Today opens still bullish (135 continues the trend): NO entry.
    assert strategy.on_candle(_candle(135.0), _ts(100)) is None
    assert strategy.on_candle(_candle(140.0), _ts(101)) is None
    assert strategy.on_candle(_candle(145.0), _ts(102)) is None


def test_context_bearish_continuation_does_not_produce_pe():
    """Row 4: context bearish, next closed candle still bearish -> NO ENTRY."""
    strategy = _strategy()
    strategy.reset()
    for i, c in enumerate((100.0, 100.0, 100.0, 90.0, 80.0, 70.0)):
        strategy.on_candle(_candle(c), _ts(i))  # warm-up: signals discarded by the engine
    assert strategy._crossover.confirmed_label == "bearish"

    strategy.on_warmup_complete(context_trusted=True)
    assert strategy._crossover.confirmed_side == -1  # preserved, not reset

    # Today opens still bearish (65 continues the trend): NO entry.
    assert strategy.on_candle(_candle(65.0), _ts(100)) is None
    assert strategy.on_candle(_candle(60.0), _ts(101)) is None
    assert strategy.on_candle(_candle(55.0), _ts(102)) is None


def test_context_bearish_first_live_candle_bullish_produces_ce_buy():
    """Row 1: context bearish, the very FIRST live candle after
    on_warmup_complete(context_trusted=True) flips bullish -> CE BUY
    immediately -- no continuation candles needed first. This is the Rev 3
    headline behaviour (previously impossible: the old unconditional second
    reset made the first post-warmup candle context-only no matter what)."""
    strategy = _strategy()
    strategy.reset()
    for i, c in enumerate((100.0, 100.0, 100.0, 90.0, 80.0, 70.0)):
        strategy.on_candle(_candle(c), _ts(i))
    assert strategy._crossover.confirmed_label == "bearish"
    strategy.on_warmup_complete(context_trusted=True)

    signal = strategy.on_candle(_candle(90.0), _ts(100))  # genuine flip, first live candle
    assert signal is not None
    assert signal.action is SignalAction.ENTER
    assert signal.option_type is OptionType.CE
    assert signal.side is OrderSide.BUY


def test_context_bullish_first_live_candle_bearish_produces_pe_buy():
    """Row 2: mirror of the above -- context bullish, first live candle
    flips bearish -> PE BUY immediately."""
    strategy = _strategy()
    strategy.reset()
    for i, c in enumerate((100.0, 100.0, 100.0, 110.0, 120.0, 130.0)):
        strategy.on_candle(_candle(c), _ts(i))
    assert strategy._crossover.confirmed_label == "bullish"
    strategy.on_warmup_complete(context_trusted=True)

    signal = strategy.on_candle(_candle(110.0), _ts(100))  # genuine flip, first live candle
    assert signal is not None
    assert signal.action is SignalAction.ENTER
    assert signal.option_type is OptionType.PE
    assert signal.side is OrderSide.BUY


# --------------------------------------------- 4b/5b. mid-session restart
#
# Acceptance-matrix rows 5/6 (spec section 4.3, mid-session restart --
# scenario B, the genuine bug fix). Mechanically identical to rows 1/2 above
# (ConfirmedCrossover has no notion of calendar days -- context is context,
# whatever its source), documented separately because it was previously
# broken for a different reason: before this fix, EVERY post-warmup first
# candle was context-only, whether the context came from yesterday's close
# or from today's own restart replay.
def test_mid_session_restart_warmed_bearish_context_first_live_candle_bullish_produces_ce_buy():
    """Row 5: today's own warmed context (established purely from today's
    candles replayed through ~10:55, not yesterday's close) is bearish; the
    first live candle after the restart (~11:00-11:05) flips bullish -> CE
    BUY on that very first live candle."""
    strategy = _strategy()
    strategy.reset()
    for i, c in enumerate((100.0, 100.0, 100.0, 90.0, 80.0, 70.0)):
        strategy.on_candle(_candle(c), _ts(i))  # today's own candles, replayed through 10:55
    strategy.on_warmup_complete(context_trusted=True)

    signal = strategy.on_candle(_candle(90.0), _ts(100))  # 11:00-11:05, genuine flip
    assert signal is not None
    assert signal.option_type is OptionType.CE
    assert signal.side is OrderSide.BUY


def test_mid_session_restart_warmed_bullish_context_first_live_candle_bearish_produces_pe_buy():
    """Row 6: mirror of the above."""
    strategy = _strategy()
    strategy.reset()
    for i, c in enumerate((100.0, 100.0, 100.0, 110.0, 120.0, 130.0)):
        strategy.on_candle(_candle(c), _ts(i))
    strategy.on_warmup_complete(context_trusted=True)

    signal = strategy.on_candle(_candle(110.0), _ts(100))
    assert signal is not None
    assert signal.option_type is OptionType.PE
    assert signal.side is OrderSide.BUY


# ------------------------------------------------------- 6/7. same-day fresh cross
#
# Spec section 4.4, cold-start / unavailable-context rule: when no legitimate
# context exists, context must never be fabricated -- both rows required.
def test_cold_start_no_context_first_ready_candle_establishes_context_only():
    """Row 1: no usable warm-up/context at all (on_warmup_complete's
    context_trusted=False, harmless no-op here since the detector is already
    fresh) -- the first candle that makes ema_slow ready establishes context
    ONLY, regardless of which side that first relationship lands on (here,
    bearish)."""
    strategy = _strategy()
    strategy.reset()
    strategy.on_warmup_complete(context_trusted=False)  # nothing was replayed; nothing to trust
    assert strategy.on_candle(_candle(100.0), _ts(0)) is None
    assert strategy.on_candle(_candle(100.0), _ts(1)) is None
    # ema_slow (period 3) becomes ready here; spread is already non-flat
    # (bearish), yet this still produces NO entry -- context only.
    assert strategy.on_candle(_candle(90.0), _ts(2)) is None
    assert strategy._crossover.confirmed_label == "bearish"


def test_a_genuinely_fresh_intraday_cross_with_no_warmup_produces_a_signal():
    """Row 2: cold start (no warm-up at all): the first ready candle only
    establishes context (regardless of its sign); the first real flip after
    that signals normally."""
    strategy = _strategy()
    strategy.reset()
    strategy.on_warmup_complete(context_trusted=False)
    seq = [100.0, 100.0, 100.0, 90.0, 80.0, 70.0, 80.0, 90.0, 100.0]
    signals = [strategy.on_candle(_candle(c), _ts(i)) for i, c in enumerate(seq)]
    fired = [(c, s.option_type) for c, s in zip(seq, signals, strict=True) if s is not None]
    assert fired == [(90.0, OptionType.PE), (90.0, OptionType.CE)]


def test_same_direction_continuation_does_not_repeatedly_signal():
    strategy = _strategy()
    strategy.reset()
    seq = [100.0, 100.0, 100.0, 90.0, 80.0, 70.0, 80.0]  # stays bearish after the flip
    signals = [strategy.on_candle(_candle(c), _ts(i)) for i, c in enumerate(seq)]
    assert sum(1 for s in signals if s is not None) == 1  # only the flip at 90


# --------------------------------------------------- 7b. lifecycle (spec section 11)
def test_day_start_reset_clears_stale_crossover_state_but_not_ema():
    """The day-start reset() itself (stale in-memory state from a previous
    process/day) is unchanged by this fix and must keep working: it clears
    the crossover detector but never the EMAs (SESSION_SPANNING)."""
    strategy = _strategy()
    strategy.reset()
    for i, c in enumerate((100.0, 100.0, 100.0, 90.0, 80.0, 70.0)):
        strategy.on_candle(_candle(c), _ts(i))
    assert strategy._crossover.confirmed_side is not None
    fast_before = strategy._ema_fast.state.value
    slow_before = strategy._ema_slow.state.value

    strategy.reset()  # simulates a fresh process's day-start reset

    assert strategy._crossover.confirmed_side is None
    assert strategy._crossover.candidate_side is None
    assert strategy._ema_fast.state.value == fast_before
    assert strategy._ema_slow.state.value == slow_before


def test_multi_day_continuous_run_never_double_feeds_the_ema():
    """Spec section 11: on a continuously running process the EMAs are never
    reset at a day boundary, and warm-up should only ever feed a
    continuously-warm EMA the candles it has not already seen. Simulate two
    day boundaries on one strategy instance and confirm the EMA's final value
    matches an independently-computed EMA fed the exact same combined
    sequence exactly once -- proving no candle was ever double-applied."""
    strategy = _strategy()

    # Day 1: day-start reset, then candles flow through on_candle (whether
    # warm-up or live, both call on_candle identically).
    strategy.reset()
    day1_closes = [100.0, 100.0, 100.0, 90.0, 80.0, 70.0]
    for i, c in enumerate(day1_closes):
        strategy.on_candle(_candle(c), _ts(i))
    strategy.on_warmup_complete(context_trusted=False)  # cold start day 1; irrelevant here
    count_after_day1 = strategy._ema_slow._count
    assert count_after_day1 == len(day1_closes)

    # Day 2: day-start reset clears the crossover but leaves the EMA alone.
    strategy.reset()
    assert strategy._ema_slow._count == count_after_day1
    assert strategy._ema_fast.state.value is not None  # still warm, not reset to None

    # A correctly-scoped day-2 warm-up/live feed supplies ONLY day 2's own
    # new candles -- never day 1's again.
    day2_new_closes = [65.0, 60.0]
    for i, c in enumerate(day2_new_closes):
        strategy.on_candle(_candle(c), _ts(100 + i))

    assert strategy._ema_slow._count == count_after_day1 + len(day2_new_closes)

    expected = EMA(strategy._ema_slow.period)
    for c in day1_closes + day2_new_closes:
        expected.update(_candle(c))
    assert strategy._ema_slow.state.value == expected.state.value


# ------------------------------------------------------------ 8. closed candles
def test_only_on_candle_calls_advance_state_no_hidden_tick_evaluation():
    """The strategy has exactly one entry point for underlying data
    (on_candle) — the engine's own CandleBuilder is what guarantees each call
    corresponds to a genuinely *closed* bar (Phase 0-8, unchanged here)."""
    strategy = _strategy()
    strategy.reset()
    for i in range(5):
        strategy.on_candle(_candle(100.0 + i), _ts(i))
    assert strategy._candles_seen == 5


# ------------------------------------------------------------- properties
def test_option_selection_is_atm_zero_steps():
    strategy = _strategy()
    sel = strategy.option_selection
    assert sel.moneyness is Moneyness.ATM
    assert sel.steps == 0


def test_trade_side_is_always_buy():
    assert _strategy().trade_side is OrderSide.BUY


def test_quantity_lots_reflects_config():
    assert _strategy(lots_per_trade=10).quantity_lots == 10


def test_lots_per_trade_must_be_at_least_1():
    with pytest.raises(ValueError, match="lots_per_trade"):
        _strategy(lots_per_trade=0)


def test_risk_manager_is_hard_stop_and_disabled_by_default():
    strategy = _strategy()
    rm = strategy.risk_manager
    assert rm.name == "hard_stop"
    rm.new_position(10, entry_price=100.0)
    # A true no-op: even a catastrophic unrealised loss never exits.
    assert rm.on_pnl(-1_000_000.0) is None


def test_needs_option_candles_is_true():
    assert _strategy().needs_option_candles is True


# --------------------------------------------------- 9. premium vs. underlying
def test_on_candle_never_returns_an_exit_signal():
    """Entry decisions live in on_candle; on_option_candle is the only path
    that can ever produce an EXIT — proven by construction, not just by
    convention: on_candle's only return path builds SignalAction.ENTER."""
    strategy = _strategy()
    strategy.reset()
    seq = [100.0, 100.0, 100.0, 90.0, 80.0, 70.0, 80.0, 90.0]
    for i, c in enumerate(seq):
        signal = strategy.on_candle(_candle(c), _ts(i))
        if signal is not None:
            assert signal.action is SignalAction.ENTER


def test_premium_exit_is_evaluated_on_the_traded_options_own_candles():
    """A NIFTY (underlying) candle sequence that would, on its own terms, look
    like a huge adverse move must never reach the premium exit at all — only
    on_option_candle can produce an EXIT."""
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, tick=None)
    # A wild underlying candle: on_candle only ever emits ENTER/None.
    signal = strategy.on_candle(_candle(1.0), _ts(0))
    assert signal is None or signal.action is SignalAction.ENTER


def test_on_option_candle_without_a_position_tick_yet_is_a_safe_no_op():
    strategy = _strategy()
    assert strategy.on_option_candle(_premium(90.0), _ts(0)) is None


# ------------------------------------------------- 10. momentum before +4%
def test_momentum_break_can_fire_before_4_percent_activation():
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, tick=None)

    assert strategy.on_option_candle(_premium(102.0), _ts(0)) is None  # first candle: context
    assert strategy._exit.trail_activated is False  # only +2%, not activated

    signal = strategy.on_option_candle(_premium(94.0), _ts(1))  # < prev low (97)
    assert signal is not None
    assert signal.action is SignalAction.EXIT
    assert signal.exit_reason is ExitReason.MOMENTUM_LOW
    assert strategy._exit.trail_fired is False  # trail never armed


# --------------------------------------------- 11/12. activation + retracement
def test_4_percent_move_activates_the_best_close_trail():
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, tick=None)

    strategy.on_option_candle(_premium(104.0), _ts(0))  # exactly +4%
    assert strategy._exit.trail_activated is True


def test_8_percent_retracement_fires_the_best_close_exit():
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, tick=None)

    assert strategy.on_option_candle(_premium(104.0), _ts(0)) is None  # activates
    assert strategy.on_option_candle(_premium(110.0), _ts(1)) is None  # extreme=110
    assert strategy.on_option_candle(_premium(106.0), _ts(2)) is None  # 3.6% retrace
    assert strategy.on_option_candle(_premium(102.0), _ts(3)) is None  # 7.3% retrace
    signal = strategy.on_option_candle(_premium(99.0), _ts(4))  # 10% retrace >= 8%
    assert signal is not None
    assert signal.exit_reason is ExitReason.HIGHEST_CLOSE_TRAIL
    assert strategy._exit.momentum_fired is False  # isolated: momentum did not also fire


# ------------------------------------------------- 13/14. premium candle gap
def test_first_post_gap_premium_candle_cannot_trigger_momentum_from_before_the_gap():
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, tick=None)

    strategy.on_option_candle(_premium(104.0), _ts(0))
    strategy.on_option_candle(_premium(110.0), _ts(1))  # extreme=110, low=105

    strategy.on_option_candle_gap()

    # Would break momentum against candle @110 (low=105) if not suppressed.
    signal = strategy.on_option_candle(_premium(103.0), _ts(2))
    assert signal is None
    assert strategy._exit.momentum_fired is False


def test_best_close_extreme_and_activation_survive_a_premium_data_gap():
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, tick=None)

    strategy.on_option_candle(_premium(104.0), _ts(0))
    strategy.on_option_candle(_premium(110.0), _ts(1))
    assert strategy._exit.extreme_close == 110.0
    assert strategy._exit.trail_activated is True

    strategy.on_option_candle_gap()
    assert strategy._exit.extreme_close == 110.0  # untouched by the gap
    assert strategy._exit.trail_activated is True

    strategy.on_option_candle(_premium(103.0), _ts(2))  # suppressed candle
    assert strategy._exit.extreme_close == 110.0  # still untouched
    assert strategy._exit.trail_activated is True


def test_momentum_resumes_normally_after_the_suppressed_candle():
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, tick=None)

    strategy.on_option_candle(_premium(104.0), _ts(0))
    strategy.on_option_candle(_premium(110.0), _ts(1))
    strategy.on_option_candle_gap()
    strategy.on_option_candle(_premium(103.0), _ts(2))  # suppressed, no exit

    signal = strategy.on_option_candle(_premium(95.0), _ts(3))  # < prev low (98): fires again
    assert signal is not None
    assert signal.exit_reason is ExitReason.MOMENTUM_AND_TRAIL


# ------------------------------------------------- 15/16. per-trade state reset
def test_position_close_clears_all_premium_exit_state():
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, tick=None)
    strategy.on_option_candle(_premium(104.0), _ts(0))
    strategy.on_option_candle(_premium(110.0), _ts(1))
    assert strategy.exit_state_snapshot() != {}

    _close_trade(strategy, position, exit_price=110.0)

    assert strategy.exit_state_snapshot() == {}
    assert strategy._exit.extreme_close is None
    assert strategy._exit.trail_activated is False
    assert strategy._prev_premium_candle is None
    assert strategy._last_position is None


def test_position_close_applies_exit_analytics_to_the_trade():
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, tick=None)
    strategy.on_option_candle(_premium(104.0), _ts(0))
    strategy.on_option_candle(_premium(110.0), _ts(1))

    trade = _close_trade(strategy, position, exit_price=110.0)
    assert trade.exit_mode == "MOMENTUM_LOW_OR_HIGHEST_CLOSE"
    assert trade.highest_completed_close == 110.0


def test_second_trade_does_not_inherit_the_first_trades_extreme_or_activation():
    strategy = _strategy()
    position1 = _open_position(entry=100.0, security_id="CE1")
    strategy.on_position_tick(position1, tick=None)
    strategy.on_option_candle(_premium(104.0), _ts(0))
    strategy.on_option_candle(_premium(150.0), _ts(1))  # a big favourable extreme
    _close_trade(strategy, position1, exit_price=150.0)

    position2 = _open_position(entry=50.0, security_id="CE2")
    strategy.on_position_tick(position2, tick=None)
    strategy.on_option_candle(_premium(50.0), _ts(10))
    assert strategy._exit.extreme_close == 50.0  # not 150 or 104
    assert strategy._exit.trail_activated is False


def test_a_stray_option_tick_after_close_cannot_book_another_exit():
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, tick=None)
    strategy.on_option_candle(_premium(104.0), _ts(0))
    _close_trade(strategy, position, exit_price=104.0)

    # No on_position_tick has re-armed last_position -- a stray premium candle
    # for the now-closed contract must be an inert no-op.
    assert strategy.on_option_candle(_premium(1.0), _ts(99)) is None


# --------------------------------------------------------- 17. restart recovery
def test_exit_state_snapshot_and_restore_round_trip():
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, tick=None)
    strategy.on_option_candle(_premium(104.0), _ts(0))
    strategy.on_option_candle(_premium(110.0), _ts(1))

    snapshot = strategy.exit_state_snapshot()
    assert snapshot["highest_close"] == 110.0
    assert snapshot["trail"]["activated"] is True

    restarted = _strategy()
    restarted.restore_exit_state(snapshot)
    assert restarted._exit.extreme_close == 110.0
    assert restarted._exit.trail_activated is True


def test_restore_with_foreign_or_empty_data_does_not_raise():
    strategy = _strategy()
    strategy.restore_exit_state({})
    strategy.restore_exit_state({"unexpected": "value", "highest_close": "not-a-number"})
