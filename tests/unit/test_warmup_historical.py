"""Phase 4 Part 4. :mod:`common.warmup.historical` -- response parsing,
frozen-``Candle`` aggregation, prior-trading-day walking, and the range fetch.
No network anywhere in this file (the Dhan client is a hand-built stub).

Two fail-first demonstrations, matching the plan:

* ``test_aggregate_candles_matches_candlebuilder_bucketing`` is what proves
  "warm-up and live candles bucket identically" is a checked property rather
  than an assertion about shared code -- it cross-checks
  :func:`~common.warmup.historical.aggregate_candles` against
  :class:`~common.candles.builder.CandleBuilder`'s own bucketing on an
  equivalent price series.
* ``test_fetch_warmup_candles_range_builds_full_datetime_from_and_to`` targets
  the reference implementation's own bug directly: it spies on what reaches
  the client and fails if a future edit reverts to passing a bare date/string
  instead of a full ``datetime``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common.candles.builder import CandleBuilder
from common.engine.config import SessionConfig
from common.engine.session import MarketSession
from common.models import Candle
from common.warmup.historical import (
    _prior_trading_day,
    aggregate_candles,
    fetch_warmup_candles_range,
    parse_intraday_response,
)

_TZ = ZoneInfo("Asia/Kolkata")


def _session(*, holidays: tuple[str, ...] = ()) -> MarketSession:
    return MarketSession(
        SessionConfig(
            start_time="09:15", end_time="15:15", square_off_time="15:20", holidays=holidays
        )
    )


# --------------------------------------------------------------------------
# parse_intraday_response
# --------------------------------------------------------------------------


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "open": [100.0, 101.0],
        "high": [101.0, 102.0],
        "low": [99.0, 100.5],
        "close": [100.5, 101.5],
        "volume": [10, 12],
        "timestamp": [1785567300, 1785567360],  # two consecutive minutes
    }
    payload.update(overrides)
    return payload


def test_parse_intraday_response_top_level_arrays() -> None:
    candles = parse_intraday_response(_payload(), "Asia/Kolkata")
    assert len(candles) == 2
    assert all(c.start_at.tzinfo is not None for c in candles)
    assert candles[0].open == 100.0
    assert candles[1].close == 101.5


def test_parse_intraday_response_data_nested_fallback() -> None:
    nested = {"status": "success", "data": _payload()}
    candles = parse_intraday_response(nested, "Asia/Kolkata")
    assert len(candles) == 2


def test_parse_intraday_response_raises_on_missing_arrays() -> None:
    with pytest.raises(ValueError, match="failure"):
        parse_intraday_response({"status": "failure", "remarks": "bad security id"}, "Asia/Kolkata")


def test_parse_intraday_response_raises_on_non_dict() -> None:
    with pytest.raises(ValueError):
        parse_intraday_response([], "Asia/Kolkata")  # type: ignore[arg-type]


def test_parse_intraday_response_skips_unparseable_rows() -> None:
    payload = _payload(close=["not-a-number", 101.5])
    candles = parse_intraday_response(payload, "Asia/Kolkata")
    assert len(candles) == 1
    assert candles[0].close == 101.5


def test_parse_intraday_response_skips_an_internally_inconsistent_row() -> None:
    # high < low on the first row -- Candle's own __post_init__ raises, and
    # that is caught the same way an unparseable field is.
    payload = _payload(high=[90.0, 102.0])  # first row: high 90 < low 99
    candles = parse_intraday_response(payload, "Asia/Kolkata")
    assert len(candles) == 1
    assert candles[0].open == 101.0


def test_parse_intraday_response_sorts_by_start_at() -> None:
    payload = _payload(timestamp=[1785567360, 1785567300])  # out of order
    candles = parse_intraday_response(payload, "Asia/Kolkata")
    assert candles[0].start_at < candles[1].start_at


def test_parse_intraday_response_missing_timestamp_key_raises() -> None:
    payload = _payload()
    del payload["timestamp"]
    with pytest.raises(ValueError, match="timestamp"):
        parse_intraday_response(payload, "Asia/Kolkata")


# --------------------------------------------------------------------------
# aggregate_candles
# --------------------------------------------------------------------------


def _one_min(start: datetime, o: float, h: float, low: float, c: float, volume: int = 10) -> Candle:
    return Candle(
        security_id="",
        instrument="",
        open=o,
        high=h,
        low=low,
        close=c,
        volume=volume,
        start_at=start,
        end_at=start + timedelta(minutes=1),
    )


def test_aggregate_candles_open_high_low_close_volume_rules() -> None:
    base = datetime(2026, 8, 3, 9, 15, tzinfo=_TZ)
    bars = [
        _one_min(base, 100.0, 102.0, 99.0, 101.0, volume=10),
        _one_min(base + timedelta(minutes=1), 101.0, 103.0, 100.5, 102.5, volume=12),
        _one_min(base + timedelta(minutes=2), 102.5, 104.0, 101.0, 103.0, volume=8),
    ]
    [agg] = aggregate_candles(bars, 5, security_id="13", instrument="NIFTY")
    assert agg.open == 100.0  # first sub-candle's open
    assert agg.high == 104.0  # max
    assert agg.low == 99.0  # min
    assert agg.close == 103.0  # last sub-candle's close
    assert agg.volume == 30  # sum
    assert agg.security_id == "13"
    assert agg.instrument == "NIFTY"
    assert agg.start_at == base
    assert agg.end_at == base + timedelta(minutes=5)


def test_aggregate_candles_rejects_non_positive_interval() -> None:
    with pytest.raises(ValueError):
        aggregate_candles([], 0, security_id="13", instrument="NIFTY")


def test_aggregate_candles_empty_input_returns_empty() -> None:
    assert aggregate_candles([], 5, security_id="13", instrument="NIFTY") == []


def test_aggregate_candles_matches_candlebuilder_bucketing() -> None:
    """Cross-checked against CandleBuilder's own bucketing on an equivalent
    price series, so "identical to the live path" is proven rather than
    assumed from both using floor_to_interval. Volume is not compared: the
    two paths source it differently (Dhan's reported per-bar volume vs. a
    tick-level delta the live builder is fed), which is a semantic
    difference, not a bucketing one.
    """
    base = datetime(2026, 8, 3, 9, 15, tzinfo=_TZ)
    prices = [
        (100.0, 102.0, 99.0, 101.0),
        (101.0, 103.0, 100.5, 102.5),
        (102.5, 104.0, 101.0, 103.0),
        (103.0, 103.5, 100.0, 100.5),
        (100.5, 101.0, 98.0, 99.0),
    ]
    one_min_bars = [
        _one_min(base + timedelta(minutes=i), o, h, low, c)
        for i, (o, h, low, c) in enumerate(prices)
    ]
    [agg] = aggregate_candles(one_min_bars, 5, security_id="13", instrument="NIFTY")

    builder = CandleBuilder(5, security_id="13", instrument="NIFTY")
    for i, (o, h, low, c) in enumerate(prices):
        bucket_start = base + timedelta(minutes=i)
        for offset, price in enumerate((o, h, low, c)):
            builder.add(price, bucket_start + timedelta(seconds=offset * 10))
    live = builder.flush()

    assert live is not None
    assert agg.start_at == live.start_at
    assert agg.end_at == live.end_at
    assert agg.open == live.open
    assert agg.high == live.high
    assert agg.low == live.low
    assert agg.close == live.close


# --------------------------------------------------------------------------
# _prior_trading_day
# --------------------------------------------------------------------------


def test_prior_trading_day_skips_weekend() -> None:
    session = _session()
    monday = datetime(2026, 8, 3, tzinfo=_TZ).date()  # a Monday
    assert (
        _prior_trading_day(session, monday, 1) == datetime(2026, 7, 31, tzinfo=_TZ).date()
    )  # Friday


def test_prior_trading_day_skips_configured_holiday() -> None:
    session = _session(holidays=("2026-07-31",))  # Friday before the Monday
    monday = datetime(2026, 8, 3, tzinfo=_TZ).date()
    assert (
        _prior_trading_day(session, monday, 1) == datetime(2026, 7, 30, tzinfo=_TZ).date()
    )  # Thursday


def test_prior_trading_day_multiple_sessions_back() -> None:
    session = _session()
    monday = datetime(2026, 8, 3, tzinfo=_TZ).date()
    assert (
        _prior_trading_day(session, monday, 2) == datetime(2026, 7, 30, tzinfo=_TZ).date()
    )  # Thursday


def test_prior_trading_day_never_raises_naive_datetime_error() -> None:
    # Fail-first target: proves the combine()-built probe stays tz-aware, so
    # MarketSession.is_trading_day never sees a naive datetime.
    session = _session()
    day = datetime(2026, 8, 3, tzinfo=_TZ).date()
    result = _prior_trading_day(session, day, 3)
    assert result < day


# --------------------------------------------------------------------------
# fetch_warmup_candles_range
# --------------------------------------------------------------------------


class _StubClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def fetch_intraday(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.response


def _timestamps_for(day: str, minutes: list[str]) -> list[int]:
    return [
        int(datetime.fromisoformat(f"{day}T{m}:00").replace(tzinfo=_TZ).timestamp())
        for m in minutes
    ]


def test_fetch_warmup_candles_range_not_a_trading_day_returns_empty() -> None:
    session = _session()
    client = _StubClient({})
    sunday = datetime(2026, 8, 2, 10, 0, tzinfo=_TZ)  # a Sunday
    result = fetch_warmup_candles_range(
        client,
        security_id="13",
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        session=session,
        timeframe_minutes=5,
        now=sunday,
    )
    assert result == []
    assert client.calls == []


def test_fetch_warmup_candles_range_excludes_the_forming_bucket() -> None:
    session = _session()
    now = datetime(2026, 8, 3, 9, 27, tzinfo=_TZ)  # mid-bucket (09:25-09:30 still forming)
    minutes = ["09:15", "09:20", "09:25"]
    payload = {
        "open": [100.0, 101.0, 102.0],
        "high": [101.0, 102.0, 103.0],
        "low": [99.0, 100.0, 101.0],
        "close": [100.5, 101.5, 102.5],
        "volume": [10, 10, 10],
        "timestamp": _timestamps_for("2026-08-03", minutes),
    }
    client = _StubClient(payload)
    result = fetch_warmup_candles_range(
        client,
        security_id="13",
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        session=session,
        timeframe_minutes=5,
        now=now,
    )
    # 09:15 candle (aggregated at 5m, covering :15-:20) is complete; 09:25
    # (covering :25-:30, the bucket "now" falls inside) must be excluded.
    assert all(c.start_at < datetime(2026, 8, 3, 9, 25, tzinfo=_TZ) for c in result)


def test_fetch_warmup_candles_range_builds_full_datetime_from_and_to() -> None:
    """Fail-first target for the reference's own bug: from_at/to_at must reach
    the client as full datetimes, not bare dates/strings.
    """
    session = _session()
    now = datetime(2026, 8, 3, 9, 30, tzinfo=_TZ)
    client = _StubClient(
        {"open": [], "high": [], "low": [], "close": [], "volume": [], "timestamp": []}
    )
    fetch_warmup_candles_range(
        client,
        security_id="13",
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        session=session,
        timeframe_minutes=5,
        lookback_sessions=1,
        now=now,
    )
    assert len(client.calls) == 1
    call = client.calls[0]
    assert isinstance(call["from_at"], datetime)
    assert isinstance(call["to_at"], datetime)
    assert call["to_at"] == now
    assert call["from_at"].tzinfo is not None


def test_fetch_warmup_candles_range_raises_on_client_failure() -> None:
    class _RaisingClient:
        def fetch_intraday(self, **kwargs: object) -> dict[str, object]:
            raise RuntimeError("boom")

    session = _session()
    now = datetime(2026, 8, 3, 9, 30, tzinfo=_TZ)
    with pytest.raises(RuntimeError, match="boom"):
        fetch_warmup_candles_range(
            _RaisingClient(),
            security_id="13",
            exchange_segment="IDX_I",
            instrument_type="INDEX",
            session=session,
            timeframe_minutes=5,
            now=now,
        )
