"""Unit tests for the pluggable exit-engine subsystem (framework.exit)."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from common.exit import available_exit_engines, build_exit_engine, get_exit_engine
from common.indicators.base import OHLC
from common.indicators.supertrend import SuperTrend
from common.models import ExitReason, OptionType, OrderSide

IST = ZoneInfo("Asia/Kolkata")


def _c(close, high=None, low=None):
    return OHLC(
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        open=close,
    )


def _pos(option_type, side=OrderSide.BUY, entry=180.0, last=180.0):
    return SimpleNamespace(
        contract=SimpleNamespace(option_type=option_type),
        side=side,
        entry_price=entry,
        last_price=last,
    )


def _exit_cfg(mode="", **blocks):
    return SimpleNamespace(enabled=True, mode=mode, settings=blocks)


# ------------------------------------------------------------- registry
def test_all_engines_registered():
    assert set(available_exit_engines()) == {
        "consecutive_reversal",
        "fixed_target",
        "highest_close",
        "momentum_close",
        "momentum_low",
        "momentum_low_or_highest_close",
        "stoploss",
        "supertrend",
        "time_exit",
        "trailing",
    }


# ------------------------------------------------------------- momentum_close
def test_momentum_close_ce():
    e = get_exit_engine("momentum_close")
    assert e.should_exit(_pos(OptionType.CE), _c(99), [_c(100)], {}, None) is True
    assert e.should_exit(_pos(OptionType.CE), _c(101), [_c(100)], {}, None) is False
    # No history (entry candle) => never exits.
    assert e.should_exit(_pos(OptionType.CE), _c(50), [], {}, None) is False


def test_momentum_close_pe():
    e = get_exit_engine("momentum_close")
    assert e.should_exit(_pos(OptionType.PE), _c(101), [_c(100)], {}, None) is True
    assert e.should_exit(_pos(OptionType.PE), _c(99), [_c(100)], {}, None) is False


def test_momentum_close_option_premium_is_side_aware_and_consecutive():
    e = get_exit_engine("momentum_close", {"price_stream": "premium"})

    # For an option-premium chart, CE/PE direction is irrelevant: a long option
    # exits on the first lower consecutive close.
    assert e.should_exit_closes(99, 100, side=OrderSide.BUY, option_type=OptionType.PE) is True
    assert e.should_exit_closes(101, 100, side=OrderSide.BUY, option_type=OptionType.PE) is False

    # A written option exits on the first higher consecutive premium close.
    assert e.should_exit_closes(101, 100, side=OrderSide.SELL, option_type=OptionType.CE) is True
    assert e.should_exit_closes(99, 100, side=OrderSide.SELL, option_type=OptionType.CE) is False
    assert e.should_exit_closes(99, None, side=OrderSide.BUY) is False


def test_momentum_close_rejects_unknown_price_stream():
    with pytest.raises(ValueError, match="price_stream"):
        get_exit_engine("momentum_close", {"price_stream": "invalid"})


# ------------------------------------------------------------- momentum_low
def test_momentum_low_uses_prev_extreme():
    e = get_exit_engine("momentum_low")
    prev = _c(100, high=105, low=95)
    # CE exits only when close breaks the previous LOW (95), not merely prev close.
    assert e.should_exit(_pos(OptionType.CE), _c(96), [prev], {}, None) is False
    assert e.should_exit(_pos(OptionType.CE), _c(94), [prev], {}, None) is True
    # PE exits when close breaks the previous HIGH (105).
    assert e.should_exit(_pos(OptionType.PE), _c(106), [prev], {}, None) is True


def test_momentum_low_side_basis_holds_on_exact_equality():
    """Strict `<`/`>`: a close exactly AT the previous extreme must hold, not
    exit — the spec's boundary rule for MOMENTUM_LOW."""
    e = get_exit_engine("momentum_low", {"basis": "side"})
    prev = _c(100, high=105, low=95)
    long_pos = _pos(OptionType.CE, side=OrderSide.BUY)
    assert e.should_exit(long_pos, _c(95), [prev], {}, None) is False  # == prev.low -> hold
    assert e.should_exit(long_pos, _c(94.99), [prev], {}, None) is True  # just below -> exit
    short_pos = _pos(OptionType.CE, side=OrderSide.SELL)
    assert e.should_exit(short_pos, _c(105), [prev], {}, None) is False  # == prev.high -> hold
    assert e.should_exit(short_pos, _c(105.01), [prev], {}, None) is True  # just above -> exit


def test_momentum_low_side_basis_is_side_aware_not_ce_pe():
    e = get_exit_engine("momentum_low", {"basis": "side"})
    prev = _c(100, high=105, low=95)
    # A bought PE is still "long" in premium terms: exits on close < prev.low,
    # exactly like a bought CE would — unlike basis="option_type".
    assert e.should_exit(_pos(OptionType.PE, side=OrderSide.BUY), _c(94), [prev], {}, None) is True
    assert e.should_exit(_pos(OptionType.PE, side=OrderSide.BUY), _c(96), [prev], {}, None) is False
    # A written (SELL) option exits on close > prev.high, regardless of CE/PE.
    assert (
        e.should_exit(_pos(OptionType.CE, side=OrderSide.SELL), _c(106), [prev], {}, None) is True
    )
    assert (
        e.should_exit(_pos(OptionType.CE, side=OrderSide.SELL), _c(104), [prev], {}, None) is False
    )


def test_momentum_low_rejects_unknown_basis():
    with pytest.raises(ValueError, match="basis"):
        get_exit_engine("momentum_low", {"basis": "nonsense"})


# ------------------------------------------------------------- highest_close
def test_highest_close_trails_ce():
    e = get_exit_engine("highest_close", {"trail_points": 20})
    e.reset()
    assert e.should_exit(_pos(OptionType.CE), _c(100), [], {}, None) is False  # peak=100
    assert e.should_exit(_pos(OptionType.CE), _c(130), [], {}, None) is False  # peak=130
    assert e.should_exit(_pos(OptionType.CE), _c(115), [], {}, None) is False  # 130-115=15 < 20
    assert e.should_exit(_pos(OptionType.CE), _c(109), [], {}, None) is True  # 130-109=21 >= 20


def test_highest_close_reset_clears_peak():
    e = get_exit_engine("highest_close", {"trail_points": 20})
    e.should_exit(_pos(OptionType.CE), _c(200), [], {}, None)
    e.reset()
    # After reset the old 200 peak is gone; a fresh 100 is the new peak.
    assert e.should_exit(_pos(OptionType.CE), _c(100), [], {}, None) is False


def test_highest_close_rejects_unknown_trail_type():
    with pytest.raises(ValueError, match="trail_type"):
        get_exit_engine("highest_close", {"trail_type": "nonsense"})


def test_highest_close_percentage_long():
    e = get_exit_engine(
        "highest_close", {"trail_type": "PERCENTAGE", "trail_percentage": 8.0, "basis": "side"}
    )
    e.reset()
    long_pos = _pos(OptionType.CE, side=OrderSide.BUY, entry=100.0)
    assert e.should_exit(long_pos, _c(100), [], {}, None) is False  # peak=100
    assert e.should_exit(long_pos, _c(140), [], {}, None) is False  # peak=140
    # 140 -> 129: retrace = 11/140 = 7.86% < 8% -> hold
    assert e.should_exit(long_pos, _c(129), [], {}, None) is False
    # 140 -> 128: retrace = 12/140 = 8.57% >= 8% -> exit
    assert e.should_exit(long_pos, _c(128), [], {}, None) is True


def test_highest_close_percentage_short():
    e = get_exit_engine(
        "highest_close", {"trail_type": "PERCENTAGE", "trail_percentage": 8.0, "basis": "side"}
    )
    e.reset()
    short_pos = _pos(OptionType.CE, side=OrderSide.SELL, entry=100.0)
    assert e.should_exit(short_pos, _c(100), [], {}, None) is False  # trough=100
    assert e.should_exit(short_pos, _c(70), [], {}, None) is False  # trough=70
    # 70 -> 76: rebound = 6/70 = 8.57% >= 8% -> exit
    assert e.should_exit(short_pos, _c(76), [], {}, None) is True


def test_highest_close_percentage_activation_gate():
    e = get_exit_engine(
        "highest_close",
        {
            "trail_type": "PERCENTAGE",
            "trail_percentage": 8.0,
            "basis": "side",
            "activation": {"enabled": True, "minimum_favourable_move_percentage": 4.0},
        },
    )
    e.reset()
    long_pos = _pos(OptionType.CE, side=OrderSide.BUY, entry=100.0)
    # Peak only reaches 102 (2% favourable move) -> activation gate never arms,
    # so even a qualifying retracement percentage must not fire.
    assert e.should_exit(long_pos, _c(102), [], {}, None) is False
    assert e.should_exit(long_pos, _c(90), [], {}, None) is False
    # Peak reaches 110 (10% favourable move) -> gate arms; now an 8%+ retrace fires.
    assert e.should_exit(long_pos, _c(110), [], {}, None) is False
    assert e.should_exit(long_pos, _c(100), [], {}, None) is True  # 10/110=9.09% >= 8%


def test_highest_close_percentage_short_activation_gate():
    e = get_exit_engine(
        "highest_close",
        {
            "trail_type": "PERCENTAGE",
            "trail_percentage": 8.0,
            "basis": "side",
            "activation": {"enabled": True, "minimum_favourable_move_percentage": 4.0},
        },
    )
    e.reset()
    short_pos = _pos(OptionType.CE, side=OrderSide.SELL, entry=100.0)
    # Trough only reaches 98 (2% favourable move for a short) -> gate never
    # arms, so a qualifying rebound percentage must not fire.
    assert e.should_exit(short_pos, _c(98), [], {}, None) is False
    assert e.should_exit(short_pos, _c(110), [], {}, None) is False
    # Trough reaches 90 (10% favourable move) -> gate arms; an 8%+ rebound fires.
    assert e.should_exit(short_pos, _c(90), [], {}, None) is False
    assert e.should_exit(short_pos, _c(98), [], {}, None) is True  # 8/90=8.89% >= 8%


def test_highest_close_percentage_exact_threshold_equality_exits():
    """'Drawdown equal to threshold -> exit' per the spec: PERCENTAGE trail
    uses >=, not strict >."""
    e = get_exit_engine(
        "highest_close", {"trail_type": "PERCENTAGE", "trail_percentage": 10.0, "basis": "side"}
    )
    e.reset()
    long_pos = _pos(OptionType.CE, side=OrderSide.BUY, entry=100.0)
    e.should_exit(long_pos, _c(100), [], {}, None)  # peak=100
    assert e.should_exit(long_pos, _c(90), [], {}, None) is True  # exactly 10.0% retrace -> exit


# --------------------------------------------- momentum_low_or_highest_close
def test_combined_engine_momentum_only():
    e = get_exit_engine("momentum_low_or_highest_close", {"trail_percentage": 8.0})
    e.reset()
    prev = _c(100, high=105, low=95)
    long_pos = _pos(OptionType.CE, side=OrderSide.BUY, entry=100.0)
    assert e.should_exit(long_pos, _c(94), [prev], {}, None) is True
    assert e.momentum_fired is True
    assert e.trail_fired is False
    assert e.exit_reason is ExitReason.MOMENTUM_LOW


def test_combined_engine_trail_only():
    e = get_exit_engine("momentum_low_or_highest_close", {"trail_percentage": 8.0})
    e.reset()
    long_pos = _pos(OptionType.CE, side=OrderSide.BUY, entry=100.0)
    # Build a peak with no history so momentum_low never fires (no candle_history).
    assert e.should_exit(long_pos, _c(140), [], {}, None) is False
    assert e.should_exit(long_pos, _c(128), [], {}, None) is True  # 140->128 = 8.57% retrace
    assert e.trail_fired is True
    assert e.momentum_fired is False
    assert e.exit_reason is ExitReason.HIGHEST_CLOSE_TRAIL


def test_combined_engine_both_fire_together():
    e = get_exit_engine("momentum_low_or_highest_close", {"trail_percentage": 8.0})
    e.reset()
    long_pos = _pos(OptionType.CE, side=OrderSide.BUY, entry=100.0)
    peak = _c(140, high=141, low=139)
    assert e.should_exit(long_pos, peak, [], {}, None) is False
    # Close breaks below peak's low (139) AND retraces >= 8% from peak (140) -> both fire.
    assert e.should_exit(long_pos, _c(128), [peak], {}, None) is True
    assert e.momentum_fired is True
    assert e.trail_fired is True
    assert e.exit_reason is ExitReason.MOMENTUM_AND_TRAIL


def test_combined_engine_short_side_reasons():
    e = get_exit_engine("momentum_low_or_highest_close", {"trail_percentage": 8.0})
    e.reset()
    short_pos = _pos(OptionType.PE, side=OrderSide.SELL, entry=100.0)
    prev = _c(100, high=105, low=95)
    assert e.should_exit(short_pos, _c(106), [prev], {}, None) is True
    assert e.exit_reason is ExitReason.MOMENTUM_HIGH


def test_combined_engine_reset_clears_extreme_and_flags():
    e = get_exit_engine("momentum_low_or_highest_close", {"trail_percentage": 8.0})
    e.reset()
    long_pos = _pos(OptionType.CE, side=OrderSide.BUY, entry=100.0)
    e.should_exit(long_pos, _c(200), [], {}, None)
    assert e.extreme_close == 200
    e.reset()
    assert e.extreme_close is None
    assert e.momentum_fired is False
    assert e.trail_fired is False


def test_combined_engine_persists_complete_trade_analytics():
    e = get_exit_engine("momentum_low_or_highest_close", {"trail_percentage": 8.0})
    pos = _pos(OptionType.CE, side=OrderSide.BUY, entry=100.0)
    e.should_exit(pos, _c(140, high=141, low=139), [], {}, None)
    e.should_exit(pos, _c(128), [_c(140, high=141, low=139)], {}, None)
    trade = SimpleNamespace(side=OrderSide.BUY, exit_price=128.0)

    e.apply_to_trade(trade)

    assert trade.exit_mode == "MOMENTUM_LOW_OR_HIGHEST_CLOSE"
    assert trade.highest_completed_close == 140.0
    assert trade.lowest_completed_close == 128.0
    assert trade.best_favourable_close == 140.0
    assert trade.retracement_points == 12.0
    assert trade.retracement_percentage == pytest.approx(12 / 140 * 100)
    assert trade.candle_structure_triggered is True
    assert trade.trail_triggered is True


# ------------------------------------------------------------- consecutive
def test_consecutive_reversal_ce():
    e = get_exit_engine("consecutive_reversal", {"reverse_candles": 2})
    e.reset()
    assert e.should_exit(_pos(OptionType.CE), _c(100), [], {}, None) is False  # seed
    assert e.should_exit(_pos(OptionType.CE), _c(99), [], {}, None) is False  # streak 1
    assert e.should_exit(_pos(OptionType.CE), _c(98), [], {}, None) is True  # streak 2


def test_consecutive_reversal_streak_resets():
    e = get_exit_engine("consecutive_reversal", {"reverse_candles": 2})
    e.reset()
    e.should_exit(_pos(OptionType.CE), _c(100), [], {}, None)
    e.should_exit(_pos(OptionType.CE), _c(99), [], {}, None)  # streak 1
    assert e.should_exit(_pos(OptionType.CE), _c(101), [], {}, None) is False  # up -> reset
    assert e.should_exit(_pos(OptionType.CE), _c(100), [], {}, None) is False  # streak 1 again


# ------------------------------------------------------------- supertrend
def test_supertrend_exit_ce_on_downtrend():
    st = SuperTrend(period=1, multiplier=1.0)
    for close in (100, 101, 102):
        st.update(_c(close))
    e = get_exit_engine("supertrend")
    # Uptrend so far: a CE should hold.
    assert e.should_exit(_pos(OptionType.CE), _c(102), [], {"supertrend": st}, None) is False
    # Drive a strong down-move to flip the trend, then a CE should exit.
    for close in (90, 80, 70):
        st.update(_c(close))
    assert e.should_exit(_pos(OptionType.CE), _c(70), [], {"supertrend": st}, None) is True


# ------------------------------------------------------------- premium engines
def test_stoploss_points_buy():
    e = get_exit_engine("stoploss", {"points": 30})
    assert (
        e.should_exit(_pos(OptionType.CE, entry=180, last=155), _c(1), [], {}, None) is False
    )  # loss 25 < 30
    assert (
        e.should_exit(_pos(OptionType.CE, entry=180, last=149), _c(1), [], {}, None) is True
    )  # loss 31 >= 30
    assert (
        e.should_exit(_pos(OptionType.CE, entry=180, last=160), _c(1), [], {}, None) is False
    )  # loss 20 < 30


def test_fixed_target_points_buy():
    e = get_exit_engine("fixed_target", {"points": 50})
    assert (
        e.should_exit(_pos(OptionType.CE, entry=180, last=235), _c(1), [], {}, None) is True
    )  # +55
    assert (
        e.should_exit(_pos(OptionType.CE, entry=180, last=210), _c(1), [], {}, None) is False
    )  # +30


def test_trailing_premium_gives_back():
    e = get_exit_engine("trailing", {"trail_points": 15})
    e.reset()
    # Ride profit up to +40 (last 220 vs entry 180), then give back to +20 -> exit.
    assert (
        e.should_exit(_pos(OptionType.CE, entry=180, last=220), _c(1), [], {}, None) is False
    )  # peak 40
    assert (
        e.should_exit(_pos(OptionType.CE, entry=180, last=200), _c(1), [], {}, None) is True
    )  # 40-20=20 >= 15


# ------------------------------------------------------------- time_exit
def test_time_exit():
    e = get_exit_engine("time_exit", {"time": "15:15"})
    before = datetime(2026, 7, 1, 15, 10, tzinfo=IST)
    after = datetime(2026, 7, 1, 15, 15, tzinfo=IST)
    assert e.should_exit(_pos(OptionType.CE), _c(1), [], {}, None, timestamp=before) is False
    assert e.should_exit(_pos(OptionType.CE), _c(1), [], {}, None, timestamp=after) is True


# ------------------------------------------------------------- composite/factory
def test_build_exit_engine_disabled_returns_none():
    assert build_exit_engine(SimpleNamespace(enabled=False, mode="", settings={})) is None
    assert build_exit_engine(None) is None


def test_composite_mode_implies_enabled_and_priority():
    cfg = _exit_cfg(
        mode="MOMENTUM_CLOSE",
        stoploss={"enabled": True, "points": 30},
        momentum_close={"enabled": True},
    )
    comp = build_exit_engine(cfg)
    # Both engines present; stoploss has higher priority than the directional one.
    assert [e.label for e in comp.engines] == ["STOPLOSS", "MOMENTUM_CLOSE"]

    # A candle that both breaks momentum AND breaches the premium stop -> stoploss
    # wins the reason (evaluated first).
    pos = _pos(OptionType.CE, entry=180, last=140)  # loss 40 >= 30
    assert comp.should_exit(pos, _c(99), [_c(100)], {}, None, timestamp=None) is True
    assert comp.last_fired.exit_reason is ExitReason.STOP_LOSS


def test_composite_no_fire_when_conditions_absent():
    comp = build_exit_engine(_exit_cfg(mode="MOMENTUM_CLOSE", momentum_close={"enabled": True}))
    assert comp.should_exit(_pos(OptionType.CE), _c(101), [_c(100)], {}, None) is False
    assert comp.last_fired is None


def test_unknown_engine_raises():
    with pytest.raises(KeyError):
        get_exit_engine("does_not_exist")
