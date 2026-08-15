"""P1-1: the nine acceptance-matrix rows the original final report admitted
were proven only by inspection or shared infrastructure, not a dedicated
test. Each test below is named for exactly the row it proves.

Mirrors ``tests/integration/test_straddle_920_engine.py``'s fixture style.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from common.broker.costs import CostRates
from common.config.models import ExecutionMode
from common.engine.config import EngineConfig, SessionConfig
from common.engine.feed import SimulatedFeed
from common.engine.multi_leg_engine import MultiLegEngine
from common.engine.multi_leg_models import LegState
from common.engine.positions import InMemoryGateway, PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.models import ExitReason, Tick
from strategies.intraday_options.straddle_920.strategy import Straddle920Strategy

IST = ZoneInfo("Asia/Kolkata")
NIFTY = "NIFTY_IDX"
CE1 = "SIM:NIFTY:WEEKLY:24000:CE"
PE1 = "SIM:NIFTY:WEEKLY:24000:PE"


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id,
        instrument=security_id,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


def _build_engine(
    *,
    trading_date: str,
    gateway: InMemoryGateway | None = None,
    resolver_expiry: str | None = None,
    daily_loss_amount: float = 10_000_000.0,
) -> tuple[MultiLegEngine, PositionManager]:
    positions = PositionManager(gateway or InMemoryGateway(slippage_points=0.0), lots=10)
    strategy = Straddle920Strategy(
        lots_per_leg=10,
        entry_evaluation_time="09:20",
        last_entry_time="15:00",
        vix_threshold=20.0,
        leg_adjustment_multiplier=2.0,
        max_adjustments_per_day=1,
        daily_loss_amount=daily_loss_amount,
        combined_stop_percentage=0.30,
        profit_target_percentage=0.50,
    )
    engine = MultiLegEngine(
        EngineConfig(
            timeframe="5m",
            session=SessionConfig(
                timezone="Asia/Kolkata",
                start_time="09:15",
                end_time="15:00",
                square_off_time="15:15",
            ),
            execution_mode=ExecutionMode.PAPER,
        ),
        feed=SimulatedFeed([]),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=75),
            strike_step=50,
            expiry=resolver_expiry,
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=NIFTY,
        trading_date=trading_date,
    )
    return engine, positions


def _run(engine: MultiLegEngine, ticks: list[Tick]) -> None:
    engine.feed = SimulatedFeed(ticks)
    engine.run()


# ------------------------------------------------------ weekend/holiday
def test_a_weekend_trading_date_never_builds_a_candle_or_enters() -> None:
    """2026-08-15 is a Saturday. No candle can ever close on a non-trading
    day (MarketSession.is_open gates candle-building itself), so the
    strategy is never even asked to decide."""
    engine, positions = _build_engine(trading_date="2026-08-15")
    ticks = [
        _tick(NIFTY, 24000.0, datetime(2026, 8, 15, 9, 16, tzinfo=IST)),
        _tick(NIFTY, 24000.0, datetime(2026, 8, 15, 9, 21, tzinfo=IST)),
        _tick(NIFTY, 24000.0, datetime(2026, 8, 15, 10, 0, tzinfo=IST)),
    ]
    _run(engine, ticks)
    assert not positions.has_position()
    assert not positions.trades


def test_a_configured_holiday_never_builds_a_candle_or_enters() -> None:
    """A weekday configured as a holiday (e.g. a market-closed festival day)
    must block identically to a weekend, through the same session-level
    gate, not a strategy-level date check."""
    positions = PositionManager(InMemoryGateway(slippage_points=0.0), lots=10)
    strategy = Straddle920Strategy(lots_per_leg=10, entry_evaluation_time="09:20")
    engine = MultiLegEngine(
        EngineConfig(
            timeframe="5m",
            session=SessionConfig(
                timezone="Asia/Kolkata",
                start_time="09:15",
                end_time="15:00",
                square_off_time="15:15",
                holidays=("2026-08-17",),  # a Monday, deliberately marked a holiday
            ),
            execution_mode=ExecutionMode.PAPER,
        ),
        feed=SimulatedFeed([]),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=75), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=NIFTY,
        trading_date="2026-08-17",
    )
    ticks = [
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 9, 16, tzinfo=IST)),
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 9, 21, tzinfo=IST)),
    ]
    _run(engine, ticks)
    assert not positions.has_position()


# --------------------------------------------------------- expiry-day entry
def test_expiry_day_entry_is_permitted() -> None:
    """Spec: no expiry-day exclusion. Proven here by resolving every
    contract with an expiry equal to the trading date itself — i.e. "today
    genuinely is the option's own expiry day" — and confirming entry still
    proceeds exactly as any other day."""
    trading_date = "2026-08-17"
    engine, positions = _build_engine(trading_date=trading_date, resolver_expiry=trading_date)
    ce1 = f"SIM:NIFTY:{trading_date}:24000:CE"
    pe1 = f"SIM:NIFTY:{trading_date}:24000:PE"
    ticks = [
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 9, 16, tzinfo=IST)),
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 9, 21, tzinfo=IST)),
        _tick(ce1, 100.0, datetime(2026, 8, 17, 9, 21, 5, tzinfo=IST)),
        _tick(pe1, 95.0, datetime(2026, 8, 17, 9, 21, 10, tzinfo=IST)),
    ]
    _run(engine, ticks)
    assert len(positions.positions) == 2
    assert all(p.contract.expiry == trading_date for p in positions.positions)


# ----------------------------------------------------------- late startup
def test_a_late_starting_process_still_enters_on_its_first_evaluated_candle() -> None:
    """Spec: no upper-bound cutoff on the *primary* entry beyond
    ``entries_consumed`` — a process whose first tick of the day arrives well
    after 09:20 (e.g. a late manual start, or a supervisor restart late in
    the morning) still evaluates and enters on the first candle it ever
    completes, because that candle's own close time is >= 09:20 and the
    day's one attempt has not been consumed yet."""
    engine, positions = _build_engine(trading_date="2026-08-17")
    ticks = [
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 14, 0, tzinfo=IST)),  # first tick ever, 14:00
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 14, 5, tzinfo=IST)),  # closes the first candle
        _tick(CE1, 100.0, datetime(2026, 8, 17, 14, 5, 5, tzinfo=IST)),
        _tick(PE1, 95.0, datetime(2026, 8, 17, 14, 5, 10, tzinfo=IST)),
    ]
    _run(engine, ticks)
    assert len(positions.positions) == 2


# --------------------------------------------------- replacement fresh tick
def test_a_replacement_leg_never_fills_on_an_unrelated_tick() -> None:
    """Once the replacement is queued (PENDING_ORDER, subscribed), an
    unrelated tick — even one on the underlying itself, which drives every
    other decision in this engine — must never fill it. Only a tick whose
    ``security_id`` is genuinely the replacement contract's own can."""
    engine, positions = _build_engine(trading_date="2026-08-17")
    ce1, pe1, ce2 = CE1, PE1, "SIM:NIFTY:WEEKLY:24050:CE"
    ticks = [
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 9, 16, tzinfo=IST)),
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 9, 21, tzinfo=IST)),
        _tick(ce1, 100.0, datetime(2026, 8, 17, 9, 21, 5, tzinfo=IST)),
        _tick(pe1, 95.0, datetime(2026, 8, 17, 9, 21, 10, tzinfo=IST)),
        _tick(ce1, 205.0, datetime(2026, 8, 17, 9, 30, tzinfo=IST)),  # doubles
        _tick(NIFTY, 24050.0, datetime(2026, 8, 17, 9, 31, tzinfo=IST)),
        _tick(NIFTY, 24050.0, datetime(2026, 8, 17, 9, 36, tzinfo=IST)),  # replacement queued
        # An unrelated underlying tick arrives — must not fill ce2.
        _tick(NIFTY, 24051.0, datetime(2026, 8, 17, 9, 37, tzinfo=IST)),
    ]
    _run(engine, ticks)
    leg = engine._basket.legs[f"{engine._basket_id}:CE:2"]
    assert leg.state is LegState.PENDING_ORDER, "an unrelated tick must never fill the replacement"
    assert not any(p.contract.security_id == ce2 for p in positions.positions)

    # Its own fresh tick, by contrast, does fill it.
    engine.feed = SimulatedFeed([_tick(ce2, 90.0, datetime(2026, 8, 17, 9, 38, tzinfo=IST))])
    engine.feed.on_tick(engine.on_tick)
    engine.feed.run()
    assert leg.state is LegState.OPEN


# ---------------------------------------- adjustment priority over combined
def test_adjustment_takes_priority_over_combined_stop_on_the_same_tick() -> None:
    """The single tick that doubles CE also, arithmetically, breaches the
    combined-stop threshold (step 4) if it were checked first. The engine
    must resolve the doubling (step 2) and stop there — never an EXIT_ALL
    combined-stop closing both legs on that same tick."""
    # open_basis = (100+95)*750 = 146,250; 30% = 43,875. CE doubling to 205
    # alone already produces U = (100-205)*750 = -78,750 <= -43,875 — the
    # combined-stop condition is also true on this exact tick.
    engine, positions = _build_engine(trading_date="2026-08-17", daily_loss_amount=10_000_000.0)
    ce1, pe1 = CE1, PE1
    ticks = [
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 9, 16, tzinfo=IST)),
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 9, 21, tzinfo=IST)),
        _tick(ce1, 100.0, datetime(2026, 8, 17, 9, 21, 5, tzinfo=IST)),
        _tick(pe1, 95.0, datetime(2026, 8, 17, 9, 21, 10, tzinfo=IST)),
        _tick(ce1, 205.0, datetime(2026, 8, 17, 9, 30, tzinfo=IST)),
    ]
    _run(engine, ticks)

    assert len(positions.trades) == 1, "only the doubled leg closes on this tick — never both"
    assert positions.trades[0].exit_reason is ExitReason.ADJUSTMENT
    # PE is untouched — a combined-stop EXIT_ALL would have closed it too.
    assert any(p.contract.security_id == pe1 for p in positions.positions)


# ------------------------------------- adjusted-out realised loss excluded
def test_the_adjusted_out_legs_realised_loss_does_not_feed_the_combined_stop() -> None:
    """Spec section 13.3: the combined stop reads *unrealised* P&L on the
    *currently open* legs only. The adjusted-out leg's large realised loss
    (already booked into R) must never leak into that check — proven by a
    combined-stop threshold that the adjustment's own realised loss alone
    would trip if (incorrectly) included, but does not."""
    # Adjustment realises -78,750 on CE1. Set the combined-stop percentage
    # tiny enough that 30%-of-basis is far smaller than 78,750 — if R leaked
    # into the unrealised-only combined-stop check, this would trip
    # immediately after the adjustment. It must not: PE1 and CE2 stay open.
    engine, positions = _build_engine(
        trading_date="2026-08-17", daily_loss_amount=10_000_000.0
    )
    engine.strategy._combined_stop_percentage = 0.01  # 1% of open basis
    ce1, pe1, ce2 = CE1, PE1, "SIM:NIFTY:WEEKLY:24050:CE"
    ticks = [
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 9, 16, tzinfo=IST)),
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 9, 21, tzinfo=IST)),
        _tick(ce1, 100.0, datetime(2026, 8, 17, 9, 21, 5, tzinfo=IST)),
        _tick(pe1, 95.0, datetime(2026, 8, 17, 9, 21, 10, tzinfo=IST)),
        _tick(ce1, 205.0, datetime(2026, 8, 17, 9, 30, tzinfo=IST)),  # adjustment: R = -78,750
        _tick(NIFTY, 24050.0, datetime(2026, 8, 17, 9, 31, tzinfo=IST)),
        _tick(NIFTY, 24050.0, datetime(2026, 8, 17, 9, 36, tzinfo=IST)),  # replacement queued
        _tick(ce2, 90.0, datetime(2026, 8, 17, 9, 36, 5, tzinfo=IST)),  # replacement fills
        # A tiny move on the replacement — must not, by itself, trip a
        # combined stop inflated by the adjusted-out leg's realised loss.
        _tick(ce2, 90.5, datetime(2026, 8, 17, 9, 40, tzinfo=IST)),
    ]
    _run(engine, ticks)

    assert any(p.contract.security_id == pe1 for p in positions.positions)
    assert any(p.contract.security_id == ce2 for p in positions.positions)
    adjustment_trades = [t for t in positions.trades if t.exit_reason is ExitReason.ADJUSTMENT]
    assert len(adjustment_trades) == 1


# -------------------------------------------------- charges excluded
def test_charges_are_excluded_from_every_risk_trigger() -> None:
    """Every threshold this strategy checks reads gross (pre-charge) P&L.
    Proven with charges inflated large enough that the *net* figure would
    cross ``daily_loss_amount`` while the *gross* figure — the one the
    strategy actually reads — does not."""
    # CE entry@100 -> exactly doubles to 200 (adjustment trigger) -> gross
    # realised R = (100-200)*750 = -75,000. With brokerage_per_order=5000
    # (both legs of the round trip), total charges are roughly 11,900+,
    # pushing NET well past -80,000 while GROSS stays at -75,000.
    gateway = InMemoryGateway(slippage_points=0.0, rates=CostRates(brokerage_per_order=5000.0))
    engine, positions = _build_engine(
        trading_date="2026-08-17", gateway=gateway, daily_loss_amount=80_000.0
    )
    ce1, pe1 = CE1, PE1
    ticks = [
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 9, 16, tzinfo=IST)),
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 9, 21, tzinfo=IST)),
        _tick(ce1, 100.0, datetime(2026, 8, 17, 9, 21, 5, tzinfo=IST)),
        _tick(pe1, 95.0, datetime(2026, 8, 17, 9, 21, 10, tzinfo=IST)),
        _tick(ce1, 200.0, datetime(2026, 8, 17, 9, 30, tzinfo=IST)),  # doubles exactly
        # A second, unrelated PE tick at an unchanged price: re-evaluates the
        # daily-loss check against the now-realised R with nothing else moving.
        _tick(pe1, 95.0, datetime(2026, 8, 17, 9, 31, tzinfo=IST)),
    ]
    _run(engine, ticks)

    adjustment_trade = next(t for t in positions.trades if t.exit_reason is ExitReason.ADJUSTMENT)
    assert adjustment_trade.gross_pnl == -75_000.0
    net_pnl = adjustment_trade.net_pnl
    assert net_pnl < -80_000.0, "the scenario must genuinely make net cross the threshold"
    # If charges had leaked into the trigger, this would already be an
    # EXIT_ALL/DAILY_LOSS_LIMIT — PE must still be open.
    assert any(p.contract.security_id == pe1 for p in positions.positions)
    assert not any(t.exit_reason is ExitReason.DAILY_LOSS_LIMIT for t in positions.trades)


# ----------------------------------------------- pending intents terminal
def test_a_never_filled_primary_leg_is_expired_not_left_pending_at_square_off() -> None:
    """One of the two primary legs never receives a fill tick all day (a
    genuinely partial primary entry, spec section 9.6) — at hard square-off
    it must reach a terminal state (EXPIRED), never be left PENDING_ORDER
    with a live subscription and no owner."""
    engine, positions = _build_engine(trading_date="2026-08-17")
    ticks = [
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 9, 16, tzinfo=IST)),
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 9, 21, tzinfo=IST)),
        _tick(CE1, 100.0, datetime(2026, 8, 17, 9, 21, 5, tzinfo=IST)),  # only CE ever fills
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 15, 16, tzinfo=IST)),  # hard square-off
    ]
    _run(engine, ticks)

    pe_leg = engine._basket.legs[f"{engine._basket_id}:PE:1"]
    assert pe_leg.state is LegState.EXPIRED
    assert len(positions.trades) == 1
    assert positions.trades[0].contract.option_type.value == "CE"


# -------------------------------------------------------- zero slippage
def test_fills_land_exactly_on_the_reference_tick_price_with_zero_slippage() -> None:
    """Spec section 10: zero paper slippage — every fill price (entry and
    exit alike) must equal the exact tick price that produced it, not an
    offset value."""
    engine, positions = _build_engine(trading_date="2026-08-17")
    ce1 = CE1
    ticks = [
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 9, 16, tzinfo=IST)),
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 9, 21, tzinfo=IST)),
        _tick(ce1, 123.45, datetime(2026, 8, 17, 9, 21, 5, tzinfo=IST)),
        _tick(PE1, 95.0, datetime(2026, 8, 17, 9, 21, 10, tzinfo=IST)),
        # Marks the leg's last price before the square-off-triggering tick
        # itself (which the engine intercepts for the square-off check
        # before ever routing it to a leg — see MultiLegEngine.on_tick).
        _tick(ce1, 76.5, datetime(2026, 8, 17, 15, 14, 30, tzinfo=IST)),
        _tick(NIFTY, 24000.0, datetime(2026, 8, 17, 15, 16, tzinfo=IST)),  # hard square-off
    ]
    _run(engine, ticks)

    ce_trade = next(t for t in positions.trades if t.contract.security_id == ce1)
    assert ce_trade.entry_price == 123.45
    assert ce_trade.exit_price == 76.5
