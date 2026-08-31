"""Behaviour-level proofs for ``SupertrendBuy1x1p2Strategy``.

Full spec: ``strategies/intraday_options/supertrend_buy_1_1p2/
SUPERTREND_BUY_1_1P2_ALGO_TRADING_SPEC.md``. Parity source: the legacy
``Trading_Automation`` ``supertrend_fast`` strategy's own code, config and tests —
**not** ``NiftyFixedStrikeSuperTrend_Master_Specification.md``, which documents a
different fixed-strike, two-position strategy (settled before implementation).

These exercise the strategy class directly (no ``TradingEngine``), so the fresh-flip
invariant, the premium-exit wiring and per-trade state reset can be pinned with exact,
hand-verified numbers. Timing, reversal, sizing, the daily cap and restart recovery —
everything the *engine* owns rather than the strategy — belong to the integration
suites instead.

Unlike ``c921_ema_cross_buy``'s tests, the production parameters are used throughout:
SuperTrend(1, 1.2) flips within two or three candles, so there is no reason to shrink
them for the tests and every reason not to. Every underlying-candle sequence below was
walked through the real :class:`~common.indicators.supertrend.SuperTrend` before being
hard-coded, and the two three-candle sequences are the legacy suite's own
(``test_flip_up_enters_ce`` and ``test_no_flip_no_signal``) verbatim.

Premium candles always carry a wick (``low = close - low_pad``) precisely so a
momentum-break comparison (``close < previous candle's low``) and a best-close
retracement can be driven independently in the same walk.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
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
from common.engine.strategy import BaseStrategy, available_strategies, get_strategy
from common.indicators.base import OHLC
from common.indicators.supertrend import DOWNTREND, UPTREND
from strategies.intraday_options.supertrend_buy_1_1p2.strategy import (
    DEFAULT_WARMUP_MIN_BARS,
    SupertrendBuy1x1p2Strategy,
)

IST = ZoneInfo("Asia/Kolkata")


def _ts(i: int) -> datetime:
    return datetime(2026, 8, 20, 9, 20, tzinfo=IST) + timedelta(minutes=5 * i)


def _candle(close: float) -> OHLC:
    """A flat underlying candle (high == low == close).

    With ``period=1`` the Wilder ATR is the bar's own True Range, so a flat candle
    makes the range purely a function of the close-to-close move — which is what keeps
    the hard-coded sequences below readable.
    """
    return OHLC(high=close, low=close, close=close)


def _premium(close: float, *, low_pad: float = 5.0, high_pad: float = 2.0) -> OHLC:
    return OHLC(high=close + high_pad, low=close - low_pad, close=close)


def _strategy(*, trusted: bool = True, **kwargs) -> SupertrendBuy1x1p2Strategy:
    """A strategy with production parameters, warmed context by default.

    ``trusted=True`` stands in for the engine's own
    ``on_warmup_complete(context_trusted=True)`` call after a verified-complete replay.
    Tests that care about the untrusted path pass ``trusted=False`` explicitly.
    """
    strategy = SupertrendBuy1x1p2Strategy(**kwargs)
    strategy.on_warmup_complete(context_trusted=trusted)
    return strategy


def _feed(strategy: SupertrendBuy1x1p2Strategy, closes: list[float]) -> list[object]:
    return [strategy.on_candle(_candle(c), _ts(i)) for i, c in enumerate(closes)]


def _open_position(entry: float = 100.0, security_id: str = "CE1") -> OpenPosition:
    contract = OptionContract(
        symbol=security_id,
        security_id=security_id,
        strike=100,
        option_type=OptionType.CE,
        expiry="2026-08-27",
        lot_size=75,
    )
    return OpenPosition(
        contract=contract, side=OrderSide.BUY, lots=1, entry_price=entry, entry_time=_ts(0)
    )


def _close_trade(
    strategy: SupertrendBuy1x1p2Strategy,
    position: OpenPosition,
    exit_price: float,
    exit_reason: ExitReason = ExitReason.STRATEGY_EXIT,
) -> Trade:
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


# --------------------------------------------------------------- registration
def test_strategy_registers_as_supertrend_buy_1_1p2():
    assert "supertrend_buy_1_1p2" in available_strategies()
    assert SupertrendBuy1x1p2Strategy.name == "supertrend_buy_1_1p2"
    assert issubclass(SupertrendBuy1x1p2Strategy, BaseStrategy)


def test_get_strategy_by_name_builds_from_cfg_parameters():
    """The registry construction path (``cls(cfg)``) reads the same knobs the
    runtime's keyword path does — ``_pick``'s reason for existing."""
    cfg = SimpleNamespace(
        parameters={
            "supertrend_period": 2,
            "supertrend_multiplier": 3.0,
            "lots_per_trade": 4,
            "warmup_min_bars": 90,
        }
    )
    strategy = get_strategy("supertrend_buy_1_1p2", cfg)
    assert isinstance(strategy, SupertrendBuy1x1p2Strategy)
    assert strategy._supertrend.period == 2
    assert strategy._supertrend.multiplier == 3.0
    assert strategy.quantity_lots == 4
    spec = strategy.warmup_spec()
    assert spec is not None and spec.min_bars == 90


def test_explicit_kwargs_win_over_cfg_parameters():
    cfg = SimpleNamespace(parameters={"lots_per_trade": 4})
    assert SupertrendBuy1x1p2Strategy(cfg, lots_per_trade=7).quantity_lots == 7


# ------------------------------------------------------------------ parameters
def test_shipped_defaults_are_the_legacy_parameters():
    """Spec sections 6/8/10: ATR period 1, multiplier 1.2, 10 lots, 8% trail with a
    4% activation gate — the legacy ``supertrend_fast`` config's own values."""
    strategy = SupertrendBuy1x1p2Strategy()
    assert strategy._supertrend.period == 1
    assert strategy._supertrend.multiplier == 1.2
    assert strategy.quantity_lots == 10
    assert strategy._exit._trail.trail_percentage == 8.0
    assert strategy._exit._trail.activation_enabled is True
    assert strategy._exit._trail.min_favourable_move_percentage == 4.0


def test_default_warmup_min_bars_is_a_full_session_not_the_atr_period():
    """Spec section 7. ``SuperTrend.warmup_requirement().min_bars`` is ``period``
    (1 here) — an *ATR readiness* number, not a trend-continuity one. The strategy
    raises the floor locally; the indicator is untouched."""
    strategy = SupertrendBuy1x1p2Strategy()
    assert DEFAULT_WARMUP_MIN_BARS == 75
    assert strategy._supertrend.warmup_requirement().min_bars == 1
    spec = strategy.warmup_spec()
    assert spec is not None
    assert spec.min_bars == 75
    assert spec.continuity_required is True


def test_warmup_spec_never_lowers_the_indicators_own_floor():
    """``max``, not an override: an indicator that one day declared a higher floor
    must win rather than be silently capped by this strategy's constant."""
    strategy = SupertrendBuy1x1p2Strategy(supertrend_period=200)
    spec = strategy.warmup_spec()
    assert spec is not None and spec.min_bars == 200


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"supertrend_multiplier": 0}, "supertrend_multiplier must be > 0"),
        ({"supertrend_multiplier": -1.2}, "supertrend_multiplier must be > 0"),
        ({"lots_per_trade": 0}, "lots_per_trade must be >= 1"),
        ({"warmup_min_bars": 0}, "warmup_min_bars must be >= 1"),
    ],
)
def test_invalid_parameters_are_refused_at_construction(kwargs, message):
    with pytest.raises(ValueError, match=message):
        SupertrendBuy1x1p2Strategy(**kwargs)


def test_supertrend_period_below_one_is_refused_by_the_indicator():
    with pytest.raises(ValueError, match="period must be >= 1"):
        SupertrendBuy1x1p2Strategy(supertrend_period=0)


# ------------------------------------------------------- 18.1 signal and entry
def test_the_seeding_candle_never_produces_an_entry():
    """Spec section 6: "First initialization/seed produces no entry." The indicator's
    own ``flipped`` is False when no previous trend existed, which is the mechanism."""
    strategy = _strategy()
    assert strategy.on_candle(_candle(20000), _ts(0)) is None
    assert strategy._supertrend.state.flipped is False


def test_the_first_candle_always_seeds_uptrend_at_this_multiplier():
    """Why the spec's "initial state becomes DOWN" row cannot be reached by a single
    candle, and therefore how it is actually satisfied.

    On bar 0 the ATR *is* the bar range ``h - l``, so ``lower_basic = hl2 - 1.2(h-l)``
    which sits below ``l`` for any multiplier above 0.5. The close can never be under
    it, so the seed is UP whatever the candle looks like. A DOWN *context* is therefore
    always the product of a later flip — proven by
    ``test_a_down_context_continuation_produces_no_entry`` — and the "no entry on the
    initial state" rule is the ``flipped``-is-False rule tested above, in both
    directions."""
    for candle in (
        _candle(20000),
        OHLC(high=20100, low=19900, close=19901),
        OHLC(high=20100, low=19900, close=20099),
    ):
        strategy = _strategy()
        assert strategy.on_candle(candle, _ts(0)) is None
        assert strategy._supertrend.state.trend == UPTREND


def test_trusted_up_context_fresh_down_flip_buys_pe():
    """Spec 18.1: "Trusted context UP, fresh flip DOWN | Buy ATM weekly PE"."""
    strategy = _strategy()
    signals = _feed(strategy, [20000, 19500])
    assert signals[0] is None
    signal = signals[1]
    assert signal is not None
    assert signal.action is SignalAction.ENTER
    assert signal.option_type is OptionType.PE
    assert signal.side is OrderSide.BUY
    assert "flipped to DOWN" in signal.reason
    assert strategy._supertrend.state.trend == DOWNTREND


def test_trusted_down_context_fresh_up_flip_buys_ce():
    """Spec 18.1: "Trusted context DOWN, fresh flip UP | Buy ATM weekly CE".

    The candle sequence is the legacy suite's ``test_flip_up_enters_ce`` verbatim."""
    strategy = _strategy()
    signals = _feed(strategy, [20000, 19500, 20200])
    signal = signals[2]
    assert signal is not None
    assert signal.action is SignalAction.ENTER
    assert signal.option_type is OptionType.CE
    assert signal.side is OrderSide.BUY
    assert "flipped to UP" in signal.reason


def test_an_up_context_continuation_produces_no_entry():
    """Spec 18.1: "UP remains UP | No new order". Legacy ``test_no_flip_no_signal``'s
    own sequence."""
    strategy = _strategy()
    assert _feed(strategy, [20000, 20010, 20020, 20030]) == [None, None, None, None]
    assert strategy._supertrend.state.trend == UPTREND


def test_a_down_context_continuation_produces_no_entry():
    """Spec 18.1: "DOWN remains DOWN | No new order"."""
    strategy = _strategy()
    signals = _feed(strategy, [20000, 19500, 19400, 19300])
    assert signals[1] is not None and signals[1].option_type is OptionType.PE
    assert signals[2] is None
    assert signals[3] is None
    assert strategy._supertrend.state.trend == DOWNTREND


def test_one_flip_produces_exactly_one_signal():
    """Spec section 6: "One flip produces at most one actionable signal." The flip
    candle signals; every subsequent candle in the same trend does not."""
    strategy = _strategy()
    signals = _feed(strategy, [20000, 19500, 20200, 20300])
    entries = [s for s in signals if s is not None]
    assert len(entries) == 2  # the DOWN flip and the UP flip, one each
    assert signals[3] is None  # the UP trend simply continued


def test_alternating_flips_alternate_ce_and_pe():
    strategy = _strategy()
    signals = _feed(strategy, [20000, 19500, 20200, 20300, 19000])
    assert [s.option_type for s in signals if s is not None] == [
        OptionType.PE,
        OptionType.CE,
        OptionType.PE,
    ]


def test_on_candle_never_returns_an_exit_signal():
    """Spec section 10: exits live on the option's own premium candles. The underlying
    stream only ever produces ENTER."""
    strategy = _strategy()
    for signal in _feed(strategy, [20000, 19500, 20200, 20300, 19000, 19100, 21000]):
        if signal is not None:
            assert signal.action is SignalAction.ENTER


# -------------------------------------------------- 18.5 warm-up trust gating
def test_an_untrusted_context_withholds_the_entry():
    """Spec section 7: only WARMED grants trusted context and permits entries."""
    strategy = _strategy(trusted=False)
    assert _feed(strategy, [20000, 19500, 20200]) == [None, None, None]


def test_an_untrusted_context_still_advances_the_indicator():
    """Withholding an entry must not corrupt the indicator: the flip still happened,
    it just was not tradable. This is what keeps a later trusted run consistent."""
    strategy = _strategy(trusted=False)
    _feed(strategy, [20000, 19500])
    assert strategy._supertrend.state.trend == DOWNTREND
    assert strategy._supertrend.state.flipped is True


def test_on_warmup_complete_defaults_to_untrusted():
    """Fail-conservative: an omitted argument must behave exactly like an untrusted
    replay, never silently certify one."""
    strategy = SupertrendBuy1x1p2Strategy()
    strategy.on_warmup_complete()
    assert _feed(strategy, [20000, 19500]) == [None, None]


def test_a_strategy_never_told_about_warmup_cannot_trade():
    strategy = SupertrendBuy1x1p2Strategy()
    assert _feed(strategy, [20000, 19500]) == [None, None]


def test_untrusted_warmup_does_not_reset_the_indicator():
    """Clearing a latched indicator would make the *next* candle a fresh seed and the
    one after it a fabricated flip. The correct fail-closed action is to keep the
    state and withhold entries."""
    strategy = _strategy()
    _feed(strategy, [20000, 19500])
    strategy.on_warmup_complete(context_trusted=False)
    assert strategy._supertrend.state.trend == DOWNTREND
    assert strategy._supertrend.count == 2


# ------------------------------------------------------------ day-start reset
def test_day_start_reset_clears_indicator_trust_and_exit_state():
    """Spec section 7: "No strategy state or trust leakage across days/runs."

    The SuperTrend *is* cleared here, unlike ``c921_ema_cross_buy``'s EMAs: a latched
    trend carried into a day whose own warm-up has not run yet is exactly what
    manufactures a false flip. Matches the legacy ``reset()``, which called
    ``self._st.reset()`` too."""
    strategy = _strategy()
    position = _open_position()
    strategy.on_position_tick(position, SimpleNamespace())  # type: ignore[arg-type]
    strategy.on_option_candle(_premium(104.0), _ts(1))
    _feed(strategy, [20000, 19500])

    strategy.reset()

    assert strategy._supertrend.count == 0
    assert strategy._supertrend.is_ready is False
    assert strategy._context_trusted is False
    assert strategy._exit.extreme_close is None
    assert strategy._exit.highest_close is None
    assert strategy._prev_premium_candle is None
    assert strategy._last_position is None
    # And a post-reset run really is a fresh seed: the first candle signals nothing.
    strategy.on_warmup_complete(context_trusted=True)
    assert strategy.on_candle(_candle(19500), _ts(10)) is None


def test_status_is_safe_before_the_first_candle():
    assert "none" in SupertrendBuy1x1p2Strategy().status()


# ------------------------------------------------------------ 18.4 premium exit
def test_needs_option_candles_is_true():
    assert SupertrendBuy1x1p2Strategy().needs_option_candles is True


def test_on_option_candle_without_a_position_tick_yet_is_a_safe_no_op():
    """The entry fill can race the first premium candle close."""
    assert _strategy().on_option_candle(_premium(100.0), _ts(1)) is None


def test_momentum_break_can_fire_before_4_percent_activation():
    """Spec 10.1: the momentum leg has no activation gate. Two consecutive premium
    candles, the second closing below the first's low, at a loss."""
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, SimpleNamespace())  # type: ignore[arg-type]
    assert strategy.on_option_candle(_premium(100.0, low_pad=5.0), _ts(1)) is None
    signal = strategy.on_option_candle(_premium(94.0), _ts(2))
    assert signal is not None
    assert signal.action is SignalAction.EXIT
    assert signal.exit_reason is ExitReason.MOMENTUM_LOW
    assert strategy._exit.trail_activated is False


def test_a_favourable_move_below_4_percent_leaves_the_trail_inactive():
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, SimpleNamespace())  # type: ignore[arg-type]
    assert strategy.on_option_candle(_premium(103.99, low_pad=20.0), _ts(1)) is None
    assert strategy._exit.trail_activated is False


def test_exactly_4_percent_activates_the_best_close_trail():
    """Spec 18.4: "Favourable move exactly 4% | Trail activates" — inclusive."""
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, SimpleNamespace())  # type: ignore[arg-type]
    assert strategy.on_option_candle(_premium(104.0, low_pad=20.0), _ts(1)) is None
    assert strategy._exit.trail_activated is True
    assert strategy._exit.extreme_close == 104.0


def test_an_activated_retracement_below_8_percent_holds():
    """Spec 18.4: "Activated retracement below 8% | Hold"."""
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, SimpleNamespace())  # type: ignore[arg-type]
    strategy.on_option_candle(_premium(125.0, low_pad=40.0), _ts(1))  # activates, extreme 125
    # 125 -> 115.1 is 7.92%, just inside the 8% threshold.
    assert strategy.on_option_candle(_premium(115.1, low_pad=40.0), _ts(2)) is None


def test_exactly_8_percent_retracement_fires_the_best_close_exit():
    """Spec 18.4: "Activated retracement exactly 8% | Exit" — inclusive.

    ``125 -> 115`` is chosen over a prettier ``104 -> 95.68`` deliberately: the latter
    evaluates to 7.999999999999993 in IEEE-754 double arithmetic and would therefore
    test the *below*-threshold branch while claiming to test equality. ``(125 - 115) /
    125 * 100`` is exactly ``8.0``, so this really does pin the ``>=``."""
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, SimpleNamespace())  # type: ignore[arg-type]
    strategy.on_option_candle(_premium(125.0, low_pad=40.0), _ts(1))  # activates, extreme 125
    assert (125.0 - 115.0) / 125.0 * 100 == 8.0
    signal = strategy.on_option_candle(_premium(115.0, low_pad=40.0), _ts(2))
    assert signal is not None
    assert signal.action is SignalAction.EXIT
    assert signal.exit_reason is ExitReason.HIGHEST_CLOSE_TRAIL
    assert strategy._exit.momentum_fired is False  # isolated: momentum did not also fire


def test_both_legs_firing_together_reports_one_combined_reason():
    """Spec 18.4: "Both conditions fire | One exit with combined/granular reason"."""
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, SimpleNamespace())  # type: ignore[arg-type]
    strategy.on_option_candle(_premium(125.0, low_pad=1.0), _ts(1))  # activates, low 124
    signal = strategy.on_option_candle(_premium(115.0), _ts(2))  # < 124 and exactly -8.00%
    assert signal is not None
    assert signal.exit_reason is ExitReason.MOMENTUM_AND_TRAIL
    assert strategy._exit.momentum_fired is True
    assert strategy._exit.trail_fired is True


def test_the_best_close_is_monotonic_and_never_rebased_downward():
    """Spec 10.2: "The best close is monotonic and never rebased downward"."""
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, SimpleNamespace())  # type: ignore[arg-type]
    for close in (104.0, 101.0, 102.0):
        strategy.on_option_candle(_premium(close, low_pad=20.0), _ts(1))
    assert strategy._exit.extreme_close == 104.0


# ---------------------------------------------------------- 18.4 premium gaps
def test_first_post_gap_premium_candle_cannot_trigger_momentum():
    """Spec 10.4: the first completed post-gap candle's "previous candle" sits on the
    far side of the hole, so that one comparison is suppressed."""
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, SimpleNamespace())  # type: ignore[arg-type]
    strategy.on_option_candle(_premium(100.0, low_pad=5.0), _ts(1))  # low 95
    strategy.on_option_candle_gap()
    assert strategy.on_option_candle(_premium(90.0, low_pad=5.0), _ts(3)) is None


def test_momentum_resumes_on_the_second_consecutive_post_gap_candle():
    """Spec 18.4: "Second consecutive post-gap candle meets rule | Exit permitted"."""
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, SimpleNamespace())  # type: ignore[arg-type]
    strategy.on_option_candle(_premium(100.0, low_pad=5.0), _ts(1))
    strategy.on_option_candle_gap()
    strategy.on_option_candle(_premium(90.0, low_pad=5.0), _ts(3))  # low 85, suppressed
    signal = strategy.on_option_candle(_premium(84.0, low_pad=5.0), _ts(4))
    assert signal is not None
    assert signal.exit_reason is ExitReason.MOMENTUM_LOW


def test_trail_state_survives_a_premium_gap():
    """Spec 18.4: "Premium gap occurs after trail activation | Trail/best-close state
    preserved". A data hole does not undo favourable progress already made."""
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, SimpleNamespace())  # type: ignore[arg-type]
    strategy.on_option_candle(_premium(125.0, low_pad=40.0), _ts(1))
    assert strategy._exit.trail_activated is True
    strategy.on_option_candle_gap()
    assert strategy._exit.trail_activated is True
    assert strategy._exit.extreme_close == 125.0
    # The trail leg is NOT suppressed by the gap — only momentum is.
    signal = strategy.on_option_candle(_premium(115.0, low_pad=40.0), _ts(3))
    assert signal is not None
    assert signal.exit_reason is ExitReason.HIGHEST_CLOSE_TRAIL


# --------------------------------------------------- 10.3 per-position reset
def test_position_close_clears_all_premium_exit_state():
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, SimpleNamespace())  # type: ignore[arg-type]
    strategy.on_option_candle(_premium(104.0, low_pad=20.0), _ts(1))
    _close_trade(strategy, position, exit_price=104.0)
    assert strategy._exit.extreme_close is None
    assert strategy._exit.trail_activated is False
    assert strategy._exit.highest_close is None
    assert strategy._prev_premium_candle is None
    assert strategy._last_position is None


def test_a_replacement_position_starts_with_independent_exit_state():
    """Spec 10.3: "On a reversal, the replacement position starts with independent
    exit state." The first trade's 104 peak must not arm the second trade's trail."""
    strategy = _strategy()
    first = _open_position(entry=100.0, security_id="CE1")
    strategy.on_position_tick(first, SimpleNamespace())  # type: ignore[arg-type]
    strategy.on_option_candle(_premium(104.0, low_pad=20.0), _ts(1))
    _close_trade(strategy, first, exit_price=104.0, exit_reason=ExitReason.OPPOSITE_SIGNAL)

    second = _open_position(entry=200.0, security_id="PE1")
    strategy.on_position_tick(second, SimpleNamespace())  # type: ignore[arg-type]
    assert strategy.on_option_candle(_premium(200.0, low_pad=20.0), _ts(2)) is None
    assert strategy._exit.extreme_close == 200.0
    assert strategy._exit.trail_activated is False


def test_position_close_applies_exit_analytics_to_the_trade():
    """Every close is annotated, including square-off and daily-cap exits, so a report
    can tell "mode active, rule did not trigger" from "mode not used"."""
    strategy = _strategy()
    position = _open_position(entry=100.0)
    strategy.on_position_tick(position, SimpleNamespace())  # type: ignore[arg-type]
    strategy.on_option_candle(_premium(104.0, low_pad=20.0), _ts(1))
    trade = _close_trade(strategy, position, exit_price=101.0, exit_reason=ExitReason.SQUARE_OFF)
    assert trade.exit_mode == "MOMENTUM_LOW_OR_HIGHEST_CLOSE"
    assert trade.highest_completed_close == 104.0
    assert trade.best_favourable_close == 104.0
    assert trade.trail_activated is True
    assert trade.trail_triggered is False
    assert trade.candle_structure_triggered is False


# ------------------------------------------------------- 18.5 restart plumbing
def test_exit_state_snapshot_and_restore_round_trip():
    """Spec section 11: entry-relevant trail state must survive a restart."""
    source = _strategy()
    position = _open_position(entry=100.0)
    source.on_position_tick(position, SimpleNamespace())  # type: ignore[arg-type]
    source.on_option_candle(_premium(104.0, low_pad=20.0), _ts(1))
    snapshot = source.exit_state_snapshot()
    assert snapshot["highest_close"] == 104.0
    assert snapshot["trail"] == {"extreme": 104.0, "activated": True}

    restored = _strategy()
    restored.restore_exit_state(snapshot)
    restored.on_position_tick(position, SimpleNamespace())  # type: ignore[arg-type]
    assert restored._exit.trail_activated is True
    assert restored._exit.extreme_close == 104.0


def test_the_momentum_comparison_is_re_primed_after_a_restart():
    """Spec section 11: the previous-premium-candle reference is deliberately not
    snapshotted — it belongs to the process that built it. The restored strategy must
    therefore re-prime rather than compare across the process gap."""
    restored = _strategy()
    restored.restore_exit_state(
        {"highest_close": 125.0, "trail": {"extreme": 125.0, "activated": True}}
    )
    position = _open_position(entry=100.0)
    restored.on_position_tick(position, SimpleNamespace())  # type: ignore[arg-type]
    assert restored._prev_premium_candle is None
    # A deep first post-restart candle cannot fire momentum (no previous candle), but
    # the restored trail is still armed and still measured from the restored extreme.
    signal = restored.on_option_candle(_premium(115.0, low_pad=40.0), _ts(1))
    assert signal is not None
    assert signal.exit_reason is ExitReason.HIGHEST_CLOSE_TRAIL


def test_restore_with_foreign_or_empty_data_does_not_raise():
    for payload in ({}, {"highest_close": "nonsense"}, {"trail": "nope"}, {"unknown": 1}):
        strategy = _strategy()
        strategy.restore_exit_state(payload)  # type: ignore[arg-type]


# ------------------------------------------------------------------ properties
def test_option_selection_is_atm_zero_steps():
    selection = SupertrendBuy1x1p2Strategy().option_selection
    assert selection.moneyness is Moneyness.ATM
    assert selection.steps == 0


def test_trade_side_is_always_buy():
    assert SupertrendBuy1x1p2Strategy().trade_side is OrderSide.BUY


def test_risk_manager_is_hard_stop_and_disabled_by_default():
    """The legacy config selected ``sl_lock_trail`` with all four thresholds null —
    a per-tick rupee rule that never fires. ``hard_stop`` with its own disabled
    default is the behavioural equivalent here."""
    strategy = SupertrendBuy1x1p2Strategy()
    strategy.risk_manager.new_position(10, entry_price=100.0)
    assert strategy.risk_manager.on_pnl(-1_000_000.0) is None


def test_the_strategy_never_names_a_lot_size():
    """Spec section 8: quantity is ``lots_per_trade`` only; the exchange lot size is
    resolved at runtime and multiplied in by the engine."""
    source = __import__(
        "inspect"
    ).getsource(SupertrendBuy1x1p2Strategy)
    assert "lot_size" not in source
