"""``supertrend_buy_1_1p2`` driven by a real, fully-constructed ``TradingEngine``
over a simulated tape. No monkeypatching, no ``__new__`` shortcuts — mirrors
``tests/integration/test_ema_cross_9_21_buy_engine.py``'s discipline.

These prove what the strategy deliberately does **not** implement itself (spec
sections 8 and 9): contract and ATM-strike resolution, order quantity from the
exchange lot size, fresh-tick execution, close-before-open reversal, the entry
cutoff and the mandatory square-off are all engine-owned, and this is where that is
demonstrated end to end. The flip logic itself is proven at unit level in
``tests/unit/test_supertrend_buy_1_1p2_strategy.py``.

A warm-up manager is mandatory here, not optional scaffolding: SuperTrend is
``continuity_required``, so ``TradingEngine.__init__`` refuses outright
(``InvalidWarmupConfig``) without one. Every engine below is therefore built with a
real ``WarmupManager`` returning a verified-complete replay — the same shape
production uses.

Verified sequence (walked through the real ``SuperTrend(1, 1.2)`` before being
hard-coded, not hand-derived): 75 flat warm-up candles at 20000 leave the indicator
in an uptrend with its band at 20000 and no flips at all, so every flip below belongs
to the live tape. Live closes ``19500 -> 20200 -> 20300 -> 19000`` then flip DOWN, UP,
(no flip), DOWN.

Strikes follow the production rule, which is worth stating because it surprises: the
ATM is resolved from ``self._spot``, the price of the tick that *closed* the bar —
i.e. the first tick of the next bucket — not the bar's own close. So the 19500 bar
resolves its strike from the following 20200 tick.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from common.engine.config import EngineConfig, SessionConfig
from common.engine.engine import TradingEngine
from common.engine.feed import SimulatedFeed
from common.engine.models import (
    ExitReason,
    OptionType,
    OrderSide,
    SignalAction,
    StrategySignal,
)
from common.engine.positions import InMemoryGateway, PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.engine.session import MarketSession
from common.indicators.base import OHLC
from common.models import Candle, Tick
from common.warmup.manager import WarmupManager
from common.warmup.session_buckets import session_bucket_starts
from common.warmup.source import WarmupSource
from strategies.intraday_options.supertrend_buy_1_1p2.strategy import SupertrendBuy1x1p2Strategy

IST = ZoneInfo("Asia/Kolkata")
UNDERLYING = "INDEX"
DEFAULT_LOT_SIZE = 75
LOTS = 10
TIMEFRAME_MINUTES = 5

_SOURCE = WarmupSource(security_id="13", exchange_segment="IDX_I", instrument_type="INDEX")

#: The live tape's flips, and the contracts each one resolves to.
LIVE_CLOSES = [19500.0, 20200.0, 20300.0, 19000.0]
PE_1 = "SIM:NIFTY:WEEKLY:20200:PE"  # DOWN flip on the 19500 bar, spot 20200
CE_2 = "SIM:NIFTY:WEEKLY:20300:CE"  # UP flip on the 20200 bar, spot 20300
PE_3 = "SIM:NIFTY:WEEKLY:19000:PE"  # DOWN flip on the 19000 bar, spot 19000


def _session_config(*, start="09:15", end="15:15", square_off="15:20") -> SessionConfig:
    return SessionConfig(
        timezone="Asia/Kolkata",
        start_time=start,
        end_time=end,
        square_off_time=square_off,
    )


def _dt(h: int, m: int, s: int = 0) -> datetime:
    """Thursday 2026-08-20 — an ordinary trading day."""
    return datetime(2026, 8, 20, h, m, s, tzinfo=IST)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id,
        instrument=security_id,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


def _underlying_ticks(closes: Sequence[float], *, start: datetime) -> list[Tick]:
    """One tick per 5-minute bucket, plus a trailing tick to close the last one.

    Bar ``i``'s signal is produced when tick ``i + 1`` is delivered — which is also
    the tick whose price becomes ``self._spot`` for that signal's strike.
    """
    ticks = [
        _tick(UNDERLYING, price, start + timedelta(minutes=5 * i + 1))
        for i, price in enumerate(closes)
    ]
    ticks.append(_tick(UNDERLYING, closes[-1], start + timedelta(minutes=5 * len(closes) + 1)))
    return ticks


def _contract_id(spot: float, option_type: OptionType) -> str:
    """The contract the engine will resolve for a signal taken at ``spot``.

    Mirrors the production rule rather than restating a constant: strike =
    ``round(spot / 50) * 50`` (``common.engine.selection.nearest_atm_strike``, Python's
    banker's rounding included), and the simulated resolver names contracts
    ``SIM:NIFTY:WEEKLY:<strike>:<CE|PE>``. Computing it is the point — the spot that
    matters is the one at *signal* time, which is the tick that closed the bar.
    """
    strike = int(round(spot / 50) * 50)
    return f"SIM:NIFTY:WEEKLY:{strike}:{option_type.value}"


def _insert_fill(ticks: list[Tick], after_index: int, security_id: str, price: float) -> None:
    """Insert an option tick 10s after ``ticks[after_index]`` to fill the pending entry
    the bar that just closed queued."""
    anchor = ticks[after_index].exchange_time
    ticks.insert(after_index + 1, _tick(security_id, price, anchor + timedelta(seconds=10)))


def _warmup_candles(session: MarketSession) -> list[Candle]:
    """75 completed buckets ending Wednesday 2026-08-19 15:15, all flat at 20000.

    Flat on purpose: with ``period=1`` the ATR is the bar's own range, so a flat series
    keeps the bands pinned at 20000 and produces **no** flips during warm-up. Every
    flip observed in a test therefore belongs to the live tape.
    """
    starts = (
        session_bucket_starts(session, date(2026, 8, 18), TIMEFRAME_MINUTES)
        + session_bucket_starts(session, date(2026, 8, 19), TIMEFRAME_MINUTES)
    )[-75:]
    return [
        Candle(
            security_id="13",
            instrument="NIFTY",
            open=20000.0,
            high=20000.0,
            low=20000.0,
            close=20000.0,
            volume=0,
            start_at=s,
            end_at=s + timedelta(minutes=TIMEFRAME_MINUTES),
        )
        for s in starts
    ]


def _build_engine(
    ticks: Sequence[Tick],
    *,
    session: SessionConfig | None = None,
    lot_size: int = DEFAULT_LOT_SIZE,
    strategy: SupertrendBuy1x1p2Strategy | None = None,
) -> tuple[TradingEngine, SupertrendBuy1x1p2Strategy, PositionManager]:
    session_cfg = session or _session_config()
    market_session = MarketSession(session_cfg)
    strategy = strategy or SupertrendBuy1x1p2Strategy()
    # Mirrors the real wiring (config_adapter + engine_worker._build):
    # PositionManager's own `lots` is what sizes every order, and the adapter reads
    # it from strategy_kwargs.lots_per_trade — the same 10 the strategy reports.
    positions = PositionManager(InMemoryGateway(slippage_points=0.0), lots=strategy.quantity_lots)
    candles = _warmup_candles(market_session)

    def _fetch(source, *, session, timeframe_minutes, lookback_sessions, now=None):
        assert source is _SOURCE
        return candles

    engine = TradingEngine(
        EngineConfig(timeframe="5m", session=session_cfg, warmup_from_history=True),
        feed=SimulatedFeed(list(ticks)),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=lot_size), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=UNDERLYING,
        warmup_manager=WarmupManager(_fetch, max_lookback_sessions=3),
        warmup_source=_SOURCE,
        clock=lambda: _dt(9, 15),
    )
    return engine, strategy, positions


def _full_tape() -> list[Tick]:
    """The three-entry tape: PE, reversal to CE, reversal to PE."""
    ticks = _underlying_ticks(LIVE_CLOSES, start=_dt(9, 15))
    _insert_fill(ticks, 1, PE_1, 120.0)  # 19500 bar closes on tick index 1
    _insert_fill(ticks, 3, CE_2, 140.0)  # 20200 bar closes on (shifted) index 3
    _insert_fill(ticks, 6, PE_3, 160.0)  # 19000 bar closes on (shifted) index 6
    return ticks


# ------------------------------------------------- 18.2 contract and quantity
def test_a_down_flip_buys_a_pe_and_an_up_flip_buys_a_ce():
    """Spec 18.2: "UP flip | CE selected", "DOWN flip | PE selected"."""
    engine, _, positions = _build_engine(_full_tape())
    engine.run()

    bought = [(t.contract.security_id, t.contract.option_type) for t in positions.trades]
    bought.extend((p.contract.security_id, p.contract.option_type) for p in positions.positions)
    assert bought == [
        (PE_1, OptionType.PE),
        (CE_2, OptionType.CE),
        (PE_3, OptionType.PE),
    ]


def test_the_atm_strike_is_recalculated_from_the_current_spot_at_every_entry():
    """Spec 18.2: "Spot moves before later signal | ATM recalculated from current
    spot", and section 8's "Contract selection must happen again on every new entry or
    reversal. Do not reuse a stale ATM contract from an earlier signal."

    Three entries, three different strikes, each matching the spot at its own signal.
    """
    engine, _, positions = _build_engine(_full_tape())
    engine.run()

    strikes = [t.contract.strike for t in positions.trades]
    strikes.extend(p.contract.strike for p in positions.positions)
    assert strikes == [20200.0, 20300.0, 19000.0]
    assert len(set(strikes)) == 3


@pytest.mark.parametrize("lot_size", [75, 65, 50])
def test_order_quantity_is_ten_lots_times_the_resolved_lot_size(lot_size: int):
    """Spec 18.2: "Normal entry | Quantity equals 10 x resolved lot size" and
    "Exchange lot size changes | Current reference-data lot size used".

    The lot size comes from the resolved contract, never from the strategy or a
    configured constant — so changing only the resolver changes the quantity.
    """
    engine, strategy, positions = _build_engine(_full_tape(), lot_size=lot_size)
    engine.run()

    assert strategy.quantity_lots == LOTS
    for trade in positions.trades:
        assert trade.contract.lot_size == lot_size
        assert trade.lots == LOTS
        assert trade.quantity == LOTS * lot_size
    (position,) = positions.positions
    assert position.quantity == LOTS * lot_size


def test_the_expiry_comes_from_the_resolver_not_from_the_strategy():
    """Spec 18.2: "Weekly expiry shifted by holiday | Exchange-listed valid expiry
    selected". The strategy names no expiry at all; whatever the resolver lists is
    what is traded. (The real ``DhanOptionChainResolver`` takes it from the daily
    instrument master, which already carries any holiday shift.)"""
    engine, _, positions = _build_engine(_full_tape())
    engine.run()

    expiries = {t.contract.expiry for t in positions.trades}
    expiries |= {p.contract.expiry for p in positions.positions}
    assert expiries == {"WEEKLY"}  # the simulated resolver's own listing


def test_an_entry_never_fills_from_a_cached_price():
    """Spec section 9: "Entry fills must use the platform's current fresh-quote/tick
    requirements; do not use a stale cached price merely to force a fill."

    The same tape with the option ticks removed: the flips still fire and queue a
    pending contract, but nothing ever fills, so the book stays empty.
    """
    engine, _, positions = _build_engine(_underlying_ticks(LIVE_CLOSES, start=_dt(9, 15)))
    engine.run()

    assert positions.positions == []
    assert positions.trades == []


# --------------------------------------------------------------- 18.3 reversal
def test_a_reversal_confirms_the_close_before_opening_the_replacement():
    """Spec 18.3: "CE open, fresh DOWN flip before cutoff | Confirm CE close, then buy
    fresh ATM PE" (and the PE->CE direction).

    Each reversal books a completed trade with ``OPPOSITE_SIGNAL`` *before* the
    replacement contract can fill, and the replacement is a freshly resolved strike,
    never the closed leg's.
    """
    engine, _, positions = _build_engine(_full_tape())
    engine.run()

    reasons = [t.exit_reason for t in positions.trades]
    assert reasons == [ExitReason.OPPOSITE_SIGNAL, ExitReason.OPPOSITE_SIGNAL]
    assert [t.contract.security_id for t in positions.trades] == [PE_1, CE_2]
    # PE -> CE -> PE: each replacement is a different contract from the one closed.
    assert positions.positions[0].contract.security_id == PE_3

    # The close really did precede the open, not merely appear before it in a list.
    close_times = [t.exit_time for t in positions.trades]
    assert close_times[0] < positions.trades[1].entry_time
    assert close_times[1] < positions.positions[0].entry_time


def test_never_more_than_one_open_position_at_any_tick():
    """Spec section 9: "Maximum one open strategy position at a time." Checked on
    every single tick, not just at the end."""
    ticks = _full_tape()
    engine, _, positions = _build_engine(ticks)
    seen: list[int] = []
    original = engine.on_tick

    def _recording(tick: Tick) -> None:
        original(tick)
        seen.append(len(positions.positions))

    # Replace the instance attribute, not the feed's callback: ``run()`` re-registers
    # ``self.on_tick`` with the feed itself, so a callback installed on the feed
    # beforehand is simply overwritten.
    engine.on_tick = _recording  # type: ignore[method-assign]
    engine.run()

    assert seen, "no ticks were delivered"
    assert max(seen) <= 1


def test_an_opposite_flip_after_the_cutoff_closes_but_does_not_reopen():
    """Spec 18.3: "Opposite flip after cutoff | Close if strategy rule requires;
    remain flat", and section 5's "At exactly or after 15:15, an existing position may
    still be exited, but an opposite signal must not open a replacement."

    The tape opens a PE well before the cutoff, then delivers the UP flip after it.
    """
    closes = [19500.0, 20200.0]
    ticks = _underlying_ticks(closes, start=_dt(15, 5))
    # bars: 15:05-15:10 (close 19500) closes at 15:11; 15:10-15:15 (close 20200)
    # closes at 15:16 — after the 15:15 cutoff.
    _insert_fill(ticks, 1, PE_1, 120.0)
    engine, _, positions = _build_engine(ticks)

    engine.run()

    assert [t.contract.security_id for t in positions.trades] == [PE_1]
    assert positions.trades[0].exit_reason is ExitReason.OPPOSITE_SIGNAL
    assert positions.positions == [], "a replacement was opened after the entry cutoff"


def test_no_entry_before_the_session_opens():
    """Spec 18.1: "Signal before 09:15 | No entry". Out-of-session ticks never reach
    the candle builder at all, so no bar and no signal exist."""
    ticks = _underlying_ticks(LIVE_CLOSES, start=_dt(8, 30))
    engine, strategy, positions = _build_engine(ticks)

    engine.run()

    assert strategy._candles_seen == 75  # warm-up only; no live bar was built
    assert positions.positions == []
    assert positions.trades == []


def test_the_mandatory_square_off_closes_an_open_position():
    """Spec 18.6: the hard square-off runs regardless of indicator state. The tape
    opens a PE, then delivers a tick past 15:20."""
    closes = [19500.0]
    ticks = _underlying_ticks(closes, start=_dt(15, 0))
    # The 15:00-15:05 bar closes on the trailing 15:06 tick, whose own price (19500)
    # is the spot the strike is resolved from.
    _insert_fill(ticks, 1, _contract_id(19500.0, OptionType.PE), 120.0)
    ticks.append(_tick(UNDERLYING, 19400.0, _dt(15, 20, 1)))
    engine, _, positions = _build_engine(ticks)

    engine.run()

    assert positions.positions == []
    assert [t.exit_reason for t in positions.trades] == [ExitReason.SQUARE_OFF]


# ------------------------------------------------------------ 18.3 no pyramiding
def test_consecutive_actionable_signals_always_alternate_side():
    """Why this strategy cannot pyramid in the first place: a SuperTrend flip is a
    *change* of direction, so two consecutive actionable signals can never name the
    same leg. The engine's same-leg dedupe (tested next) is a second line of defence,
    not the only one."""
    engine, _strategy, positions = _build_engine(_full_tape())
    engine.run()
    ordered = [t.contract.option_type for t in positions.trades]
    ordered.extend(p.contract.option_type for p in positions.positions)
    assert ordered == [OptionType.PE, OptionType.CE, OptionType.PE]
    for earlier, later in pairwise(ordered):
        assert earlier is not later


class _RepeatsTheSameEntry(SupertrendBuy1x1p2Strategy):
    """The real strategy with one behaviour forced: every completed bar emits the same
    BUY PE entry. Used only to reach the engine's same-leg dedupe, which the genuine
    flip logic above structurally cannot reach."""

    def on_candle(self, candle: OHLC, timestamp: datetime) -> StrategySignal | None:
        super().on_candle(candle, timestamp)
        return StrategySignal(
            action=SignalAction.ENTER,
            timestamp=timestamp,
            option_type=OptionType.PE,
            side=OrderSide.BUY,
            reason="forced duplicate for the dedupe test",
        )


def test_a_repeated_same_side_signal_does_not_pyramid():
    """Spec 18.3: "Duplicate same-side signal while open | No pyramiding". An ENTER
    naming the leg already open is a no-op: no second order, no second position, and
    no spurious close-and-reopen round trip."""
    ticks = _underlying_ticks([19500.0, 19400.0, 19300.0], start=_dt(9, 15))
    # The first bar closes on the 09:21 tick (price 19400), so that is the spot the
    # first — and only — entry resolves its strike from.
    first_contract = _contract_id(19400.0, OptionType.PE)
    _insert_fill(ticks, 1, first_contract, 120.0)
    engine, _, positions = _build_engine(ticks, strategy=_RepeatsTheSameEntry())

    engine.run()

    assert len(positions.positions) == 1
    assert positions.positions[0].contract.security_id == first_contract
    assert positions.trades == [], "the same-leg signal closed and reopened the position"
