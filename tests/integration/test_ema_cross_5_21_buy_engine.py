"""``ema_cross_5_21_buy`` driven by a real, fully-constructed ``TradingEngine``
over a simulated tape — the third real strategy, a faithful clone of
``ema_cross_9_21_buy`` (Phase 9) with only the fast EMA period (5 instead of
9) and identity changed; the slow period (21) is unchanged. No
monkeypatching, no ``__new__`` shortcuts — mirrors
``tests/integration/test_engine_premium_candle_exit.py``'s discipline.

These prove exactly what the strategy does **not** implement itself (spec
section 9, 8, 3, 6.4): reversal semantics, the entry window/cutoff, the
mandatory square-off, lot-size-driven order quantity and the daily live-MTM
loss cap are all engine-owned, and this is where that is demonstrated end to
end. Everything about the crossover/premium-exit *logic itself* is proven at
the unit level in ``tests/unit/test_ema_cross_5_21_buy_strategy.py``.

``ema_fast=2, ema_slow=3`` (rather than the production 5/21 defaults) keeps
every tape to a handful of candles; every candle sequence and its resulting
signal was verified against the real ``EMA``/``ConfirmedCrossover`` classes
before being hard-coded here (not hand-derived). Because that override (not
the 5/21 production defaults) drives every tape below, the tapes themselves
are identical to the sibling ``test_ema_cross_9_21_buy_engine.py``/
``test_ema_cross_5_9_buy_engine.py`` suites' — only the strategy class/id
differs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from common.config.models import ExecutionMode, StrategyConfig, effective_live_gate
from common.engine.config import EngineConfig, SessionConfig
from common.engine.daily_guard import DailyRiskRecovery
from common.engine.engine import TradingEngine
from common.engine.feed import SimulatedFeed
from common.engine.models import (
    AdoptedPosition,
    ExitReason,
    OptionContract,
    OptionSelection,
    OptionType,
    OrderSide,
)
from common.engine.positions import InMemoryGateway, PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.models import Candle, Tick
from strategies.intraday_options.ema_cross_5_21_buy.strategy import EmaCross5x21BuyStrategy

IST = ZoneInfo("Asia/Kolkata")
UNDERLYING = "INDEX"
LOT_SIZE = 75
PE_CONTRACT = "SIM:NIFTY:WEEKLY:100:PE"
CE_CONTRACT = "SIM:NIFTY:WEEKLY:100:CE"


def _dt(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 8, 7, h, m, s, tzinfo=IST)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id, instrument=security_id, last_price=price,
        exchange_time=ts, received_at=ts,
    )


def _underlying_ticks(closes: Sequence[float], *, start: datetime) -> list[Tick]:
    """One tick per 5-minute bucket, plus a trailing tick to close the last
    one — the same shape ``test_engine_premium_candle_exit.py``'s
    ``_entry_tape``/``_premium_tape`` use. Candle ``i``'s signal is produced
    when this list's ``i + 1``-th tick is delivered (that is what forces
    bucket ``i`` to close)."""
    ticks = [
        _tick(UNDERLYING, price, start + timedelta(minutes=5 * i + 1))
        for i, price in enumerate(closes)
    ]
    ticks.append(_tick(UNDERLYING, closes[-1], start + timedelta(minutes=5 * len(closes) + 1)))
    return ticks


def _today_history_candles(closes: Sequence[float], *, start: datetime) -> list[Candle]:
    """5-minute ``Candle`` objects for ``TradingEngine(history_provider=...)``
    -- the no-warm-up-manager, today's-own-candles fallback that is the
    actual mid-session-restart mechanism (``TradingEngine._warm_up``)."""
    out = []
    for i, c in enumerate(closes):
        s = start + timedelta(minutes=5 * i)
        out.append(
            Candle(
                security_id=UNDERLYING, instrument=UNDERLYING,
                open=c, high=c, low=c, close=c, volume=0,
                start_at=s, end_at=s + timedelta(minutes=5),
            )
        )
    return out


def _insert_fill(ticks: list[Tick], after_index: int, security_id: str, price: float) -> None:
    """Insert an option tick 10s after ``ticks[after_index]`` to fill a pending
    entry queued by the candle that just closed."""
    anchor = ticks[after_index].exchange_time
    ticks.insert(after_index + 1, _tick(security_id, price, anchor + timedelta(seconds=10)))


def _session(*, start="09:15", end="14:45", square_off="15:15") -> SessionConfig:
    return SessionConfig(
        timezone="Asia/Kolkata", start_time=start, end_time=end, square_off_time=square_off
    )


def _build_engine(
    ticks: Sequence[Tick],
    *,
    strategy: EmaCross5x21BuyStrategy | None = None,
    session: SessionConfig | None = None,
    lots: int | None = None,
    **engine_kwargs,
) -> tuple[TradingEngine, EmaCross5x21BuyStrategy, PositionManager]:
    strategy = strategy or EmaCross5x21BuyStrategy(ema_fast=2, ema_slow=3, lots_per_trade=1)
    # Mirrors the real wiring (runtimes.intraday_options.config_adapter +
    # engine_worker._build): PositionManager's own `lots`, not
    # strategy.quantity_lots, is what actually sizes every order — see
    # config_adapter.py's _build_engine_worker_config docstring for why
    # lots_per_trade has exactly one configured home.
    positions = PositionManager(
        InMemoryGateway(slippage_points=0.0),
        lots=lots if lots is not None else strategy.quantity_lots,
    )
    engine = TradingEngine(
        EngineConfig(
            timeframe="5m",
            session=session or _session(),
            warmup_from_history=True,
            **engine_kwargs,
        ),
        feed=SimulatedFeed(ticks),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=LOT_SIZE), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=UNDERLYING,
    )
    return engine, strategy, positions


# Verified sequence (see module docstring): candle 3 (close=90) fires a fresh
# PE; candle 7 (close=90 again) fires a fresh CE — a PE->CE reversal.
_REVERSAL_CLOSES = [100, 100, 100, 90, 80, 70, 80, 90, 100, 110, 120, 105, 95, 85]


def _reversal_tape() -> list[Tick]:
    ticks = _underlying_ticks(_REVERSAL_CLOSES, start=_dt(9, 15))
    _insert_fill(ticks, 4, PE_CONTRACT, 50.0)   # candle3 (PE) closes on tick index4
    _insert_fill(ticks, 9, CE_CONTRACT, 60.0)   # candle7 (CE) closes on (shifted) tick index9
    _insert_fill(ticks, 15, PE_CONTRACT, 40.0)  # candle12 (PE) closes on (shifted) tick index15
    return ticks


# ------------------------------------------------------- 1. one position at a time
def test_never_more_than_one_open_position_at_any_tick():
    engine, _strategy, positions = _build_engine(_reversal_tape())

    original_open = positions.open

    def _guarded_open(*args, **kwargs):
        assert not positions.has_position(), "a second position was opened while one was live"
        return original_open(*args, **kwargs)

    positions.open = _guarded_open  # type: ignore[method-assign]
    engine.run()
    assert len(positions.trades) >= 2


# ------------------------------------------------------------- 2/3. reversal
def test_pe_to_ce_reversal_closes_the_pe_leg_and_opens_ce():
    engine, _strategy, positions = _build_engine(_reversal_tape())
    engine.run()

    assert len(positions.trades) >= 2
    first, second = positions.trades[0], positions.trades[1]
    assert first.contract.option_type is OptionType.PE
    assert first.exit_reason is ExitReason.OPPOSITE_SIGNAL
    assert second.contract.option_type is OptionType.CE
    assert second.entry_price == 60.0


def test_ce_to_pe_reversal_closes_the_ce_leg_and_reopens_pe():
    engine, _strategy, positions = _build_engine(_reversal_tape())
    engine.run()

    assert len(positions.trades) >= 2
    second = positions.trades[1]
    assert second.contract.option_type is OptionType.CE
    assert second.exit_reason is ExitReason.OPPOSITE_SIGNAL
    # A third leg (PE again) is left open (or already square-off closed) —
    # either way, proving the reversal reopened the opposite side.
    still_open = positions.positions
    third_leg_pe = any(p.contract.option_type is OptionType.PE for p in still_open)
    third_trade_pe = (
        len(positions.trades) >= 3 and positions.trades[2].contract.option_type is OptionType.PE
    )
    assert third_leg_pe or third_trade_pe


# ----------------------------------------------- 4. reversal after cutoff: exit-only
def test_reversal_after_entry_cutoff_closes_but_does_not_reopen():
    """Verified sequence: PE entry fires well before 14:45; the flip to
    bullish fires at 14:56, past the 14:45 cutoff."""
    closes = [100, 100, 100, 90, 80, 70, 70, 70, 70, 70, 90, 100, 110]
    ticks = _underlying_ticks(closes, start=_dt(14, 0))
    _insert_fill(ticks, 4, PE_CONTRACT, 50.0)

    engine, _strategy, positions = _build_engine(ticks)
    engine.run()

    assert len(positions.trades) == 1
    assert positions.trades[0].contract.option_type is OptionType.PE
    assert positions.trades[0].exit_reason is ExitReason.OPPOSITE_SIGNAL
    assert positions.trades[0].exit_time.time() >= _dt(14, 45).time()
    assert not positions.has_position()
    assert engine._pending is None  # the CE side was never even queued


# --------------------------------------------------------------- 5/6. timing
def test_no_entry_before_09_15():
    """Pre-open ticks that would otherwise cross must never even reach a candle."""
    closes = [100, 100, 100, 90]  # would fire a PE if evaluated
    ticks = _underlying_ticks(closes, start=_dt(8, 50))
    engine, strategy, positions = _build_engine(ticks)
    engine.run()

    assert strategy._candles_seen == 0
    assert positions.trades == []
    assert not positions.has_position()


def test_no_entry_after_14_45_when_flat():
    """A fresh cross with nothing open must not enter once the cutoff has passed."""
    closes = [100, 100, 100, 90]  # would fire a PE if within the window
    ticks = _underlying_ticks(closes, start=_dt(14, 50))
    engine, strategy, positions = _build_engine(ticks)
    engine.run()

    assert strategy._candles_seen == len(closes)  # candles ARE evaluated (exits stay live)
    assert positions.trades == []
    assert engine._pending is None


# ------------------------------------------------------------ 7. square-off
def test_mandatory_15_15_square_off_still_closes_an_open_position():
    ticks = _underlying_ticks([100, 100, 100, 90], start=_dt(9, 15))
    _insert_fill(ticks, 4, PE_CONTRACT, 50.0)
    ticks.append(_tick(UNDERLYING, 90.0, _dt(15, 21)))  # any tick past square-off

    engine, _strategy, positions = _build_engine(ticks)
    engine.run()

    assert len(positions.trades) == 1
    assert positions.trades[0].exit_reason is ExitReason.SQUARE_OFF
    assert not positions.has_position()


# --------------------------------------------------------------- 8. sizing
def test_order_quantity_is_lots_per_trade_times_scrip_master_lot_size():
    strategy = EmaCross5x21BuyStrategy(ema_fast=2, ema_slow=3, lots_per_trade=10)
    ticks = _underlying_ticks([100, 100, 100, 90], start=_dt(9, 15))
    _insert_fill(ticks, 4, PE_CONTRACT, 50.0)

    engine, _strategy, positions = _build_engine(ticks, strategy=strategy)
    engine.run()

    assert strategy.quantity_lots == 10
    opened = positions.trades[0] if positions.trades else positions.positions[0]
    assert opened.lots == 10
    assert opened.quantity == 10 * LOT_SIZE  # 750 -- LOT_SIZE comes from the resolver, not config


# ----------------------------------------------------- 9. expiry never hardcoded
def test_option_selection_carries_no_hardcoded_expiry():
    """Expiry resolution is entirely delegated to OptionSelector/the scrip
    master (spec section 3/12.2) — OptionSelection has no expiry field for a
    strategy to hardcode into, and this strategy's own selection never
    supplies one."""
    strategy = EmaCross5x21BuyStrategy()
    selection = strategy.option_selection
    assert isinstance(selection, OptionSelection)
    assert not hasattr(selection, "expiry")


# ------------------------------------------------------- 10. premium vs. underlying
def test_premium_exit_never_fires_from_the_underlying_stream():
    """A wildly adverse-looking underlying move must not exit a position —
    only the traded option's own premium candles can."""
    closes = [100, 100, 100, 90, 1, 1, 1, 1]  # underlying craters after entry
    ticks = _underlying_ticks(closes, start=_dt(9, 15))
    _insert_fill(ticks, 4, PE_CONTRACT, 50.0)
    # No further PE ticks at all -- the premium exit has nothing to fire from.
    engine, _strategy, positions = _build_engine(ticks)
    engine.run()
    assert positions.trades == [] or positions.trades[0].exit_reason is not ExitReason.MOMENTUM_LOW


# -------------------------------------------------------------- 11/12. daily cap
def test_daily_mtm_loss_trips_the_cap_mid_trade():
    """capital_base 1,00,000 * 3% = Rs 3,000 daily_max_loss. Entry@50, qty=75:
    a drop to 10 books unrealised (10-50)*75 = -3,000, tripping the cap on the
    very next option tick -- before any candle even closes."""
    ticks = _underlying_ticks([100, 100, 100, 90], start=_dt(9, 15))
    _insert_fill(ticks, 4, PE_CONTRACT, 50.0)
    ticks.append(_tick(PE_CONTRACT, 10.0, _dt(9, 40)))

    engine, _strategy, positions = _build_engine(
        ticks, max_daily_loss_percent=3.0, starting_capital=100_000.0
    )
    engine.run()

    assert len(positions.trades) == 1
    assert positions.trades[0].exit_reason is ExitReason.DAILY_LOSS_LIMIT
    assert not positions.has_position()


def test_daily_cap_latches_off_all_further_entries_for_the_day():
    """The same tape that would otherwise reverse PE->CE->PE (see the
    reversal tests above) — but with the cap tripped right after the first
    fill. The later CE/PE fresh-cross signals must never open anything: the
    extra fill ticks already baked into `_reversal_tape()` simply arrive with
    no pending/open position to match, and are dropped."""
    ticks = _reversal_tape()
    # Chronological position matters -- SimulatedFeed delivers strictly in
    # list order. The PE fill (_insert_fill(ticks, 4, ...)) landed at index 5;
    # insert the cap-tripping tick immediately after it, not at the tape's end.
    ticks.insert(6, _tick(PE_CONTRACT, 10.0, _dt(9, 36, 20)))

    engine, _strategy, positions = _build_engine(
        ticks, max_daily_loss_percent=3.0, starting_capital=100_000.0
    )
    engine.run()

    assert len(positions.trades) == 1  # the DAILY_LOSS_LIMIT close only
    assert positions.trades[0].exit_reason is ExitReason.DAILY_LOSS_LIMIT
    assert not positions.has_position()


# --------------------------------------------------------- 13/14. restart recovery
def test_restart_of_the_same_open_trade_restores_persisted_exit_state():
    """AdoptedPosition + a persisted exit-state snapshot: the trail's extreme
    and activation must be exactly as a previous process left them, not reset."""
    contract = OptionContract(
        symbol="NIFTY WEEKLY 100 PE", security_id=PE_CONTRACT, strike=100.0,
        option_type=OptionType.PE, expiry="WEEKLY", lot_size=LOT_SIZE,
    )
    adopted = AdoptedPosition(
        contract=contract, side=OrderSide.BUY, lots=1, entry_price=50.0, entry_time=_dt(9, 20),
        last_price=60.0,
    )
    snapshot = {
        "security_id": PE_CONTRACT,
        "state": {
            "highest_close": 60.0,
            "lowest_close": 60.0,
            "trail": {"extreme": 60.0, "activated": True},
        },
    }

    strategy = EmaCross5x21BuyStrategy(ema_fast=2, ema_slow=3, lots_per_trade=1)
    positions = PositionManager(InMemoryGateway(slippage_points=0.0), lots=1)
    ticks = [_tick(UNDERLYING, 100.0, _dt(9, 15, 30))]
    engine = TradingEngine(
        EngineConfig(timeframe="5m", session=_session(), warmup_from_history=True),
        feed=SimulatedFeed(ticks),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=LOT_SIZE), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=UNDERLYING,
        recover_position=lambda: adopted,
        recover_exit_state=lambda: snapshot,
    )
    engine.run()

    assert positions.has_position()
    assert strategy._exit.extreme_close == 60.0
    assert strategy._exit.trail_activated is True


def test_restart_restores_daily_risk_state_and_the_cap_trips_from_the_carried_over_baseline():
    """Rs 2,000 already realised before the restart; capital_base 1,00,000 *
    3% = Rs 3,000 cap. Only Rs 1,000 more of unrealised loss should be needed
    to trip it post-restart -- proving the baseline carried over rather than
    resetting to zero."""
    contract = OptionContract(
        symbol="NIFTY WEEKLY 100 PE", security_id=PE_CONTRACT, strike=100.0,
        option_type=OptionType.PE, expiry="WEEKLY", lot_size=LOT_SIZE,
    )
    adopted = AdoptedPosition(
        contract=contract, side=OrderSide.BUY, lots=1, entry_price=50.0, entry_time=_dt(9, 20),
        last_price=50.0,
    )
    recovered_risk = DailyRiskRecovery(realised_pnl=-2_000.0, trade_count=1)

    strategy = EmaCross5x21BuyStrategy(ema_fast=2, ema_slow=3, lots_per_trade=1)
    positions = PositionManager(InMemoryGateway(slippage_points=0.0), lots=1)
    ticks = [
        _tick(UNDERLYING, 100.0, _dt(9, 15, 30)),
        # Only ~Rs 1,013 more of unrealised loss (quantity=75): drop to
        # ~36.5 -> (36.5-50)*75 = -1012.5, so realised(-2000)+open(-1012.5) =
        # -3012.5 <= -3000.
        _tick(PE_CONTRACT, 36.0, _dt(9, 21)),
    ]
    engine = TradingEngine(
        EngineConfig(
            timeframe="5m", session=_session(), warmup_from_history=True,
            max_daily_loss_percent=3.0, starting_capital=100_000.0,
        ),
        feed=SimulatedFeed(ticks),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=LOT_SIZE), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=UNDERLYING,
        recover_position=lambda: adopted,
        recover_daily_risk=lambda: recovered_risk,
    )
    engine.run()

    assert len(positions.trades) == 1
    assert positions.trades[0].exit_reason is ExitReason.DAILY_LOSS_LIMIT


# ------------------------------------------------ 14b. mid-session restart (real engine)
#
# Proven end to end through a real TradingEngine (not just the strategy in
# isolation): the no-warm-up-manager "today's-history" fallback (engine.py's
# own docstring: "this is the mid-session-restart mechanism") replays today's
# own candles through ~10:55; a genuine flip on the very first live candle
# after that must fire immediately. Also proves the completeness check
# (spec section 4.4: "verify it actually reached the required candle") is
# wired correctly at the engine level, both at the exact trust boundary and
# against a stale reconstruction.
def test_mid_session_restart_preserves_warmed_context_and_flips_on_the_first_live_candle():
    """Boundary-exact positive case: history_provider's last candle ends
    EXACTLY at the trust boundary (10:55, with clock pinned at 10:56 --
    inside the still-forming 10:55-11:00 bucket, so current_bucket == 10:55)
    -- this must land on the trusted side (>=), not be incorrectly excluded.
    The first live candle (10:55-11:00, closing bullish) then fires CE
    immediately."""
    history = _today_history_candles(
        [100.0, 100.0, 100.0, 90.0, 80.0, 70.0], start=_dt(10, 25)
    )  # last candle: 10:50-10:55 -- ends exactly on the trust boundary
    ticks = _underlying_ticks([90.0], start=_dt(10, 55))
    _insert_fill(ticks, 1, CE_CONTRACT, 60.0)  # fills the entry once the candle closes

    strategy = EmaCross5x21BuyStrategy(ema_fast=2, ema_slow=3, lots_per_trade=1)
    positions = PositionManager(InMemoryGateway(slippage_points=0.0), lots=1)
    engine = TradingEngine(
        EngineConfig(timeframe="5m", session=_session(), warmup_from_history=True),
        feed=SimulatedFeed(ticks),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=LOT_SIZE), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=UNDERLYING,
        history_provider=lambda: history,
        clock=lambda: _dt(10, 56),
    )
    engine.run()

    assert positions.trades or positions.has_position()
    opened = positions.trades[0] if positions.trades else positions.positions[0]
    assert opened.contract.option_type is OptionType.CE


def test_mid_session_restart_with_a_stale_history_provider_does_not_trust_the_context():
    """Negative counterpart: history_provider returns a real (non-empty,
    non-raising) but STALE reconstruction -- it stops at 10:25, well short
    of the pinned "now" (10:56, i.e. coverage through 10:55 is required).
    The completeness check must reject this and clear the crossover instead
    of trusting it, so the first live candle (10:55-11:00, closing bullish)
    must NOT enter even though it would flip a trusted bearish context."""
    history = _today_history_candles(
        [100.0, 100.0, 100.0, 90.0, 80.0, 70.0], start=_dt(9, 55)
    )  # last candle: 10:20-10:25 -- a 30-minute gap before "now"
    ticks = _underlying_ticks([90.0], start=_dt(10, 55))
    _insert_fill(ticks, 1, CE_CONTRACT, 60.0)  # would fill IF an entry (wrongly) fired

    strategy = EmaCross5x21BuyStrategy(ema_fast=2, ema_slow=3, lots_per_trade=1)
    positions = PositionManager(InMemoryGateway(slippage_points=0.0), lots=1)
    engine = TradingEngine(
        EngineConfig(timeframe="5m", session=_session(), warmup_from_history=True),
        feed=SimulatedFeed(ticks),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=LOT_SIZE), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=UNDERLYING,
        history_provider=lambda: history,
        clock=lambda: _dt(10, 56),
    )
    engine.run()

    assert positions.trades == []
    assert not positions.has_position()


# --------------------------------------------------- 15. stray ticks after close
def test_a_stray_option_tick_after_close_cannot_book_a_second_exit():
    """Reuses the daily-cap trip (proven above) to actually close the
    position, then delivers a stray tick for the now-abandoned contract."""
    ticks = _underlying_ticks([100, 100, 100, 90], start=_dt(9, 15))
    _insert_fill(ticks, 4, PE_CONTRACT, 50.0)  # lands at index 5
    ticks.insert(6, _tick(PE_CONTRACT, 10.0, _dt(9, 36, 20)))  # closes it (cap trip)
    ticks.insert(7, _tick(PE_CONTRACT, 5.0, _dt(9, 36, 30)))  # stray, position is gone

    engine, _strategy, positions = _build_engine(
        ticks, max_daily_loss_percent=3.0, starting_capital=100_000.0
    )
    engine.run()

    assert len(positions.trades) == 1
    assert not positions.has_position()


# ------------------------------------------------------------ 16. paper safety
def test_paper_mode_config_is_refused_by_the_live_gate():
    """The strategy's own delivered config (mode: paper, live_approved: false)
    can never route to a live broker, independent of anything this strategy
    itself does — the gate is consulted by the broker factory, not derived
    here (deviation D19)."""
    from common.config.models import GlobalConfig, ResolvedConfig, RuntimeConfig

    cfg = ResolvedConfig(
        global_config=GlobalConfig(live_trading_enabled=False),
        runtime=RuntimeConfig(runtime_id="intraday_options", enabled=True),
        strategy=StrategyConfig(
            strategy_id="ema_cross_5_21_buy",
            runtime_id="intraday_options",
            enabled=False,
            mode=ExecutionMode.PAPER,
            live_approved=False,
            parameters={"instrument": "NIFTY", "security_id": "13"},
        ),
    )
    decision = effective_live_gate(cfg)
    assert decision.allowed is False


def test_paper_execution_never_touches_a_real_broker_gateway():
    """InMemoryGateway (what every offline/paper run in this suite uses) has
    no network client at all — a real Dhan order is structurally impossible
    through this path."""
    ticks = _underlying_ticks([100, 100, 100, 90], start=_dt(9, 15))
    _insert_fill(ticks, 4, PE_CONTRACT, 50.0)
    engine, _strategy, positions = _build_engine(ticks)
    gateway = positions._gateway
    assert isinstance(gateway, InMemoryGateway)
    assert not hasattr(gateway, "_client") and not hasattr(gateway, "_http")
    engine.run()
