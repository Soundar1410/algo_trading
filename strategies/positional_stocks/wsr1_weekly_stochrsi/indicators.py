"""Weekly indicators for ``wsr1_weekly_stochrsi`` (spec section 4.4).

Pure functions. No I/O, no clock, no network, no pandas, and nothing from
``common.engine`` — spec section 13 puts this file beside the rules core, and
the Phase 3 guard test holds both to the same imports.

Every formula must match TradingView (spec section 7), and each one follows the
Pine built-in named beside it:

=====================  ============================================================
Indicator              Definition
=====================  ============================================================
RSI(14)                ``ta.rsi``: Wilder RMA of gains and of losses, each seeded
                       with the SMA of the first 14 **changes**, so the first RSI
                       is at bar 14 (bar 0 has no change)
Stoch of RSI (14)      ``100 * (RSI - min14) / (max14 - min14)``; **undefined** when
                       max == min (never a division by zero)
K, D                   SMA(3) of the stoch; SMA(3) of K
EMA(n)                 ``ta.ema``: alpha = 2/(n+1), **seeded with the SMA of the
                       first n closes** (spec v1.2c); undefined before bar n-1
ATR(14)                ``ta.atr``: Wilder RMA of true range, SMA-seeded; the first
                       bar's true range is its high - low
52-week high           max high over the last 52 bars, including the current one
6-month performance    close / the close of the ISO week 26 weeks earlier - 1
                       (spec v1.2e); undefined if that week has no bar
RS                     stock 6M performance - index 6M performance, in percentage
                       points
=====================  ============================================================

``None`` means undefined
------------------------
Warm-up bars, and anything computed from an undefined input, are ``None`` —
never ``NaN`` and never ``0``. A ``NaN`` compares false against everything, so
``K < 20`` on a ``NaN`` silently reads "not armed"; a ``None`` in a comparison
raises. The rules core has to decide what an undefined value means, and this
keeps that decision from being made by accident.

The flat-range case of spec 4.4 ("the symbol is skipped that week and
flagged") is kept distinguishable from ordinary warm-up:
:attr:`IndicatorSeries.stoch_flat` marks the bars where max == min, and
:meth:`IndicatorSeries.kd_blocked_by_flat_range` says whether a bar's K or D is
undefined because of one.

The history must be long enough
-------------------------------
EMA200 is SMA-seeded, so its value depends on where the series starts until
the seed has decayed — about 55% of it is still seed after 60 further bars.
Spec 6.1 v1.2d therefore fetches each symbol's **full** history; nothing here
can detect a truncated series, so a caller that passes one gets a well-formed,
wrong EMA200.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from .iso_weeks import shift
from .models import WeeklyBar

#: Spec 4.4 lengths.
RSI_LENGTH = 14
STOCH_LENGTH = 14
K_SMOOTHING = 3
D_SMOOTHING = 3
ATR_LENGTH = 14
EMA_LENGTHS = (10, 40, 50, 200)
HIGH_LOOKBACK_BARS = 52
PERFORMANCE_LOOKBACK_WEEKS = 26

#: How many stoch values one D value depends on: D averages 3 K values, and
#: each K averages 3 stoch values, so D at bar i reads stoch[i-4 .. i].
_D_STOCH_SPAN = K_SMOOTHING + D_SMOOTHING - 1

Series = list[float | None]


def _check_length(length: int) -> None:
    if length < 1:
        raise ValueError(f"length must be at least 1, got {length}")


def sma(values: Sequence[float | None], length: int) -> Series:
    """Simple moving average; undefined until ``length`` defined values in a row.

    Any undefined value inside the window makes that bar undefined — it is not
    skipped, which is what ``ta.sma`` does with ``na`` too.
    """
    _check_length(length)
    out: Series = [None] * len(values)
    for i in range(length - 1, len(values)):
        window = values[i - length + 1 : i + 1]
        if all(value is not None for value in window):
            out[i] = sum(value for value in window if value is not None) / length
    return out


def _seeded(values: Sequence[float], length: int, alpha: float) -> Series:
    """Exponential smoothing seeded with the SMA of the first ``length`` values.

    The shared shape of ``ta.rma`` (alpha = 1/length) and ``ta.ema``
    (alpha = 2/(length+1)): the first output is at index ``length - 1``.
    """
    _check_length(length)
    out: Series = [None] * len(values)
    if len(values) < length:
        return out
    average = sum(values[:length]) / length
    out[length - 1] = average
    for i in range(length, len(values)):
        average = alpha * values[i] + (1.0 - alpha) * average
        out[i] = average
    return out


def rma(values: Sequence[float], length: int) -> Series:
    """Wilder's moving average (``ta.rma``), SMA-seeded."""
    return _seeded(values, length, 1.0 / length)


def ema(values: Sequence[float], length: int) -> Series:
    """Exponential moving average (``ta.ema``), SMA-seeded — spec 4.4 v1.2c.

    Not first-value seeded: TradingView's ETERNAL EMA200 at 18 Sep 2026 is
    209.70, which the SMA seed reproduces and the first-value seed (223.24)
    does not.
    """
    return _seeded(values, length, 2.0 / (length + 1))


def rsi(closes: Sequence[float], length: int = RSI_LENGTH) -> Series:
    """Wilder RSI (``ta.rsi``); the first value is at index ``length``.

    Pine's own tie-break is kept: no losses → 100 (even with no gains either),
    no gains → 0.
    """
    _check_length(length)
    out: Series = [None] * len(closes)
    if len(closes) <= length:
        return out
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = rma([max(change, 0.0) for change in changes], length)
    losses = rma([max(-change, 0.0) for change in changes], length)
    for i, (gain, loss) in enumerate(zip(gains, losses, strict=True)):
        if gain is None or loss is None:
            continue
        if loss == 0:
            value = 100.0
        elif gain == 0:
            value = 0.0
        else:
            value = 100.0 - 100.0 / (1.0 + gain / loss)
        # changes[i] is the move into bar i + 1.
        out[i + 1] = value
    return out


def stoch(values: Sequence[float | None], length: int = STOCH_LENGTH) -> tuple[Series, list[bool]]:
    """Stochastic of ``values`` over ``length`` bars, and where its range was flat.

    Returns ``(stoch, flat)``. ``flat[i]`` is True where the window is fully
    defined but max == min; ``stoch[i]`` is then ``None`` (spec 4.4: undefined,
    never divided). A window with an undefined value is warm-up, not flat.
    """
    _check_length(length)
    out: Series = [None] * len(values)
    flat = [False] * len(values)
    for i in range(length - 1, len(values)):
        window = [value for value in values[i - length + 1 : i + 1] if value is not None]
        current = values[i]
        if len(window) < length or current is None:
            continue
        highest, lowest = max(window), min(window)
        if highest == lowest:
            flat[i] = True
            continue
        out[i] = 100.0 * (current - lowest) / (highest - lowest)
    return out, flat


def true_range(bars: Sequence[WeeklyBar]) -> list[float]:
    """``ta.tr(true)``: the first bar has no previous close, so it is high - low."""
    out: list[float] = []
    for i, bar in enumerate(bars):
        if i == 0:
            out.append(bar.high - bar.low)
            continue
        previous = bars[i - 1].close
        out.append(max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous)))
    return out


def atr(bars: Sequence[WeeklyBar], length: int = ATR_LENGTH) -> Series:
    """Wilder ATR (``ta.atr``); the first value is at index ``length - 1``."""
    return rma(true_range(bars), length)


def ratio(numerators: Sequence[float | None], denominators: Sequence[float]) -> Series:
    """``numerator / denominator`` per bar — ATR% is ``ratio(atr, closes)``."""
    return [
        None if numerator is None else numerator / denominator
        for numerator, denominator in zip(numerators, denominators, strict=True)
    ]


def highest_high(bars: Sequence[WeeklyBar], length: int = HIGH_LOOKBACK_BARS) -> Series:
    """Max weekly high over the last ``length`` bars, including the current one."""
    _check_length(length)
    out: Series = [None] * len(bars)
    for i in range(length - 1, len(bars)):
        out[i] = max(bar.high for bar in bars[i - length + 1 : i + 1])
    return out


def performance(bars: Sequence[WeeklyBar], weeks: int = PERFORMANCE_LOOKBACK_WEEKS) -> Series:
    """``close / close of the ISO week `weeks` earlier - 1``, as a fraction.

    Counted in **ISO weeks, not bars** (spec 4.4 v1.2e). A stock with a missing
    week — a suspension — would otherwise compare a longer window than the
    index, and RS would subtract two different periods. When the series has no
    bar for the earlier week, the value is undefined.
    """
    _check_length(weeks)
    closes = {bar.iso_key: bar.close for bar in bars}
    out: Series = []
    for bar in bars:
        earlier = closes.get(shift(bar.iso_key, -weeks))
        out.append(None if earlier is None else bar.close / earlier - 1.0)
    return out


def relative_strength(
    stock: Sequence[WeeklyBar],
    index: Sequence[WeeklyBar],
    weeks: int = PERFORMANCE_LOOKBACK_WEEKS,
) -> Series:
    """Stock 6M performance minus the index's, in **percentage points**, per stock bar.

    The two series are matched on ISO week (:attr:`WeeklyBar.iso_key`), never
    on position or ``week_ending``: the index and a stock end on different
    dates when the stock missed the week's last session. A stock bar whose week
    the index lacks is undefined.
    """
    _require_ascending(stock)
    _require_ascending(index)
    index_performance = dict(
        zip(
            (bar.iso_key for bar in index),
            performance(index, weeks),
            strict=True,
        )
    )
    out: Series = []
    for bar, own in zip(stock, performance(stock, weeks), strict=True):
        benchmark = index_performance.get(bar.iso_key)
        out.append(None if own is None or benchmark is None else 100.0 * (own - benchmark))
    return out


@dataclass(frozen=True, slots=True)
class IndicatorSeries:
    """Every spec 4.4 series for one symbol, index-aligned with ``bars``."""

    bars: tuple[WeeklyBar, ...]
    rsi: tuple[float | None, ...]
    stoch: tuple[float | None, ...]
    #: True where the stoch window was fully defined but flat (max == min).
    stoch_flat: tuple[bool, ...]
    k: tuple[float | None, ...]
    d: tuple[float | None, ...]
    ema: dict[int, tuple[float | None, ...]]
    atr: tuple[float | None, ...]
    atr_pct: tuple[float | None, ...]
    high_52w: tuple[float | None, ...]
    performance_6m: tuple[float | None, ...]

    def kd_blocked_by_flat_range(self, i: int) -> bool:
        """Is K or D undefined at bar ``i`` because of a flat stoch range?

        Spec 4.4: such a week skips the symbol and is flagged — distinct from
        plain warm-up, which is not a data condition worth reporting. D at bar
        ``i`` reads stoch ``i-4 .. i``, so a flat range blocks five bars.
        """
        if self.k[i] is not None and self.d[i] is not None:
            return False
        start = max(0, i - _D_STOCH_SPAN)
        return any(self.stoch_flat[start : i + 1])


def compute(bars: Sequence[WeeklyBar]) -> IndicatorSeries:
    """All spec 4.4 series for one symbol's weekly bars, oldest first.

    RS is not here: it needs the index's bars too — see
    :func:`relative_strength`.

    Raises:
        ValueError: the bars are not strictly ascending by ISO week.
    """
    _require_ascending(bars)
    ordered = tuple(bars)
    closes = [bar.close for bar in ordered]
    rsi_values = rsi(closes)
    stoch_values, flat = stoch(rsi_values)
    k_values = sma(stoch_values, K_SMOOTHING)
    d_values = sma(k_values, D_SMOOTHING)
    atr_values = atr(ordered)
    return IndicatorSeries(
        bars=ordered,
        rsi=tuple(rsi_values),
        stoch=tuple(stoch_values),
        stoch_flat=tuple(flat),
        k=tuple(k_values),
        d=tuple(d_values),
        ema={length: tuple(ema(closes, length)) for length in EMA_LENGTHS},
        atr=tuple(atr_values),
        atr_pct=tuple(ratio(atr_values, closes)),
        high_52w=tuple(highest_high(ordered)),
        performance_6m=tuple(performance(ordered)),
    )


def _require_ascending(bars: Sequence[WeeklyBar]) -> None:
    for earlier, later in pairwise(bars):
        if later.iso_key <= earlier.iso_key:
            raise ValueError(
                f"weekly bars must be strictly ascending by ISO week: "
                f"{earlier.iso_year}-W{earlier.iso_week:02d} is followed by "
                f"{later.iso_year}-W{later.iso_week:02d}"
            )
