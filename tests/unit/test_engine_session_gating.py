"""Pre-open ticks must not contaminate the live candle stream.

**A port of the one ``TradingEngine`` test in the reference repository's
``tests/test_session_candle_gating.py``** (Phase 3 Part 2b-i). That file holds
three tests; the other two construct ``MultiLegEngine`` and ``FixedStrikeEngine``,
which Phase 3 explicitly does not port, so they are excluded rather than stubbed
(deviation D22).

The test's shape is the reference's, including the ``__new__`` + hand-set
attributes: it deliberately bypasses ``__init__`` so it pins *exactly* which
private attributes ``_on_underlying_tick`` touches. That is worth keeping — it is
the closest thing the reference has to a written contract for that method, and it
would catch a port that quietly added a dependency.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from common.candles.builder import CandleBuilder
from common.engine.config import SessionConfig
from common.engine.engine import TradingEngine
from common.engine.session import MarketSession
from common.indicators.base import OHLC
from common.models import Tick

IST = ZoneInfo("Asia/Kolkata")


def _ts(hour: int, minute: int) -> datetime:
    return datetime(2026, 7, 16, hour, minute, tzinfo=IST)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    """The reference's ``Tick(security_id, ltp, timestamp)`` in this repository's model.

    ``common.models.Tick`` carries an instrument label and separates the exchange
    clock from ours, and it refuses a naive timestamp — so the three-positional
    form cannot come across literally. Nothing the test asserts depends on the
    extra fields.
    """
    return Tick(
        security_id=security_id,
        instrument=security_id,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


def _session() -> MarketSession:
    return MarketSession(
        SessionConfig(
            timezone="Asia/Kolkata",
            start_time="09:15",
            end_time="15:15",
            square_off_time="15:20",
        )
    )


def test_single_leg_ignores_pre_open_ticks_but_keeps_latest_spot() -> None:
    engine = TradingEngine.__new__(TradingEngine)
    engine.session = _session()
    engine.candles = CandleBuilder(5, security_id="INDEX", instrument="INDEX")
    engine._spot = None
    # Premium-candle tracking is opt-in (needs_option_candles); a bare __new__'d
    # engine skips __init__, so set the disabled defaults by hand.
    engine._option_candles = None
    engine._option_candle_contract_id = None
    engine._last_option_tick_ts = None
    engine._premium_gap_logged = False
    closed: list[tuple[OHLC, datetime]] = []
    engine._on_candle_close = lambda candle, ts: closed.append((candle, ts))  # type: ignore[method-assign]

    engine._on_underlying_tick(_tick("INDEX", 23970.30, _ts(9, 0)))
    engine._on_underlying_tick(_tick("INDEX", 24142.10, _ts(9, 10)))

    assert engine._spot == 24142.10
    assert engine.candles.current is None

    engine._on_underlying_tick(_tick("INDEX", 24141.65, _ts(9, 15)))
    engine._on_underlying_tick(_tick("INDEX", 24157.05, _ts(9, 19)))
    engine._on_underlying_tick(_tick("INDEX", 24157.00, _ts(9, 20)))

    assert len(closed) == 1
    candle, _ = closed[0]
    assert (candle.open, candle.high, candle.low, candle.close) == (
        24141.65,
        24157.05,
        24141.65,
        24157.05,
    )
