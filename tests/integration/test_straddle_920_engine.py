"""``straddle_920`` driven through a real :class:`MultiLegEngine` + ``SimulatedFeed``.

Mirrors ``tests/integration/test_ema_cross_9_21_buy_engine.py``'s structure: a
real engine, a real (offline) execution path (``InMemoryGateway``), and a
deterministic tick tape — no monkeypatching, no real broker/network. Covers the
acceptance-matrix rows that only a real engine run can prove: entry, partial
fill, adjustment end-to-end (close-then-replace), replacement fresh-tick gating,
one-adjustment-per-day, exact risk priority, and hard square-off.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from common.config.models import ExecutionMode
from common.engine.config import EngineConfig, SessionConfig
from common.engine.feed import SimulatedFeed
from common.engine.multi_leg_engine import MultiLegEngine
from common.engine.positions import InMemoryGateway, PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.models import ExitReason, Tick
from strategies.intraday_options.straddle_920.strategy import Straddle920Strategy

IST = ZoneInfo("Asia/Kolkata")
NIFTY = "NIFTY_IDX"
VIX = "INDIA_VIX_IDX"


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 17, hour, minute, second, tzinfo=IST)  # a Monday


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
    vix_threshold: float = 20.0,
    daily_loss_amount: float = 60_000.0,
    combined_stop_percentage: float = 0.30,
    profit_target_percentage: float = 0.50,
    blackout_dates: tuple[str, ...] = (),
    trading_date: str = "2026-08-17",
) -> tuple[MultiLegEngine, PositionManager]:
    positions = PositionManager(InMemoryGateway(slippage_points=0.0), lots=10)
    strategy = Straddle920Strategy(
        lots_per_leg=10,
        entry_evaluation_time="09:20",
        last_entry_time="15:00",
        vix_threshold=vix_threshold,
        leg_adjustment_multiplier=2.0,
        max_adjustments_per_day=1,
        daily_loss_amount=daily_loss_amount,
        combined_stop_percentage=combined_stop_percentage,
        profit_target_percentage=profit_target_percentage,
        blackout_dates=blackout_dates,
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
        feed=SimulatedFeed([]),  # replaced per-test below
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=75), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=NIFTY,
        vix_security_id=VIX,
        trading_date=trading_date,
    )
    return engine, positions


def _run(engine: MultiLegEngine, ticks: list[Tick]) -> None:
    engine.feed = SimulatedFeed(ticks)
    engine.run()


# ---------------------------------------------------------------- 1. entry
def test_0920_entry_sells_current_atm_ce_and_pe() -> None:
    engine, positions = _build_engine()
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 16)),
        _tick(VIX, 12.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 21)),  # closes the 09:15-09:20 candle -> ENTER_BASKET
        _tick("SIM:NIFTY:WEEKLY:24000:CE", 100.0, _ts(9, 21, 5)),
        _tick("SIM:NIFTY:WEEKLY:24000:PE", 95.0, _ts(9, 21, 10)),
    ]
    _run(engine, ticks)

    assert positions.has_position()
    assert len(positions.positions) == 2
    open_roles = {p.contract.option_type.value for p in positions.positions}
    assert open_roles == {"CE", "PE"}


# --------------------------------------------------------- 2. partial fill
def test_one_leg_fill_stays_open_while_the_other_remains_pending() -> None:
    engine, positions = _build_engine()
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 21)),  # ENTER_BASKET queued
        _tick("SIM:NIFTY:WEEKLY:24000:CE", 100.0, _ts(9, 21, 5)),  # only CE fills
        _tick(NIFTY, 24000.0, _ts(15, 16)),  # past square-off: force close
    ]
    _run(engine, ticks)

    assert len(positions.trades) == 1
    assert positions.trades[0].contract.option_type.value == "CE"


# --------------------------------------------------- 3. VIX above 20 skips
def test_vix_above_threshold_skips_the_day() -> None:
    engine, positions = _build_engine(vix_threshold=20.0)
    ticks = [
        _tick(VIX, 25.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 21)),
    ]
    _run(engine, ticks)
    assert not positions.has_position()
    assert not positions.trades


def test_vix_exactly_threshold_permits_entry() -> None:
    engine, positions = _build_engine(vix_threshold=20.0)
    ticks = [
        _tick(VIX, 20.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 21)),
        _tick("SIM:NIFTY:WEEKLY:24000:CE", 100.0, _ts(9, 21, 5)),
        _tick("SIM:NIFTY:WEEKLY:24000:PE", 95.0, _ts(9, 21, 10)),
    ]
    _run(engine, ticks)
    assert len(positions.positions) == 2


def test_missing_vix_permits_entry_fail_open() -> None:
    engine, positions = _build_engine(vix_threshold=20.0)
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 16)),  # no VIX tick at all this session
        _tick(NIFTY, 24000.0, _ts(9, 21)),
        _tick("SIM:NIFTY:WEEKLY:24000:CE", 100.0, _ts(9, 21, 5)),
        _tick("SIM:NIFTY:WEEKLY:24000:PE", 95.0, _ts(9, 21, 10)),
    ]
    _run(engine, ticks)
    assert len(positions.positions) == 2


# -------------------------------------------------------- 4. news blackout
def test_news_blackout_date_skips_the_day() -> None:
    engine, positions = _build_engine(blackout_dates=("2026-08-17",))
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 21)),
    ]
    _run(engine, ticks)
    assert not positions.has_position()


# ---------------------------------------------- 5. adjustment: CE doubles
def test_ce_doubling_closes_ce_only_and_replaces_it() -> None:
    engine, positions = _build_engine()
    ce1 = "SIM:NIFTY:WEEKLY:24000:CE"
    pe1 = "SIM:NIFTY:WEEKLY:24000:PE"
    ce2 = "SIM:NIFTY:WEEKLY:24050:CE"  # a replacement ATM shifts with spot
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 21)),  # ENTER_BASKET
        _tick(ce1, 100.0, _ts(9, 21, 5)),
        _tick(pe1, 95.0, _ts(9, 21, 10)),
        _tick(ce1, 205.0, _ts(9, 30)),  # CE doubles (>= 2x100) -> ADJUSTMENT
        _tick(NIFTY, 24050.0, _ts(9, 31)),  # next candle: spot moved, ATM now 24050
        _tick(NIFTY, 24050.0, _ts(9, 36)),  # closes that candle -> ENTER_LEG (replacement)
        _tick(ce2, 90.0, _ts(9, 36, 5)),  # replacement fills on its first fresh tick
        _tick(NIFTY, 24050.0, _ts(15, 16)),  # square-off
    ]
    _run(engine, ticks)

    ce_trades = [t for t in positions.trades if t.contract.option_type.value == "CE"]
    pe_trades = [t for t in positions.trades if t.contract.option_type.value == "PE"]
    assert len(ce_trades) == 2  # the doubled original + the square-off of the replacement
    assert ce_trades[0].exit_reason is ExitReason.ADJUSTMENT
    assert ce_trades[0].contract.security_id == ce1
    assert ce_trades[1].contract.security_id == ce2
    assert len(pe_trades) == 1  # PE was never touched by the adjustment
    assert pe_trades[0].exit_reason is ExitReason.SQUARE_OFF


def test_only_one_adjustment_per_day() -> None:
    engine, positions = _build_engine()
    ce1 = "SIM:NIFTY:WEEKLY:24000:CE"
    pe1 = "SIM:NIFTY:WEEKLY:24000:PE"
    ce2 = "SIM:NIFTY:WEEKLY:24000:CE"  # ATM unchanged -> replacement resolves to the same id
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 21)),
        _tick(ce1, 100.0, _ts(9, 21, 5)),
        _tick(pe1, 95.0, _ts(9, 21, 10)),
        _tick(ce1, 205.0, _ts(9, 30)),  # first doubling -> adjustment #1
        _tick(NIFTY, 24000.0, _ts(9, 31)),
        _tick(NIFTY, 24000.0, _ts(9, 36)),  # replacement queued
        _tick(ce2, 90.0, _ts(9, 36, 5)),  # replacement fills
        _tick(ce2, 500.0, _ts(9, 40)),  # replacement also doubles — must NOT adjust again
        _tick(NIFTY, 24000.0, _ts(15, 16)),
    ]
    _run(engine, ticks)

    ce_trades = [t for t in positions.trades if t.contract.option_type.value == "CE"]
    adjustment_exits = [t for t in ce_trades if t.exit_reason is ExitReason.ADJUSTMENT]
    assert len(adjustment_exits) == 1


# ------------------------------------------------------ 6. hard square-off
def test_hard_square_off_closes_every_open_leg() -> None:
    engine, positions = _build_engine()
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 21)),
        _tick("SIM:NIFTY:WEEKLY:24000:CE", 100.0, _ts(9, 21, 5)),
        _tick("SIM:NIFTY:WEEKLY:24000:PE", 95.0, _ts(9, 21, 10)),
        _tick(NIFTY, 24000.0, _ts(15, 16)),
    ]
    _run(engine, ticks)

    assert not positions.has_position()
    assert len(positions.trades) == 2
    assert all(t.exit_reason is ExitReason.SQUARE_OFF for t in positions.trades)


# ------------------------------------------------------- 7. profit target
def test_profit_target_closes_both_legs() -> None:
    # B_original = 100 + 95 = 195; Q = 10 lots x 75 = 750; target = 0.5*195*750 = 73,125
    engine, positions = _build_engine()
    ce1 = "SIM:NIFTY:WEEKLY:24000:CE"
    pe1 = "SIM:NIFTY:WEEKLY:24000:PE"
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 21)),
        _tick(ce1, 100.0, _ts(9, 21, 5)),
        _tick(pe1, 95.0, _ts(9, 21, 10)),
        # both legs collapse in value -> big gross unrealised profit for the seller
        _tick(ce1, 10.0, _ts(10, 0)),
        _tick(pe1, 5.0, _ts(10, 1)),  # U = (100-10)*750 + (95-5)*750 = 67500+67500=135000 >= 73125
    ]
    _run(engine, ticks)

    assert not positions.has_position()
    assert len(positions.trades) == 2
    assert all(t.exit_reason is ExitReason.TARGET_PROFIT for t in positions.trades)


# ------------------------------------------------------------ 8. PE doubles
def test_pe_doubling_closes_pe_only_and_retains_ce() -> None:
    engine, positions = _build_engine()
    ce1 = "SIM:NIFTY:WEEKLY:24000:CE"
    pe1 = "SIM:NIFTY:WEEKLY:24000:PE"
    pe2 = "SIM:NIFTY:WEEKLY:23950:PE"
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 21)),
        _tick(ce1, 100.0, _ts(9, 21, 5)),
        _tick(pe1, 95.0, _ts(9, 21, 10)),
        _tick(pe1, 195.0, _ts(9, 30)),  # PE doubles (>= 2x95)
        _tick(NIFTY, 23950.0, _ts(9, 31)),
        _tick(NIFTY, 23950.0, _ts(9, 36)),  # replacement queued
        _tick(pe2, 80.0, _ts(9, 36, 5)),
        _tick(NIFTY, 23950.0, _ts(15, 16)),
    ]
    _run(engine, ticks)

    pe_trades = [t for t in positions.trades if t.contract.option_type.value == "PE"]
    ce_trades = [t for t in positions.trades if t.contract.option_type.value == "CE"]
    assert len(pe_trades) == 2
    assert pe_trades[0].exit_reason is ExitReason.ADJUSTMENT
    assert len(ce_trades) == 1
    assert ce_trades[0].exit_reason is ExitReason.SQUARE_OFF


# --------------------------------------------------------- 9. combined stop
def test_combined_stop_closes_both_legs() -> None:
    # open_basis = (100+95)*750 = 146,250; 30% -> 43,875 loss allowance.
    # daily_loss_amount raised well above the loss this scenario produces, so
    # only the combined-stop trigger (not the daily-loss one, checked first)
    # can fire — isolating the rule under test.
    engine, positions = _build_engine(daily_loss_amount=1_000_000.0)
    ce1 = "SIM:NIFTY:WEEKLY:24000:CE"
    pe1 = "SIM:NIFTY:WEEKLY:24000:PE"
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 21)),
        _tick(ce1, 100.0, _ts(9, 21, 5)),
        _tick(pe1, 95.0, _ts(9, 21, 10)),
        # Both legs rise moderately (not doubling CE) so combined stop fires
        # before either leg individually reaches 2x.
        _tick(ce1, 150.0, _ts(9, 40)),  # -50*750 = -37500 unrealised
        _tick(pe1, 150.0, _ts(9, 41)),  # -55*750 = -41250 more; U=-78750 <= -43875
    ]
    _run(engine, ticks)

    assert not positions.has_position()
    assert len(positions.trades) == 2
    assert all(t.exit_reason is ExitReason.STOP_LOSS for t in positions.trades)


# ---------------------------------------------------------- 10. daily loss
def test_replacement_is_prohibited_after_the_1500_cutoff() -> None:
    """Spec section 12.4: the cutoff is reached before a replacement can be
    attempted — no replacement leg is created, the surviving leg (PE) is
    never auto-closed for it, and normal risk/square-off stays active."""
    engine, positions = _build_engine()
    ce1 = "SIM:NIFTY:WEEKLY:24000:CE"
    pe1 = "SIM:NIFTY:WEEKLY:24000:PE"
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 21)),
        _tick(ce1, 100.0, _ts(9, 21, 5)),
        _tick(pe1, 95.0, _ts(9, 21, 10)),
        _tick(ce1, 205.0, _ts(14, 58)),  # CE doubles just before the cutoff
        _tick(NIFTY, 24050.0, _ts(14, 59)),
        _tick(NIFTY, 24050.0, _ts(15, 4)),  # next candle closes AFTER 15:00
        _tick(NIFTY, 24050.0, _ts(15, 16)),  # hard square-off
    ]
    _run(engine, ticks)

    # No replacement CE was ever created.
    ce_trades = [t for t in positions.trades if t.contract.option_type.value == "CE"]
    assert len(ce_trades) == 1
    assert ce_trades[0].exit_reason is ExitReason.ADJUSTMENT

    # PE was never touched by the expired replacement and is squared off normally.
    pe_trades = [t for t in positions.trades if t.contract.option_type.value == "PE"]
    assert len(pe_trades) == 1
    assert pe_trades[0].exit_reason is ExitReason.SQUARE_OFF

    basket = engine._basket
    assert basket.pending_replacement_state == "EXPIRED"
    assert basket.pending_replacement_role is None


def test_a_missing_replacement_tick_does_not_flatten_the_surviving_leg() -> None:
    """Spec section 12.4: replacement contract subscribed but never receives a
    fresh tick before square-off — the pending leg is terminally resolved
    (never a phantom fill on stale data) and PE stays open and risk-managed
    throughout, closed only by the ordinary hard square-off.

    daily_loss_amount is raised well above what the adjusted-out CE's own
    realised loss produces, so only the property under test — the
    surviving leg's own fate — decides the outcome, not an unrelated
    daily-loss trip.
    """
    engine, positions = _build_engine(daily_loss_amount=10_000_000.0)
    ce1 = "SIM:NIFTY:WEEKLY:24000:CE"
    pe1 = "SIM:NIFTY:WEEKLY:24000:PE"
    ce2 = "SIM:NIFTY:WEEKLY:24050:CE"  # subscribed, but never ticks
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 21)),
        _tick(ce1, 100.0, _ts(9, 21, 5)),
        _tick(pe1, 95.0, _ts(9, 21, 10)),
        _tick(ce1, 205.0, _ts(9, 30)),  # CE doubles
        _tick(NIFTY, 24050.0, _ts(9, 31)),
        _tick(NIFTY, 24050.0, _ts(9, 36)),  # replacement queued (PENDING_ORDER)
        # ce2 never ticks; PE keeps trading normally until square-off.
        _tick(pe1, 90.0, _ts(10, 0)),
        _tick(NIFTY, 24050.0, _ts(15, 16)),  # hard square-off
    ]
    _run(engine, ticks)

    assert not positions.has_position()
    pe_trades = [t for t in positions.trades if t.contract.option_type.value == "PE"]
    assert len(pe_trades) == 1
    assert pe_trades[0].exit_reason is ExitReason.SQUARE_OFF
    # ce2 was never opened — it never received a fresh tick — so it never
    # appears in positions.trades (no fill occurred) at all.
    assert all(t.contract.security_id != ce2 for t in positions.trades)

    leg = engine._basket.legs[f"{engine._basket.basket_id}:CE:2"]
    assert leg.state.value == "EXPIRED"


def test_combined_stop_after_replacement_uses_the_rebased_basis() -> None:
    """Spec section 13.3: after a replacement fills, the combined-stop basis
    is the *retained* leg's entry premium plus the *replacement*'s — not the
    original (now-closed) leg's. Constructed so the rebased threshold
    (retained PE + replacement CE) trips while the stale, un-rebased
    threshold (original CE + PE) would not have — proving the engine is
    genuinely using the rebased basis, not the original one."""
    engine, positions = _build_engine(daily_loss_amount=10_000_000.0)
    ce1 = "SIM:NIFTY:WEEKLY:24000:CE"
    pe1 = "SIM:NIFTY:WEEKLY:24000:PE"
    ce2 = "SIM:NIFTY:WEEKLY:24050:CE"
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 21)),
        _tick(ce1, 100.0, _ts(9, 21, 5)),
        _tick(pe1, 95.0, _ts(9, 21, 10)),
        _tick(ce1, 205.0, _ts(9, 30)),  # CE doubles and closes (realised -78750)
        _tick(NIFTY, 24050.0, _ts(9, 31)),
        _tick(NIFTY, 24050.0, _ts(9, 36)),  # replacement queued
        _tick(ce2, 90.0, _ts(9, 36, 5)),  # replacement fills
        # Rebased open basis = (PE 95 + CE2 90) * 750 = 138,750; 30% = 41,625.
        # Original (stale) basis would have been (CE 100 + PE 95) * 750 =
        # 146,250; 30% = 43,875. U = -42,000 crosses the rebased threshold
        # but not the stale one.
        _tick(ce2, 146.0, _ts(9, 40)),  # CE2: (90-146)*750 = -42,000
    ]
    _run(engine, ticks)

    assert not positions.has_position()
    ce_trades = [t for t in positions.trades if t.contract.option_type.value == "CE"]
    pe_trades = [t for t in positions.trades if t.contract.option_type.value == "PE"]
    assert len(ce_trades) == 2  # the adjusted-out original + the replacement
    assert ce_trades[1].exit_reason is ExitReason.STOP_LOSS
    assert ce_trades[1].contract.security_id == ce2
    assert len(pe_trades) == 1
    assert pe_trades[0].exit_reason is ExitReason.STOP_LOSS


def test_daily_loss_limit_closes_both_legs() -> None:
    engine, positions = _build_engine(daily_loss_amount=1_000.0)  # tiny cap, easy to trip
    ce1 = "SIM:NIFTY:WEEKLY:24000:CE"
    pe1 = "SIM:NIFTY:WEEKLY:24000:PE"
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 16)),
        _tick(NIFTY, 24000.0, _ts(9, 21)),
        _tick(ce1, 100.0, _ts(9, 21, 5)),
        _tick(pe1, 95.0, _ts(9, 21, 10)),
        _tick(ce1, 101.0, _ts(9, 25)),  # -750 unrealised, still above -1000
        _tick(pe1, 96.0, _ts(9, 26)),  # another -750 -> total -1500 <= -1000
    ]
    _run(engine, ticks)

    assert not positions.has_position()
    assert len(positions.trades) == 2
    assert all(t.exit_reason is ExitReason.DAILY_LOSS_LIMIT for t in positions.trades)
