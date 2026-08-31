"""``c921_ema_cross_buy`` — NIFTY 5-minute EMA(9)/EMA(21) crossover, ATM weekly
options, BUY-only, intraday. Phase 9's first real strategy.

Full functional/design spec: ``c921_ema_cross_buy_spec.md`` in this directory.
Read that before touching this file — it is the authoritative source of the
behaviour here, not this docstring.

What this file deliberately does **not** implement, because the engine already does
-------------------------------------------------------------------------------------
* **Entry-window / entry-cutoff / mandatory square-off timing.**
  :class:`~common.engine.session.MarketSession` and
  :mod:`common.engine.square_off` own it. This strategy emits an ``ENTER``
  signal purely off a fresh crossover; :meth:`~common.engine.engine.
  TradingEngine._on_candle_close`'s own ``session.can_enter()`` gate is what
  keeps a signal from opening a position outside 09:15-14:45, and the
  reversal-after-cutoff "close only, never re-open" rule (spec section 9)
  falls out of that same gate for free — the exit half of a reversal always
  runs; only the re-entry half is gated on ``can_enter()``.
* **One-position-at-a-time / reversal.** ``TradingEngine._on_candle_close``
  already treats an ``ENTER`` naming a different leg than the one currently
  open as "close it, then enter the new one" and an ``ENTER`` naming the
  *same* leg as a no-op. This strategy therefore always emits a plain
  ``ENTER`` on a fresh crossover — never an explicit exit-then-enter pair or a
  same-leg dedupe.
* **The daily 3% live-MTM loss cap.**
  ``TradingEngine._build_daily_guard``/``_on_option_tick`` already construct
  and drive ``DailyRiskGuard.check_open_mtm()``/``register_trade()`` from
  ``EngineConfig.starting_capital``/``max_daily_loss_percent`` — configuration
  (see ``config/strategies/c921_ema_cross_buy.yaml``), not strategy code.
* **Lot-size/expiry resolution and order quantity.**
  :class:`~common.engine.selection.OptionSelector` plus
  :mod:`common.market_data.scrip_master` already resolve the ATM weekly
  contract's real (exchange-driven) lot size at runtime, and
  ``OpenPosition.quantity = lots * contract.lot_size`` already applies it.
  This strategy only ever names ``lots_per_trade`` (a sizing choice) and never
  touches ``lot_size``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from common.engine.models import (
    Moneyness,
    OpenPosition,
    OptionSelection,
    OptionType,
    OrderSide,
    SignalAction,
    StrategySignal,
    Trade,
)
from common.engine.risk import RiskManager, get_risk_manager
from common.engine.strategy import BaseStrategy, register_strategy
from common.exit.combined_candle_exit import CombinedCandleExit
from common.indicators.base import OHLC
from common.indicators.ema import EMA, ConfirmedCrossover
from common.models import Tick
from common.warmup.requirements import StrategyWarmupSpec


def _pick(explicit: Any, params: dict[str, Any], key: str, default: Any) -> Any:
    """Prefer an explicit constructor kwarg, then ``cfg.parameters[key]``, then default.

    Lets this strategy be constructed either the real runtime's way — a
    dotted ``strategy_ref`` plus keyword arguments
    (``runtimes.intraday_options.engine_worker.load_strategy``, the shape
    every keyword-only engine-contract strategy in this repository uses, e.g.
    ``EngineFixtureStrategy``) — or the registry's way
    (``common.engine.strategy.get_strategy(name, cfg)``, reading
    ``cfg.parameters``) — without maintaining two parallel construction paths.
    """
    return explicit if explicit is not None else params.get(key, default)


@register_strategy("c921_ema_cross_buy")
class EmaCross9x21BuyStrategy(BaseStrategy):
    """NIFTY 5m EMA(9)/EMA(21) crossover -> ATM weekly CE/PE, BUY-only."""

    name = "c921_ema_cross_buy"
    #: For a UI that wants a human label; not read by the engine.
    display_name = "EMA Cross (9/21) Buy"

    def __init__(
        self,
        cfg: Any = None,
        *,
        ema_fast: int | None = None,
        ema_slow: int | None = None,
        minimum_separation: float | None = None,
        confirmation_candles: int | None = None,
        lots_per_trade: int | None = None,
        trail_percentage: float | None = None,
        activation_minimum_favourable_move_percentage: float | None = None,
        risk_manager_name: str | None = None,
        risk_manager_params: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(cfg)
        p = self.params

        fast = int(_pick(ema_fast, p, "ema_fast", 9))
        slow = int(_pick(ema_slow, p, "ema_slow", 21))
        if slow <= fast:
            raise ValueError(f"ema_slow ({slow}) must be greater than ema_fast ({fast})")
        self._ema_fast = EMA(fast)
        self._ema_slow = EMA(slow)

        # Spec section 12.6, settled by the Phase 9 brief: a pure closed-candle
        # sign-flip cross, confirmed on the closing candle of the flip itself.
        min_sep = float(_pick(minimum_separation, p, "minimum_separation", 0.0))
        confirm = int(_pick(confirmation_candles, p, "confirmation_candles", 1))
        self._crossover = ConfirmedCrossover(min_sep, confirm)

        self._lots_per_trade = int(_pick(lots_per_trade, p, "lots_per_trade", 1))
        if self._lots_per_trade < 1:
            raise ValueError("lots_per_trade must be >= 1")

        # Spec section 6.1: 4% activation gates only the trailing leg; the
        # momentum-break leg has no activation gate (settled by the Phase 9
        # brief's section 12.4 answer: "the momentum structure leg remains
        # active before +4%"). CombinedCandleExit already implements exactly
        # that split — nothing extra to wire here.
        trail_pct = float(_pick(trail_percentage, p, "trail_percentage", 8.0))
        activation_pct = float(
            _pick(
                activation_minimum_favourable_move_percentage,
                p,
                "activation_minimum_favourable_move_percentage",
                4.0,
            )
        )
        self._exit = CombinedCandleExit(
            {
                "trail_percentage": trail_pct,
                "activation": {
                    "enabled": True,
                    "minimum_favourable_move_percentage": activation_pct,
                },
            }
        )

        # Spec section 7 / 12.5: minimal, disabled-by-default backstop. See
        # common.engine.risk_managers.HardStopRiskManager for why this is a
        # per-lot rupee floor rather than a percentage of premium.
        risk_name = str(_pick(risk_manager_name, p, "risk_manager_name", "hard_stop"))
        risk_params = dict(_pick(risk_manager_params, p, "risk_manager_params", {}) or {})
        self._risk_manager: RiskManager = get_risk_manager(risk_name, risk_params)

        # Per-position premium-exit bookkeeping. Cleared on every close
        # (on_position_closed) and never inherited by the next entry — spec
        # section 6.1 "Per-trade state reset".
        self._prev_premium_candle: OHLC | None = None
        self._last_position: OpenPosition | None = None
        self._candles_seen = 0

    # ------------------------------------------------------------- lifecycle
    def reset(self) -> None:
        """Start of each trading day (``TradingEngine._start_day``).

        Clears the crossover detector's stale in-memory state — left over
        from the previous process/day — but deliberately *not* the EMAs:
        they are ``SESSION_SPANNING`` (:meth:`warmup_spec`) and carry state
        across the overnight gap on purpose. Warm-up replay (which runs
        after this, through :meth:`on_candle` like any other candle)
        reconstructs the crossover's context; :meth:`on_warmup_complete`
        then decides whether that reconstruction is trustworthy enough to
        keep as today's context (spec section 4.3/4.4/11) — it is not
        cleared unconditionally a second time.
        """
        self._crossover.reset()
        self._candles_seen = 0
        self._reset_exit_state()

    def _reset_exit_state(self) -> None:
        self._exit.reset()
        self._prev_premium_candle = None
        self._last_position = None

    def on_warmup_complete(self, *, context_trusted: bool = False) -> None:
        """Warm-up (if any ran) has fed today's opening EMA state through
        :meth:`on_candle`, which may have left the crossover detector
        confirmed on some relationship — yesterday's close, or today's own
        candles replayed so far. Rev 3 (spec section 4.3/4.4): that
        relationship is *preserved* as crossover context when it is
        trustworthy (``context_trusted``, computed by
        :meth:`~common.engine.engine.TradingEngine._warm_up` from whether the
        replay it ran, if any, was verified-complete) — a subsequent
        current-day closed candle can then flip it into a real entry. It is
        *cleared* when it is not (no usable warm-up, or a
        partial/stale/failed/skipped replay), so the detector's own "first
        observation after a reset is context-only" rule re-establishes
        context safely from the first live candle instead of trusting an
        unverified one. EMAs are untouched either way.

        Default ``False`` (fail-conservative): an omitted argument clears
        the detector, exactly like an untrusted replay would. The engine's
        production call site always passes an explicit computed value.
        """
        if not context_trusted:
            self._crossover.reset()

    def status(self) -> str:
        def _value(ema: EMA) -> float | None:
            try:
                return round(ema.state.value, 2)
            except RuntimeError:
                return None

        return (
            f"candles={self._candles_seen} "
            f"ema{self._ema_fast.period}={_value(self._ema_fast)} "
            f"ema{self._ema_slow.period}={_value(self._ema_slow)} "
            f"confirmed={self._crossover.confirmed_label}"
        )

    def warmup_spec(self) -> StrategyWarmupSpec | None:
        return StrategyWarmupSpec.from_indicators([self._ema_fast, self._ema_slow])

    # ---------------------------------------------------------------- entries
    def on_candle(self, candle: OHLC, timestamp: datetime) -> StrategySignal | None:
        self._candles_seen += 1
        self._ema_fast.update(candle)
        self._ema_slow.update(candle)
        if not self._ema_slow.is_ready:
            # Section 4.4: no entry until the 21-EMA is genuinely ready (>= 21
            # closed candles) on a cold start; warmed, this is true from the
            # first live candle. Either way the crossover detector is simply
            # not fed until both EMAs hold a real value, so it cannot
            # establish context on a partial seed.
            return None

        spread = self._ema_fast.state.value - self._ema_slow.state.value
        side = self._crossover.update(spread)
        if side is None:
            return None

        option_type = OptionType.CE if side == 1 else OptionType.PE
        direction = "bullish" if side == 1 else "bearish"
        return StrategySignal(
            action=SignalAction.ENTER,
            timestamp=timestamp,
            option_type=option_type,
            side=OrderSide.BUY,
            reason=(
                f"fresh intraday {direction} EMA{self._ema_fast.period}/"
                f"{self._ema_slow.period} crossover"
            ),
        )

    # ----------------------------------------------------------- premium exit
    @property
    def needs_option_candles(self) -> bool:
        return True

    def on_option_candle(self, candle: OHLC, timestamp: datetime) -> StrategySignal | None:
        if self._last_position is None:
            # No option tick has reached on_position_tick for this contract yet
            # (the entry fill itself can race the first premium candle close)
            # -- nothing to evaluate the exit against.
            return None
        history = [self._prev_premium_candle] if self._prev_premium_candle is not None else []
        fired = self._exit.should_exit(
            self._last_position, candle, history, {}, None, timestamp=timestamp
        )
        self._prev_premium_candle = candle
        if not fired:
            return None
        return StrategySignal(
            action=SignalAction.EXIT,
            timestamp=timestamp,
            exit_reason=self._exit.exit_reason,
            reason=f"premium exit ({self._exit.label}): {self._exit.reason}",
        )

    def on_option_candle_gap(self) -> None:
        """Spec section 6.1 "Premium candle-gap behaviour": forget the momentum
        leg's previous-candle reference for exactly the next premium candle;
        the best-close trail survives untouched
        (:meth:`~common.exit.combined_candle_exit.CombinedCandleExit.on_gap`
        already keeps it).
        """
        self._exit.on_gap()
        self._prev_premium_candle = None

    def on_position_tick(self, position: OpenPosition, tick: Tick) -> StrategySignal | None:
        self._last_position = position
        return None

    def on_position_closed(self, trade: Trade) -> None:
        """Every actual close — premium exit, reversal, daily-cap square-off,
        or the mandatory 15:15 square-off — clears all premium-exit state so
        trade N+1 starts clean (spec section 6.1 "Per-trade state reset").
        """
        self._exit.apply_to_trade(trade)
        self._reset_exit_state()

    # --------------------------------------------- Phase 6 Part 2: restart state
    def exit_state_snapshot(self) -> dict[str, Any]:
        return self._exit.snapshot()

    def restore_exit_state(self, data: dict[str, Any]) -> None:
        self._exit.restore(data)

    # ------------------------------------------------------------- properties
    @property
    def option_selection(self) -> OptionSelection:
        return OptionSelection(moneyness=Moneyness.ATM, steps=0)

    @property
    def trade_side(self) -> OrderSide:
        return OrderSide.BUY

    @property
    def quantity_lots(self) -> int:
        return self._lots_per_trade

    @property
    def risk_manager(self) -> RiskManager:
        return self._risk_manager
