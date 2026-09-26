"""Phase 4a-fix: gap thresholds on the exact decimal ratio (spec 6.1 v1.2j).

As floats, 130.26 / 100.20 - 1 is 0.29999999999999993, so a move of exactly
30% went unflagged. Each close now goes through its shortest repr into a
Decimal, and the threshold is compared on that exact ratio.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from strategies.positional_stocks.wsr1_weekly_stochrsi.gaps import (
    FLAG_MOVE,
    REPORT_MOVE,
    exact_move,
    scan,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import DailyBar


def _daily(closes: list[float], start: date = date(2020, 1, 6)) -> list[DailyBar]:
    return [DailyBar(start + timedelta(days=i), c, c, c, c, 1000.0) for i, c in enumerate(closes)]


def test_the_float_difference_really_misses_this_move() -> None:
    # The premise of the fix: the old float comparison said "below 30%".
    assert abs(130.26 / 100.20 - 1.0) < 0.30


def test_exactly_30_percent_is_flagged() -> None:
    (gap,) = scan("X", _daily([100.20, 130.26]))
    assert gap.move == Decimal("0.3") and gap.flagged


def test_exactly_30_percent_down_is_flagged() -> None:
    (gap,) = scan("X", _daily([100.0, 70.0]))
    assert gap.move == Decimal("-0.3") and gap.flagged


def test_exactly_15_percent_is_reported_but_not_flagged() -> None:
    (gap,) = scan("X", _daily([100.20, 115.23]))
    assert gap.move == Decimal("0.15") and not gap.flagged


def test_just_under_a_threshold_stays_under() -> None:
    assert scan("X", _daily([100.20, 115.22])) == []
    (gap,) = scan("X", _daily([100.20, 130.25]))
    assert not gap.flagged


def test_thresholds_are_decimals() -> None:
    assert FLAG_MOVE == Decimal("0.30") and REPORT_MOVE == Decimal("0.15")
    assert exact_move(100.20, 130.26) == Decimal("0.3")
