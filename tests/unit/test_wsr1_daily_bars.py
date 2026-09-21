"""Phase 1. Parsing Dhan's daily-candle response into :class:`DailyBar` (spec 6.1).

The wire shape is the one a real call returned on 2026-09-21: top-level
parallel arrays ``open``/``high``/``low``/``close``/``volume``/``timestamp``,
timestamps in epoch seconds.

The timezone tests are the ones with consequences. A session stamped at its
start, read in the host's zone, can land on the neighbouring calendar date and
therefore in the neighbouring ISO week — which would silently move a bar from
one weekly candle to another. Every date this parser produces is an IST
calendar date regardless of ``TZ``.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from strategies.positional_stocks.wsr1_weekly_stochrsi.daily_bars import (
    DailyResponseError,
    parse_daily_response,
)

IST = ZoneInfo("Asia/Kolkata")


def _stamp(day: date, hour: int = 9, minute: int = 15) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST).timestamp())


def _response(*sessions: tuple[date, float, float, float, float, float]) -> dict[str, Any]:
    return {
        "open": [s[1] for s in sessions],
        "high": [s[2] for s in sessions],
        "low": [s[3] for s in sessions],
        "close": [s[4] for s in sessions],
        "volume": [s[5] for s in sessions],
        "timestamp": [_stamp(s[0]) for s in sessions],
    }


_THREE = (
    (date(2026, 9, 16), 106.0, 110.0, 98.0, 99.0, 1000.0),
    (date(2026, 9, 17), 99.0, 105.0, 97.0, 104.0, 1100.0),
    (date(2026, 9, 18), 104.0, 112.0, 103.0, 111.0, 1200.0),
)


# ------------------------------------------------------------------- parsing
def test_the_verified_wire_shape_parses_into_daily_bars() -> None:
    bars = parse_daily_response(_response(*_THREE))

    assert len(bars) == 3
    assert bars[0].session == date(2026, 9, 16)
    assert (bars[0].open, bars[0].high, bars[0].low, bars[0].close) == (106.0, 110.0, 98.0, 99.0)
    assert bars[0].volume == 1000.0


def test_bars_come_back_in_ascending_session_order() -> None:
    shuffled = _response(_THREE[2], _THREE[0], _THREE[1])

    bars = parse_daily_response(shuffled)

    assert [bar.session for bar in bars] == [
        date(2026, 9, 16),
        date(2026, 9, 17),
        date(2026, 9, 18),
    ]


def test_a_data_nested_body_is_also_accepted() -> None:
    """Kept from ``parse_intraday_response``: one ``isinstance`` covers a
    wrapper shape neither endpoint is documented to rule out."""
    bars = parse_daily_response({"data": _response(*_THREE)})

    assert len(bars) == 3


def test_an_empty_but_well_formed_response_is_an_empty_series_not_an_error() -> None:
    """A symbol with no sessions in the range is a fact, not a failure."""
    assert (
        parse_daily_response(
            {"open": [], "high": [], "low": [], "close": [], "volume": [], "timestamp": []}
        )
        == []
    )


def test_a_missing_volume_array_defaults_to_zero() -> None:
    payload = _response(*_THREE)
    del payload["volume"]

    bars = parse_daily_response(payload)

    assert [bar.volume for bar in bars] == [0.0, 0.0, 0.0]


def test_traded_value_is_close_times_volume() -> None:
    """Spec 4.1 says to compute it this way when the source gives no traded
    value, and Dhan's daily candle does not."""
    bar = parse_daily_response(_response(_THREE[0]))[0]

    assert bar.traded_value == pytest.approx(99.0 * 1000.0)


# --------------------------------------------------------------- resilience
def test_a_row_with_an_impossible_ohlc_is_skipped_not_fatal() -> None:
    """Dhan has returned internally inconsistent rows. ``DailyBar``'s own
    validation catches them, and one bad row must not cost the symbol."""
    payload = _response(*_THREE)
    payload["high"][1] = 1.0  # high below low

    bars = parse_daily_response(payload)

    assert [bar.session for bar in bars] == [date(2026, 9, 16), date(2026, 9, 18)]


def test_a_row_with_an_unparseable_price_is_skipped() -> None:
    payload = _response(*_THREE)
    payload["close"][0] = "n/a"

    assert len(parse_daily_response(payload)) == 2


def test_a_row_with_a_null_price_is_skipped() -> None:
    payload = _response(*_THREE)
    payload["open"][2] = None

    assert len(parse_daily_response(payload)) == 2


def test_a_zero_price_is_skipped_rather_than_treated_as_a_real_quote() -> None:
    payload = _response(*_THREE)
    payload["low"][0] = 0.0

    assert len(parse_daily_response(payload)) == 2


def test_ragged_arrays_are_truncated_to_the_shortest() -> None:
    payload = _response(*_THREE)
    payload["close"] = payload["close"][:2]

    assert len(parse_daily_response(payload)) == 2


def test_a_repeated_session_collapses_to_one_bar() -> None:
    """Two bars for one day would double that day's weight in a weekly
    aggregate, and its high and low would count twice."""
    payload = _response(_THREE[0], _THREE[0], _THREE[1])

    bars = parse_daily_response(payload)

    assert len(bars) == 2
    assert [bar.session for bar in bars] == [date(2026, 9, 16), date(2026, 9, 17)]


# ------------------------------------------------------------ failure modes
def test_a_body_with_no_candle_arrays_raises() -> None:
    """ "The request did not return data" must never be mistaken for "this
    symbol had no sessions"."""
    with pytest.raises(DailyResponseError, match="no candle arrays"):
        parse_daily_response({"status": "failure", "remarks": "rate limited"})


def test_the_failure_message_carries_dhans_own_status_and_remarks() -> None:
    with pytest.raises(DailyResponseError) as caught:
        parse_daily_response({"status": "failure", "remarks": "DH-905"})

    assert "failure" in str(caught.value)
    assert "DH-905" in str(caught.value)


def test_a_body_with_closes_but_no_timestamps_raises() -> None:
    with pytest.raises(DailyResponseError, match="timestamp"):
        parse_daily_response({"close": [100.0], "open": [100.0]})


def test_a_non_dict_response_raises() -> None:
    with pytest.raises(DailyResponseError, match="Unexpected daily response type"):
        parse_daily_response([1, 2, 3])  # type: ignore[arg-type]


def test_the_label_appears_in_the_failure_message() -> None:
    with pytest.raises(DailyResponseError, match="RELIANCE"):
        parse_daily_response({"status": "failure"}, label="RELIANCE")


# --------------------------------------------------------------- timezone
def test_sessions_are_dated_in_ist_whatever_the_host_zone_is() -> None:
    """A session stamped 09:15 IST is 03:45 UTC the same day and 23:45 the
    *previous* day in New York. Read in the host zone it would fall in the
    wrong ISO week."""
    payload = _response(*_THREE)
    original = os.environ.get("TZ")
    try:
        for zone in ("UTC", "America/New_York", "Asia/Tokyo", "Pacific/Auckland"):
            os.environ["TZ"] = zone
            time.tzset()
            bars = parse_daily_response(payload)
            assert [bar.session for bar in bars] == [
                date(2026, 9, 16),
                date(2026, 9, 17),
                date(2026, 9, 18),
            ], f"host TZ={zone} changed the session dates"
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        time.tzset()


def test_a_session_stamped_just_after_ist_midnight_keeps_its_own_date() -> None:
    """00:15 IST is 18:45 UTC the previous day — the boundary case."""
    payload = {
        "open": [100.0],
        "high": [101.0],
        "low": [99.0],
        "close": [100.0],
        "volume": [1.0],
        "timestamp": [_stamp(date(2026, 9, 18), hour=0, minute=15)],
    }

    assert parse_daily_response(payload)[0].session == date(2026, 9, 18)


def test_a_session_stamped_late_in_the_ist_evening_keeps_its_own_date() -> None:
    """23:45 IST is 18:15 UTC the same day, but tomorrow in Auckland."""
    payload = {
        "open": [100.0],
        "high": [101.0],
        "low": [99.0],
        "close": [100.0],
        "volume": [1.0],
        "timestamp": [_stamp(date(2026, 9, 18), hour=23, minute=45)],
    }

    assert parse_daily_response(payload)[0].session == date(2026, 9, 18)
