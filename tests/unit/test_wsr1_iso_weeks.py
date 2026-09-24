"""ISO-week arithmetic (spec 4.4 v1.2e, 4.10-4.11 v1.2f): pure, and exact at year ends."""

from __future__ import annotations

from datetime import date

import pytest

from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import (
    monday_of,
    shift,
    week_of,
    weeks_between,
)


def test_monday_of_and_week_of_round_trip() -> None:
    assert monday_of((2026, 38)) == date(2026, 9, 14)
    assert week_of(date(2026, 9, 18)) == (2026, 38)
    assert week_of(date(2026, 9, 20)) == (2026, 38)  # Sunday stays in its week


def test_2026_has_53_iso_weeks() -> None:
    # Fri 1 Jan 2027 falls in 2026-W53.
    assert week_of(date(2027, 1, 1)) == (2026, 53)
    assert shift((2026, 53), 1) == (2027, 1)
    assert shift((2027, 1), -1) == (2026, 53)
    assert shift((2026, 52), 2) == (2027, 1)


def test_a_52_week_year_has_no_week_53() -> None:
    assert shift((2025, 52), 1) == (2026, 1)
    with pytest.raises(ValueError):
        monday_of((2025, 53))


def test_weeks_between_counts_iso_weeks() -> None:
    assert weeks_between((2026, 38), (2026, 38)) == 0
    assert weeks_between((2026, 1), (2027, 1)) == 53
    assert weeks_between((2025, 1), (2026, 1)) == 52
    assert weeks_between((2026, 10), (2026, 8)) == -2


def test_shift_and_weeks_between_agree() -> None:
    for weeks in (-60, -26, -1, 0, 1, 26, 52, 53, 520):
        assert weeks_between((2024, 30), shift((2024, 30), weeks)) == weeks
