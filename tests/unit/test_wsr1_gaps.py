"""Phase 4a: unexplained gaps, keyed acknowledgements and the 520-bar window
(spec 6.1 v1.2b, v1.2e), plus the calendar helpers the fill model uses."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from strategies.positional_stocks.wsr1_weekly_stochrsi.gaps import (
    block_window_start,
    blocking_gaps,
    gap_ratio,
    scan,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.inputs import (
    InputFileError,
    load_gap_acknowledgements,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    DailyBar,
    GapAcknowledgement,
    WeeklyBar,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import (
    CalendarError,
    TradingCalendar,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.weekly_bars import iso_key

CONFIG = Path(__file__).resolve().parents[2] / "config"
CALENDAR = TradingCalendar.from_config(CONFIG)


def _daily(closes: list[float], start: date = date(2020, 1, 6)) -> list[DailyBar]:
    return [DailyBar(start + timedelta(days=i), c, c, c, c, 1000.0) for i, c in enumerate(closes)]


def _weekly(n: int, last_friday: date = date(2026, 9, 18)) -> list[WeeklyBar]:
    bars = []
    for i in range(n):
        friday = last_friday - timedelta(weeks=n - 1 - i)
        year, week = iso_key(friday)
        bars.append(WeeklyBar(friday, year, week, 100, 100, 100, 100, friday, sessions=5))
    return bars


# ------------------------------------------------------------------- scan
def test_scan_reports_15_percent_and_flags_30_percent() -> None:
    gaps = scan("X", _daily([100.0, 110.0, 128.0, 90.0, 120.0]))
    # 110 -> 128 is +16.4% (reported); 128 -> 90 is -29.7% (reported);
    # 90 -> 120 is +33.3% (flagged).
    assert [(g.session.day, g.flagged) for g in gaps] == [(8, False), (9, False), (10, True)]
    assert gaps[-1].ratio == Decimal("1.3333")


def test_a_move_below_15_percent_is_not_a_gap() -> None:
    assert scan("X", _daily([100.0, 114.9, 100.0])) == []


def test_gap_ratio_is_the_acknowledgement_key() -> None:
    # MOTHERSON's 2024-04-30 boundary: 130.80 -> 87.47.
    assert gap_ratio(130.80, 87.47) == Decimal("0.6687")


# --------------------------------------------------- acknowledgement keys
_GAP = scan("X", _daily([100.0, 50.0]))[0]


def _ack(
    symbol: str = "X", session: date = _GAP.session, ratio: str = "0.5000"
) -> GapAcknowledgement:
    return GapAcknowledgement(symbol, session, Decimal(ratio), date(2026, 9, 1))


def test_an_acknowledged_gap_does_not_block() -> None:
    assert blocking_gaps([_GAP], [_ack()], window_start=date.min) == []


@pytest.mark.parametrize(
    "ack",
    [
        _ack(ratio="0.6667"),  # same session restated to another ratio
        _ack(session=_GAP.session + timedelta(days=1)),  # another session
        _ack(symbol="Y"),  # another symbol
    ],
    ids=["restated-ratio", "other-session", "other-symbol"],
)
def test_an_acknowledgement_is_keyed_to_symbol_session_and_ratio(ack: GapAcknowledgement) -> None:
    assert blocking_gaps([_GAP], [ack], window_start=date.min) == [_GAP]


def test_a_reported_gap_never_blocks() -> None:
    reported = scan("X", _daily([100.0, 120.0]))[0]
    assert not reported.flagged
    assert blocking_gaps([reported], [], window_start=date.min) == []


# ------------------------------------------------------ the 520-bar window
def test_only_gaps_in_the_most_recent_520_weekly_bars_block() -> None:
    weekly = _weekly(600)
    start = block_window_start(weekly)
    assert start == date.fromisocalendar(*weekly[-520].iso_key, 1)
    # _daily's gap falls on the day after its start.
    inside = scan("X", _daily([100.0, 50.0], start=start - timedelta(days=1)))[0]
    outside = scan("X", _daily([100.0, 50.0], start=start - timedelta(days=2)))[0]
    assert inside.session == start and outside.session == start - timedelta(days=1)
    assert blocking_gaps([inside, outside], [], window_start=start) == [inside]


def test_a_short_history_is_entirely_inside_the_window() -> None:
    weekly = _weekly(300)
    assert block_window_start(weekly) == date.fromisocalendar(*weekly[0].iso_key, 1)
    assert block_window_start([]) == date.min


# ----------------------------------------------------------------- loader
def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "gap_acknowledgements.csv"
    path.write_text("symbol,gap_session,ratio,acknowledged_on,note\n" + body, encoding="utf-8")
    return path


def test_the_committed_file_is_header_only() -> None:
    assert (
        load_gap_acknowledgements(CONFIG / "positional_stocks" / "gap_acknowledgements.csv") == ()
    )


def test_the_loader_reads_and_rounds_a_row(tmp_path: Path) -> None:
    (ack,) = load_gap_acknowledgements(
        _write(tmp_path, "motherson,2024-04-30,0.66872,2026-09-24,partial back-adjustment\n")
    )
    assert ack == GapAcknowledgement(
        "MOTHERSON",
        date(2024, 4, 30),
        Decimal("0.6687"),
        date(2026, 9, 24),
        "partial back-adjustment",
    )


@pytest.mark.parametrize(
    "body",
    [
        "X,2024-04-30,abc,2026-09-24,\n",
        "X,2024-04-30,-1,2026-09-24,\n",
        "X,30/04/2024,0.5,2026-09-24,\n",
        "X,2024-04-30,0.5,2026-09-24,\nX,2024-04-30,0.6,2026-09-24,\n",
        "X,2024-04-30,,2026-09-24,\n",
    ],
    ids=["not-a-number", "negative", "ambiguous-date", "duplicate", "blank-ratio"],
)
def test_the_loader_fails_closed(tmp_path: Path, body: str) -> None:
    with pytest.raises(InputFileError):
        load_gap_acknowledgements(_write(tmp_path, body))


def test_a_missing_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(InputFileError):
        load_gap_acknowledgements(tmp_path / "absent.csv")


# ----------------------------------------------------- calendar helpers
def test_first_session_is_monday_or_the_first_non_holiday() -> None:
    assert CALENDAR.first_session((2026, 38)) == date(2026, 9, 14 + 1)  # Mon 14 Sep: holiday
    assert CALENDAR.first_session((2026, 39)) == date(2026, 9, 21)
    # Mon 26 Jan 2026 is Republic Day.
    assert CALENDAR.first_session((2026, 5)) == date(2026, 1, 27)


def test_next_session_after_skips_weekends_and_holidays() -> None:
    assert CALENDAR.next_session_after(date(2026, 9, 18)) == date(2026, 9, 21)
    assert CALENDAR.next_session_after(date(2026, 1, 23)) == date(2026, 1, 27)
    assert CALENDAR.next_session_after(date(2026, 9, 11)) == date(2026, 9, 15)


def test_a_week_with_no_trading_weekday_raises() -> None:
    closed = TradingCalendar.from_holidays(
        [(date(2026, 3, 2) + timedelta(days=d)).isoformat() for d in range(5)]
    )
    with pytest.raises(CalendarError):
        closed.first_session((2026, 10))
