"""Warm-up handoff for ``supertrend_buy_1_1p2``, through a real ``TradingEngine``.

The unit-level companion (``tests/unit/test_supertrend_buy_1_1p2_warmup.py``) proves
what :class:`~common.warmup.manager.WarmupManager` *decides*. This file proves what
that decision actually does to the engine: mandated proofs 7 and 8 of the warm-up
requirement — a replay places no order however many flips it contains, and the first
live candle enters only on a genuine flip relative to the warmed state.

Fully real engine over a simulated tape, mirroring
``tests/integration/test_ema_cross_9_21_buy_engine.py`` and
``tests/integration/test_engine_warmup_end_to_end.py``: no monkeypatching of engine
internals, a hand-built ``WarmupManager``/``WarmupSource`` injected directly (a real
Dhan fetch is :mod:`common.warmup.historical`'s own concern, covered elsewhere).

Calendar: warm-up covers the 75 completed buckets ending Wednesday 2026-08-19 15:15 —
that session's 73 plus Tuesday 2026-08-18's last two, because a 09:15-15:20 lifecycle
contributes only 73 completed five-minute buckets and the 75-bucket trust floor
therefore spans sessions by design. The live tape is Thursday 2026-08-20's open.

Every number below was walked through the real ``SuperTrend(1, 1.2)`` before being
hard-coded: the warm-up series contains **ten** genuine flips and leaves the indicator
in an uptrend with its active band at 24178.0, so a live close of 24228.0 continues
that trend and one of 24128.0 flips it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from common.engine.config import EngineConfig, SessionConfig
from common.engine.engine import TradingEngine
from common.engine.feed import SimulatedFeed
from common.engine.models import OptionType, OrderSide
from common.engine.positions import InMemoryGateway, PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.engine.session import MarketSession
from common.indicators.base import OHLC
from common.indicators.supertrend import DOWNTREND, UPTREND
from common.models import Candle, Tick
from common.warmup.manager import WarmupManager
from common.warmup.session_buckets import session_bucket_starts
from common.warmup.source import WarmupSource
from strategies.intraday_options.supertrend_buy_1_1p2.strategy import SupertrendBuy1x1p2Strategy

IST = ZoneInfo("Asia/Kolkata")
UNDERLYING = "INDEX"
LOT_SIZE = 75
LOTS = 10
TIMEFRAME_MINUTES = 5

_SOURCE = WarmupSource(security_id="13", exchange_segment="IDX_I", instrument_type="INDEX")

#: The active SuperTrend band after the warm-up series below — verified, not derived.
WARMED_BAND = 24178.0
CONTINUATION_CLOSE = WARMED_BAND + 50.0  # 24228.0 — stays UP
FLIP_CLOSE = WARMED_BAND - 50.0  # 24128.0 — flips DOWN
#: round(24128 / 50) * 50 == 24150 — the ATM strike the flip resolves to.
FLIP_PE_CONTRACT = "SIM:NIFTY:WEEKLY:24150:PE"


def _session_config() -> SessionConfig:
    """This strategy's real session: entries 09:15-15:15, hard square-off 15:20."""
    return SessionConfig(
        timezone="Asia/Kolkata",
        start_time="09:15",
        end_time="15:15",
        square_off_time="15:20",
    )


def _dt(h: int, m: int, s: int = 0) -> datetime:
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
    """One tick per 5-minute bucket, plus a trailing tick to close the last one."""
    ticks = [
        _tick(UNDERLYING, price, start + timedelta(minutes=5 * i + 1))
        for i, price in enumerate(closes)
    ]
    ticks.append(_tick(UNDERLYING, closes[-1], start + timedelta(minutes=5 * len(closes) + 1)))
    return ticks


def _warmup_closes() -> list[float]:
    """A 75-bar zigzag that genuinely flips the SuperTrend ten times.

    The point is not the shape but the flip count: a replay that placed orders would
    place ten of them here, so "no trade" is a real assertion rather than a vacuous
    one on a series that never flipped.
    """
    return [
        24000.0 + (150.0 if (i // 7) % 2 == 0 else -150.0) + (i % 7) * 10.0 for i in range(75)
    ]


def _warmup_candles(session: MarketSession) -> list[Candle]:
    """The 75 completed buckets ending Wednesday 2026-08-19 15:15 — Wednesday's 73
    plus Tuesday's last two."""
    starts = (
        session_bucket_starts(session, date(2026, 8, 18), TIMEFRAME_MINUTES)
        + session_bucket_starts(session, date(2026, 8, 19), TIMEFRAME_MINUTES)
    )[-75:]
    closes = _warmup_closes()
    assert len(starts) == len(closes) == 75
    return [
        Candle(
            security_id="13",
            instrument="NIFTY",
            open=c,
            high=c,
            low=c,
            close=c,
            volume=0,
            start_at=s,
            end_at=s + timedelta(minutes=TIMEFRAME_MINUTES),
        )
        for s, c in zip(starts, closes, strict=True)
    ]


class _CountingGateway(InMemoryGateway):
    """An ordinary in-memory gateway that also records how often it was asked to
    trade — so "warm-up placed no order" can be asserted at the order path itself,
    not merely inferred from an empty position book."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.orders: list[tuple[str, str]] = []

    def buy(self, contract, lots, **kwargs):  # type: ignore[no-untyped-def]
        self.orders.append(("BUY", contract.security_id))
        return super().buy(contract, lots, **kwargs)

    def sell(self, contract, lots, **kwargs):  # type: ignore[no-untyped-def]
        self.orders.append(("SELL", contract.security_id))
        return super().sell(contract, lots, **kwargs)


def _build_engine(
    ticks: Sequence[Tick],
    *,
    clock_at: datetime,
) -> tuple[TradingEngine, SupertrendBuy1x1p2Strategy, PositionManager, _CountingGateway]:
    session_cfg = _session_config()
    session = MarketSession(session_cfg)
    strategy = SupertrendBuy1x1p2Strategy()
    gateway = _CountingGateway(slippage_points=0.0)
    positions = PositionManager(gateway, lots=strategy.quantity_lots)

    candles = _warmup_candles(session)

    def _fetch(source, *, session, timeframe_minutes, lookback_sessions, now=None):
        assert source is _SOURCE
        return candles

    engine = TradingEngine(
        EngineConfig(timeframe="5m", session=session_cfg, warmup_from_history=True),
        feed=SimulatedFeed(list(ticks)),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=LOT_SIZE), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=UNDERLYING,
        warmup_manager=WarmupManager(_fetch, max_lookback_sessions=3),
        warmup_source=_SOURCE,
        clock=lambda: clock_at,
    )
    return engine, strategy, positions, gateway


# ------------------------------------------------ 7. replay places no order
def test_warmup_replay_seeds_state_and_places_no_order():
    """Mandated proof 7. Ten genuine flips are replayed through the very same
    ``on_candle`` the live path uses; not one of them reaches the order path.

    The live tape here deliberately never closes a bucket (a single tick), so the only
    thing that could possibly have traded is the replay itself.
    """
    engine, strategy, positions, gateway = _build_engine(
        [_tick(UNDERLYING, 24200.0, _dt(9, 16))], clock_at=_dt(9, 15)
    )

    engine.run()

    assert strategy._candles_seen == 75  # every replayed candle really was fed through
    assert gateway.orders == []  # the order path was never reached
    assert positions.positions == []
    assert positions.trades == []
    # ...and the replay was nonetheless trusted, so this is not "no trade because
    # everything was blocked".
    assert engine.entries_blocked is None
    assert strategy._context_trusted is True
    assert strategy._supertrend.state.trend == UPTREND
    assert strategy._supertrend.state.line == WARMED_BAND


def test_the_replayed_series_really_does_flip_ten_times():
    """Guards proof 7 against silently becoming vacuous: if a future edit flattened
    the warm-up series, "no orders" would still pass while proving nothing."""
    strategy = SupertrendBuy1x1p2Strategy()
    strategy.on_warmup_complete(context_trusted=True)
    flips = 0
    for close in _warmup_closes():
        signal = strategy.on_candle(OHLC(high=close, low=close, close=close), _dt(9, 15))
        flips += 1 if signal is not None else 0
    assert flips == 10


def test_an_untrusted_replay_blocks_entries_for_the_whole_day():
    """The other half of the handoff: coverage that is not verified-complete latches
    entries off, rather than seeding a trend the first live candle could flip."""
    session_cfg = _session_config()
    session = MarketSession(session_cfg)
    strategy = SupertrendBuy1x1p2Strategy()
    gateway = _CountingGateway(slippage_points=0.0)
    positions = PositionManager(gateway, lots=strategy.quantity_lots)
    only_one = _warmup_candles(session)[-1:]  # a single valid, current candle

    def _fetch(source, *, session, timeframe_minutes, lookback_sessions, now=None):
        return only_one

    engine = TradingEngine(
        EngineConfig(timeframe="5m", session=session_cfg, warmup_from_history=True),
        feed=SimulatedFeed(_underlying_ticks([FLIP_CLOSE], start=_dt(9, 15))),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=LOT_SIZE), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=UNDERLYING,
        warmup_manager=WarmupManager(_fetch, max_lookback_sessions=3),
        warmup_source=_SOURCE,
        clock=lambda: _dt(9, 15),
    )

    engine.run()

    assert engine.entries_blocked is not None
    assert "PARTIAL" in engine.entries_blocked
    assert strategy._context_trusted is False
    assert gateway.orders == []
    assert positions.positions == []


# --------------------------------- 8. the first live candle needs a real flip
def test_the_first_live_candle_does_not_enter_without_a_genuine_flip():
    """Mandated proof 8, negative half. The warmed context is UP; a first live candle
    that closes above the active band continues it and must not trade."""
    engine, strategy, positions, gateway = _build_engine(
        _underlying_ticks([CONTINUATION_CLOSE], start=_dt(9, 15)), clock_at=_dt(9, 15)
    )

    engine.run()

    assert strategy._candles_seen == 76  # 75 replayed + 1 live
    assert strategy._supertrend.state.trend == UPTREND
    assert strategy._supertrend.state.flipped is False
    assert gateway.orders == []
    assert positions.positions == []
    assert engine.entries_blocked is None  # not blocked — simply no signal


def test_the_first_live_candle_enters_on_a_genuine_flip():
    """Mandated proof 8, positive half. Same warmed UP context; a first live candle
    that closes below the active band is a real DOWN flip and buys the ATM weekly PE
    at ten lots times the resolved lot size."""
    ticks = _underlying_ticks([FLIP_CLOSE], start=_dt(9, 15))
    # The 09:15-09:20 bucket closes when the trailing 09:21 tick arrives; the entry
    # then fills on the first fresh tick of the newly subscribed contract.
    ticks.insert(2, _tick(FLIP_PE_CONTRACT, 120.0, _dt(9, 21, 10)))

    engine, strategy, positions, gateway = _build_engine(ticks, clock_at=_dt(9, 15))

    engine.run()

    assert strategy._candles_seen == 76
    assert strategy._supertrend.state.trend == DOWNTREND
    assert strategy._supertrend.state.flipped is True
    assert gateway.orders == [("BUY", FLIP_PE_CONTRACT)]

    (position,) = positions.positions
    assert position.contract.security_id == FLIP_PE_CONTRACT
    assert position.contract.option_type is OptionType.PE
    assert position.side is OrderSide.BUY
    assert position.lots == LOTS
    assert position.quantity == LOTS * LOT_SIZE
    assert position.entry_price == 120.0
