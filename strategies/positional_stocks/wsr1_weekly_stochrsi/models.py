"""Value types for the ``wsr1_weekly_stochrsi`` data layer (spec sections 3, 6).

Phase 1 shapes only: bars, and the three operator input rows. Signals,
positions and orders arrive with the rules core and the runtime in Phases 3-4,
as spec section 13 anticipates.

Every type here is frozen and validates itself on construction. That is the
same judgement ``common.models.Candle`` makes and for the same reason: a bar
whose ``high`` is below its ``low`` is a source-data defect, and catching it
where the bar is built names the real problem, rather than letting it surface
five weeks later as an impossible ATR.

Deliberately **not** ``common.models.Candle``: that type is intraday-shaped
(``start_at``/``end_at`` datetimes, a minutes interval, a ``security_id`` and
an ``instrument``), and spec section 13 keeps weekly bars out of
``common/candles/`` and ``common/warmup/`` entirely — ``parse_timeframe_minutes``
rejects ``1d`` and ``1W``, and the whole warm-up stack buckets by intraday
minutes within one session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class BarError(ValueError):
    """A bar could not be built because its values are not internally consistent."""


def _validate_ohlc(open_: float, high: float, low: float, close: float, label: str) -> None:
    values = {"open": open_, "high": high, "low": low, "close": close}
    for name, value in values.items():
        if value != value:  # NaN is the one float that is not equal to itself
            raise BarError(f"{label}: {name} is NaN")
        if value <= 0:
            raise BarError(f"{label}: {name} must be positive, got {value}")
    if high < low:
        raise BarError(f"{label}: high {high} is below low {low}")
    if not (low <= open_ <= high):
        raise BarError(f"{label}: open {open_} is outside [{low}, {high}]")
    if not (low <= close <= high):
        raise BarError(f"{label}: close {close} is outside [{low}, {high}]")


@dataclass(frozen=True, slots=True)
class DailyBar:
    """One trading session, as Dhan's daily-candle endpoint reports it.

    ``session`` is the session's date in IST. There is no time component: the
    endpoint's timestamps are converted once, at parse time, and every
    downstream comparison in this strategy is date-to-date.
    """

    session: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        _validate_ohlc(self.open, self.high, self.low, self.close, f"daily bar {self.session}")
        if self.volume < 0:
            raise BarError(f"daily bar {self.session}: volume {self.volume} is negative")

    @property
    def traded_value(self) -> float:
        """Close x volume — the liquidity measure of spec 4.1, which says to use
        this when the source gives no traded value. Dhan's daily candle does not."""
        return self.close * self.volume


@dataclass(frozen=True, slots=True)
class WeeklyBar:
    """One completed ISO week, aggregated from that week's sessions (spec 3).

    ``week_ending`` is the date of the week's **last trading session**, not its
    Friday — spec section 3 defines it that way, and in a holiday week the two
    differ. ``sessions`` carries how many sessions the week actually had, so a
    short week stays visible to anything that cares.
    """

    week_ending: date
    iso_year: int
    iso_week: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    sessions: int = 0

    def __post_init__(self) -> None:
        label = f"weekly bar {self.iso_year}-W{self.iso_week:02d}"
        _validate_ohlc(self.open, self.high, self.low, self.close, label)
        if self.sessions <= 0:
            raise BarError(f"{label}: a weekly bar needs at least one session")
        if self.volume < 0:
            raise BarError(f"{label}: volume {self.volume} is negative")

    @property
    def iso_key(self) -> tuple[int, int]:
        """``(iso_year, iso_week)`` — the identity a week is compared on.

        Never ``week_ending``: two symbols' bars for the same week end on
        different dates when one of them did not trade on the week's last day.
        """
        return (self.iso_year, self.iso_week)


class QualityStatus(Enum):
    """The operator's verdict for one symbol (spec 4.2, 6.4)."""

    PASS = "PASS"
    EVENT_RISK = "EVENT_RISK"
    FAIL = "FAIL"


class OnExit(Enum):
    """What happens to a held symbol the operator removed from the universe (spec 6.3)."""

    HOLD = "hold"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class UniverseRow:
    """One row of ``universe.csv`` (spec 6.3)."""

    symbol: str
    isin: str
    company: str
    industry: str
    nifty100: bool
    group: str | None
    on_exit: OnExit
    as_of: date | None

    @property
    def effective_group(self) -> str:
        """The promoter group this symbol counts against for the limit of spec 4.12.

        A blank ``group`` means "its own group" (spec 6.3), so it resolves to
        the symbol itself — which keeps the per-group limit meaningful without
        the operator having to name a group for every standalone company.
        """
        return self.group or self.symbol


@dataclass(frozen=True, slots=True)
class QualityRow:
    """One row of ``quality_gate.csv`` (spec 6.4)."""

    symbol: str
    status: QualityStatus
    checked_on: date | None
    valid_until: date
    notes: str = ""

    def is_valid_on(self, execution_date: date) -> bool:
        """Spec 4.2: usable only while ``valid_until`` is on or after the
        execution date. Expiry alone decides this; ``status`` is a separate
        question the caller asks next."""
        return execution_date <= self.valid_until


@dataclass(frozen=True, slots=True)
class ResultsRow:
    """One row of ``results_calendar.csv`` (spec 6.5)."""

    symbol: str
    results_date: date
