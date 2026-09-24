"""Phase 2 acceptance: ``indicators.py`` against TradingView (spec section 7).

The readings are the operator's, recorded in spec section 7 (v1.2c):
TradingView, 1W, NSE, "Adjust data for dividends" OFF, the bar TradingView
labels "Tue 15 Sep '26" — the week ending Friday 18 Sep 2026 (Mon 14 Sep was a
holiday). Tolerances are the spec's: ±0.5 for K and D, ±0.5% for EMA and ATR.

The bars are a **committed fixture**, frozen from the local Dhan daily cache
after spec 6.1 v1.2d's full-history refetch and aggregated by
``weekly_bars.build_weekly_bars``. So this test runs offline and in CI, and a
failure here is either the formulas or the frozen data — never the network.

**The fixture must hold each symbol's full history.** EMA200 is SMA-seeded, and
the Phase 2 pre-check found ETERNAL's EMA200 at 206.60 from a 260-week window
against TradingView's 209.70 — the seed averaged a different 200 weeks. That is
why the test asserts the history length before it asserts any value.

Each symbol first checks the weekly **close** against TradingView's: if that
disagrees, the data is misaligned (a wrong week, a missing weekend special
session, an adjustment gap), and every indicator comparison after it would be
meaningless.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from strategies.positional_stocks.wsr1_weekly_stochrsi.indicators import compute
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import WeeklyBar

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "wsr1_parity_weekly_2026-09-18.json"
WEEK_ENDING = date(2026, 9, 18)

K_D_TOLERANCE = 0.5
RELATIVE_TOLERANCE = 0.005

#: The weekend special sessions TradingView counts in their Monday-Sunday week
#: (spec section 7). RELIANCE's K moves by ~1.1 without the Sunday 2026-02-01.
SPECIAL_SESSION_WEEKS = {
    date(2024, 1, 20): (2024, 3),
    date(2024, 3, 2): (2024, 9),
    date(2024, 5, 18): (2024, 20),
    date(2025, 2, 1): (2025, 5),
    date(2026, 2, 1): (2026, 5),
}


@dataclass(frozen=True)
class Reading:
    close: float
    k: float
    d: float
    ema50: float
    ema200: float | None
    atr14: float


#: Spec section 7, verbatim.
TRADINGVIEW = {
    "ADANIENSOL": Reading(1436.10, 4.64, 4.51, 1285.70, None, 116.00),
    "RELIANCE": Reading(1226.40, 34.68, 49.60, 1353.80, None, 55.60),
    "HDFCBANK": Reading(731.00, 16.53, 14.86, 817.94, None, 33.56),
    "INFY": Reading(1051.40, 58.64, 70.51, 1270.60, None, 76.06),
    "LT": Reading(3885.00, 25.83, 40.62, 3909.70, None, 168.23),
    "ETERNAL": Reading(326.85, 90.14, 93.05, 280.88, 209.70, 19.27),
}


def _load() -> dict[str, list[WeeklyBar]]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    columns = payload["columns"]
    out: dict[str, list[WeeklyBar]] = {}
    for symbol, rows in payload["symbols"].items():
        bars = []
        for row in rows:
            record = dict(zip(columns, row, strict=True))
            bars.append(
                WeeklyBar(
                    week_ending=date.fromisoformat(record["week_ending"]),
                    iso_year=record["iso_year"],
                    iso_week=record["iso_week"],
                    open=record["open"],
                    high=record["high"],
                    low=record["low"],
                    close=record["close"],
                    expected_last_session=date.fromisoformat(record["expected_last_session"]),
                    volume=record["volume"],
                    sessions=record["sessions"],
                )
            )
        out[symbol] = bars
    return out


BARS = _load()


def test_the_fixture_holds_exactly_the_six_parity_symbols() -> None:
    assert set(BARS) == set(TRADINGVIEW)


@pytest.mark.parametrize("symbol", sorted(TRADINGVIEW))
def test_the_fixture_is_full_history_through_the_parity_week(symbol: str) -> None:
    bars = BARS[symbol]
    assert bars[-1].week_ending == WEEK_ENDING
    assert bars[-1].iso_key == (2026, 38)
    # More than the 260 weeks whose window made EMA200 wrong (spec 6.1 v1.2d).
    # ETERNAL listed in July 2021, so it has the fewest.
    assert len(bars) > 260


@pytest.mark.parametrize("symbol", sorted(TRADINGVIEW))
def test_weekend_special_sessions_sit_in_their_iso_week(symbol: str) -> None:
    """A Saturday or Sunday session is its week's last, so the bar must end on it."""
    by_week = {bar.iso_key: bar for bar in BARS[symbol]}
    for session, week in SPECIAL_SESSION_WEEKS.items():
        assert by_week[week].week_ending == session, (symbol, session)


@pytest.mark.parametrize("symbol", sorted(TRADINGVIEW))
def test_parity_with_tradingview(symbol: str) -> None:
    expected = TRADINGVIEW[symbol]
    series = compute(BARS[symbol])
    last = len(series.bars) - 1

    # Data alignment first: the same week, the same close.
    assert series.bars[last].close == expected.close

    k, d = series.k[last], series.d[last]
    assert k is not None and d is not None
    assert k == pytest.approx(expected.k, abs=K_D_TOLERANCE)
    assert d == pytest.approx(expected.d, abs=K_D_TOLERANCE)

    ema50 = series.ema[50][last]
    assert ema50 == pytest.approx(expected.ema50, rel=RELATIVE_TOLERANCE)
    if expected.ema200 is not None:
        ema200 = series.ema[200][last]
        assert ema200 == pytest.approx(expected.ema200, rel=RELATIVE_TOLERANCE)

    assert series.atr[last] == pytest.approx(expected.atr14, rel=RELATIVE_TOLERANCE)


def test_adaniensol_first_reference_value() -> None:
    """Spec section 7's first known value: K 4.64, D 4.51."""
    series = compute(BARS["ADANIENSOL"])
    assert series.k[-1] == pytest.approx(4.64, abs=K_D_TOLERANCE)
    assert series.d[-1] == pytest.approx(4.51, abs=K_D_TOLERANCE)
