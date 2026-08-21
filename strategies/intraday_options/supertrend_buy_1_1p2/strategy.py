"""``supertrend_buy_1_1p2`` — NIFTY 5-minute SuperTrend(period 1, multiplier 1.2),
ATM weekly options, BUY-only, intraday.

Full functional/design spec: ``SUPERTREND_BUY_1_1P2_ALGO_TRADING_SPEC.md`` in this
directory. Read that before touching this file — it, and the legacy
``supertrend_fast`` code/config/tests it was extracted from, are the authoritative
source of the behaviour here, not this docstring.

Parity note (settled before implementation): the legacy tree also contains
``md_docs/NiftyFixedStrikeSuperTrend_Master_Specification.md``, which describes a
*different* strategy — one strike fixed at 09:16 for the whole day, SuperTrend run on
the CE and PE **premium** charts, multiplier 1, and two simultaneous positions. It is
deliberately **not** used as a parity source. The legacy ``supertrend_fast``
``strategy.py`` / ``config/config.yaml`` / ``tests/test_strategy.py`` are.

What this file deliberately does **not** implement, because the engine already does
-------------------------------------------------------------------------------------
* **Entry-window / entry-cutoff / mandatory square-off timing.**
  :class:`~common.engine.session.MarketSession` and :mod:`common.engine.square_off`
  own it. This strategy emits an ``ENTER`` signal purely off a fresh SuperTrend flip;
  ``TradingEngine._on_candle_close``'s own ``session.can_enter()`` gate is what keeps
  a signal from opening a position outside 09:15-15:15, and the spec's
  "an opposite flip after the cutoff closes but must not re-open" rule falls out of
  that same gate for free — the exit half of a reversal always runs, only the
  re-entry half is gated.
* **One-position-at-a-time / close-before-open reversal.**
  ``TradingEngine._on_candle_close`` already treats an ``ENTER`` naming a different
  leg than the one currently open as "close it, then enter the new one", and an
  ``ENTER`` naming the *same* leg as a no-op. It also fails closed on an unresolved
  close: ``PositionManager.close`` raises when the position is gone, and
  ``LifecycleGateway`` raises ``GatewayExecutionError`` unless a genuine fill exists,
  so a failed or unknown close can never be followed by a replacement entry. This
  strategy therefore always emits a plain ``ENTER`` on a fresh flip — never an
  explicit exit-then-enter pair and never a same-leg dedupe of its own.
* **The daily 3% live-MTM loss cap.** ``TradingEngine._build_daily_guard`` /
  ``_on_option_tick`` already construct and drive ``DailyRiskGuard.check_open_mtm()``
  / ``register_trade()`` from ``EngineConfig.starting_capital`` /
  ``max_daily_loss_percent`` — configuration, not strategy code.
* **Lot-size/expiry resolution and order quantity.**
  :class:`~common.engine.selection.OptionSelector` plus
  :mod:`common.market_data.scrip_master` resolve the ATM weekly contract's real
  (exchange-driven) lot size at runtime, and ``OpenPosition.quantity = lots *
  contract.lot_size`` applies it. This strategy only ever names ``lots_per_trade``
  (a sizing choice) and never touches ``lot_size``.
"""

from __future__ import annotations

from dataclasses import replace
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
from common.indicators.supertrend import UPTREND, SuperTrend
from common.models import Tick
from common.warmup.requirements import StrategyWarmupSpec

#: Minimum completed 5-minute buckets a warm-up replay must cover before this
#: strategy's trend context may be trusted (spec section 7).
#:
#: **Not** ``SuperTrend.warmup_requirement().min_bars``, which is ``period`` — i.e.
#: ``1`` here. That number describes only when the *ATR* has a value; it says nothing
#: about reconstructing a trustworthy pre-existing trend. SuperTrend's direction is
#: latched and path-dependent, so a replay of one recent candle would seed a direction
#: outright and could then read the first live crossing as a fresh flip that never
#: happened — or hold the opposite direction and swallow a real one. The bar count
#: therefore has to express "at least a full session of continuous history", which is
#: what this constant does; ``continuity_required`` (inherited from the indicator)
#: remains the categorical gate on top of it.
#:
#: Sizing: the canonical 5-minute grid for a 09:15-15:20 session holds **73** full
#: buckets (09:15 through the 15:15-15:20 bar) — ``common.warmup.session_buckets.
#: session_bucket_count``. 75 is therefore deliberately *more* than one session, so
#: the required suffix always spans two trading sessions and a start right at the open
#: cannot be satisfied by a partial day. ``WarmupManager`` walks the extra sessions
#: through ``MarketSession.prior_trading_day``, so weekends and configured holidays
#: are legitimate gaps rather than false ones.
DEFAULT_WARMUP_MIN_BARS = 75


def _pick(explicit: Any, params: dict[str, Any], key: str, default: Any) -> Any:
    """Prefer an explicit constructor kwarg, then ``cfg.parameters[key]``, then default.

    Lets this strategy be constructed either the real runtime's way — a dotted
    ``strategy_ref`` plus keyword arguments
    (``runtimes.intraday_options.engine_worker.load_strategy``) — or the registry's way
    (``common.engine.strategy.get_strategy(name, cfg)``, reading ``cfg.parameters``) —
    without maintaining two parallel construction paths. Same helper, same reasoning,
    as ``ema_cross_9_21_buy``.
    """
    return explicit if explicit is not None else params.get(key, default)


@register_strategy("supertrend_buy_1_1p2")
class SupertrendBuy1x1p2Strategy(BaseStrategy):
    """NIFTY 5m SuperTrend(1, 1.2) fresh flip -> ATM weekly CE/PE, BUY-only."""

    name = "supertrend_buy_1_1p2"
    #: For a UI that wants a human label; not read by the engine.
    display_name = "SuperTrend Buy 1/1.2 — NIFTY ATM Option"

    def __init__(
        self,
        cfg: Any = None,
        *,
        supertrend_period: int | None = None,
        supertrend_multiplier: float | None = None,
        lots_per_trade: int | None = None,
        trail_percentage: float | None = None,
        activation_minimum_favourable_move_percentage: float | None = None,
        warmup_min_bars: int | None = None,
        risk_manager_name: str | None = None,
        risk_manager_params: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(cfg)
        p = self.params

        # Spec section 6: the legacy parameters, shipped as the defaults. Changing
        # them is a strategy-spec change, not an implementation detail.
        period = int(_pick(supertrend_period, p, "supertrend_period", 1))
        multiplier = float(_pick(supertrend_multiplier, p, "supertrend_multiplier", 1.2))
        # SuperTrend itself refuses period < 1; multiplier <= 0 would make both bands
        # collapse onto hl2 and every bar a flip, which is not a configuration this
        # strategy can trade, so it is refused here rather than silently accepted.
        if multiplier <= 0:
            raise ValueError(f"supertrend_multiplier must be > 0 (got {multiplier})")
        self._supertrend = SuperTrend(period=period, multiplier=multiplier)

        self._warmup_min_bars = int(
            _pick(warmup_min_bars, p, "warmup_min_bars", DEFAULT_WARMUP_MIN_BARS)
        )
        if self._warmup_min_bars < 1:
            raise ValueError("warmup_min_bars must be >= 1")

        self._lots_per_trade = int(_pick(lots_per_trade, p, "lots_per_trade", 10))
        if self._lots_per_trade < 1:
            raise ValueError("lots_per_trade must be >= 1")

        # Spec section 10: 4% activation gates only the trailing leg; the momentum
        # structure leg has no activation gate and can fire from the second completed
        # premium candle after entry. CombinedCandleExit already implements exactly
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

        # The legacy config selected `sl_lock_trail` with every one of its four
        # thresholds null — i.e. no per-tick rupee rule at all. This repository ships
        # one risk manager, `hard_stop`, and its own disabled default
        # (catastrophic_stop_rupees_per_lot: none) is behaviourally identical: a
        # backstop that never fires. Same choice ema_cross_9_21_buy made.
        risk_name = str(_pick(risk_manager_name, p, "risk_manager_name", "hard_stop"))
        risk_params = dict(_pick(risk_manager_params, p, "risk_manager_params", {}) or {})
        self._risk_manager: RiskManager = get_risk_manager(risk_name, risk_params)

        # Per-position premium-exit bookkeeping. Cleared on every close
        # (on_position_closed) and never inherited by the next entry — spec section
        # 10.3 "Reset all exit state on a genuinely new position".
        self._prev_premium_candle: OHLC | None = None
        self._last_position: OpenPosition | None = None
        self._candles_seen = 0
        # Spec section 7: only a verified-complete (WARMED) replay grants trusted
        # trend context, and only trusted context permits an entry. Starts False so a
        # strategy that is driven without ever calling on_warmup_complete cannot
        # trade on a cold seed.
        self._context_trusted = False

    # ------------------------------------------------------------- lifecycle
    def reset(self) -> None:
        """Start of each trading day (``TradingEngine._start_day``).

        Everything is cleared, **including the SuperTrend itself** — unlike
        ``ema_cross_9_21_buy``, which keeps its EMAs across the day boundary. The
        difference is deliberate and follows from what the two indicators are: an EMA
        converges exponentially, so yesterday's value is a useful head start; a
        SuperTrend's direction is *latched*, so carrying a stale one into a day whose
        own warm-up has not run yet is precisely the state that manufactures a false
        flip. ``_warm_up()`` runs immediately after this and re-seeds the indicator
        from verified history, which is the only seed this strategy trusts. This
        matches the legacy ``supertrend_fast``'s own ``reset()``, which likewise
        called ``self._st.reset()``.

        ``_context_trusted`` is cleared here too, so trust can never leak across days
        or runs (spec section 7's last bullet).
        """
        self._supertrend.reset()
        self._candles_seen = 0
        self._context_trusted = False
        self._reset_exit_state()

    def _reset_exit_state(self) -> None:
        self._exit.reset()
        self._prev_premium_candle = None
        self._last_position = None

    def on_warmup_complete(self, *, context_trusted: bool = False) -> None:
        """Record whether the replay just fed through :meth:`on_candle` is trustworthy.

        Spec section 7: only ``WARMED`` grants trusted context and permits entries.
        ``context_trusted`` is computed by ``TradingEngine._warm_up`` from whether the
        replay it ran was verified complete, current, ordered, duplicate-free and
        gap-free against the trading calendar.

        Deliberately does **not** reset the SuperTrend on an untrusted replay. Clearing
        a latched indicator does not make it safe — it makes the *next* candle a fresh
        seed, and the candle after that a fabricated "flip". The correct fail-closed
        action is to keep the state and withhold entries, which is what
        :meth:`on_candle` does while this is ``False``. (The engine independently
        latches entries off for the whole day in this case, because
        ``StrategyWarmupSpec.entry_blocked_by`` returns True for every non-``WARMED``
        status when ``continuity_required`` is set. This flag is the strategy-side
        half of the same rule, so the strategy is safe even when driven directly.)

        Default ``False`` (fail-conservative): an omitted argument behaves exactly like
        an untrusted replay. The engine's production call site always passes an
        explicit computed value.
        """
        self._context_trusted = bool(context_trusted)

    def status(self) -> str:
        try:
            state = self._supertrend.state
            trend = "UP" if state.trend == UPTREND else "DOWN"
            line = f"{state.line:.2f}"
        except RuntimeError:
            trend, line = "none", "none"
        return (
            f"candles={self._candles_seen} "
            f"st({self._supertrend.period},{self._supertrend.multiplier})={trend} "
            f"line={line} trusted={self._context_trusted}"
        )

    def warmup_spec(self) -> StrategyWarmupSpec | None:
        """Spec section 7 — a full session of continuous history, not one ATR bar.

        Starts from the indicator's own declared requirement (which is where
        ``continuity_required=True`` comes from, and must keep coming from — this
        strategy does not restate the indicator's categorical contract) and raises
        only the bar floor to :data:`DEFAULT_WARMUP_MIN_BARS`. See that constant for
        why ``min_bars = period = 1`` is not a safe warm-up requirement even though it
        is a correct *ATR readiness* requirement.

        ``max`` rather than an outright override, so a future indicator change that
        raised the component's own floor above this one would win rather than be
        silently capped.
        """
        base = StrategyWarmupSpec.from_indicators([self._supertrend])
        return replace(base, min_bars=max(base.min_bars, self._warmup_min_bars))

    # ---------------------------------------------------------------- entries
    def on_candle(self, candle: OHLC, timestamp: datetime) -> StrategySignal | None:
        """One completed 5-minute NIFTY candle. Spec sections 4-6.

        Called for warm-up replay candles too (``TradingEngine._warm_up``'s sink),
        which is exactly how the indicator gets seeded — the sink discards whatever
        this returns, so a historical flip can never place an order.
        """
        self._candles_seen += 1
        state = self._supertrend.update(candle)

        if not state.flipped:
            # Covers both "the seeding candle" (``flipped`` is False when no previous
            # trend existed) and "the trend continued". Spec section 6: the initial
            # direction is context only and is never itself an entry, and repeated
            # candles in the same trend produce no entry.
            return None

        if not self._context_trusted:
            # Spec section 7: a flip measured against an untrusted seed is not a
            # signal, it is an artefact of the seed. Entries are withheld; exits,
            # daily-risk and square-off are unaffected because they never consult
            # this path.
            return None

        option_type = OptionType.CE if state.trend == UPTREND else OptionType.PE
        trend_label = "UP" if state.trend == UPTREND else "DOWN"
        return StrategySignal(
            action=SignalAction.ENTER,
            timestamp=timestamp,
            option_type=option_type,
            side=OrderSide.BUY,
            reason=(
                f"SuperTrend({self._supertrend.period},{self._supertrend.multiplier}) "
                f"flipped to {trend_label} (line={state.line:.2f})"
            ),
        )

    # ----------------------------------------------------------- premium exit
    @property
    def needs_option_candles(self) -> bool:
        return True

    def on_option_candle(self, candle: OHLC, timestamp: datetime) -> StrategySignal | None:
        """Spec section 10 — exits are evaluated on the *traded option's own*
        completed 5-minute premium candles, never on the NIFTY candle.
        """
        if self._last_position is None:
            # No option tick has reached on_position_tick for this contract yet (the
            # entry fill itself can race the first premium candle close) — nothing to
            # evaluate the exit against.
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
        """Spec section 10.4: forget the momentum leg's previous-candle reference for
        exactly the next premium candle; the best-close trail and its activation flag
        survive untouched (:meth:`~common.exit.combined_candle_exit.CombinedCandleExit.
        on_gap` already keeps them).
        """
        self._exit.on_gap()
        self._prev_premium_candle = None

    def on_position_tick(self, position: OpenPosition, tick: Tick) -> StrategySignal | None:
        self._last_position = position
        return None

    def on_position_closed(self, trade: Trade) -> None:
        """Every actual close — premium exit, reversal, daily-cap square-off, or the
        mandatory 15:20 square-off — clears all premium-exit state so the next
        position starts clean (spec section 10.3).
        """
        self._exit.apply_to_trade(trade)
        self._reset_exit_state()

    # --------------------------------------------------------- restart state
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
