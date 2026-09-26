"""Build one decision week's inputs from the local cache and the operator CSVs.

Offline by construction (spec 10.3): it reads :class:`DailyBarCache` — keyed
by symbol, so no instrument id is resolved — and the CSVs under
``config/positional_stocks/``. No Dhan client, scrip master, token or feed.

* **Staleness from the calendar only (6.2).** The week's expected last session
  is :meth:`TradingCalendar.expected_last_session`, never read from the data.
  NIFTY 50 missing it is a **cold cache**: :class:`ColdCache`, no trades.
* **Any other stale series is skipped and reported.** A stale symbol that is
  held or has a pending order is kept with its series ending *before* the
  week, so it is marked at its last close and the rules' "no bar this week:
  held, no decision" applies — dropping it would leave the book unmarkable.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from strategies.positional_stocks.wsr1_weekly_stochrsi.daily_cache import (
    DailyBarCache,
    assess_index_publication,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.indicators import (
    IndicatorSeries,
    compute,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.inputs import (
    QualityGateFile,
    ResultsCalendarFile,
    UniverseFile,
    load_corporate_actions,
    load_gap_acknowledgements,
    load_quality_gate,
    load_results_calendar,
    load_universe,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import WeekKey, shift
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    CorporateActionRow,
    DailyBar,
    GapAcknowledgement,
    RulesParameters,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import SymbolWeek, WeekContext
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar
from strategies.positional_stocks.wsr1_weekly_stochrsi.weekly_bars import (
    build_weekly_bars,
    week_release_moment,
)

from .accounting import WeekInputs

#: Daily history kept in the WeekInputs (fills, the 30-session traded value,
#: the gap scan's 520-week block window, corporate-action checks). The full
#: history is used only to build the weekly series.
_DAILY_KEEP_DAYS = 11 * 366

OPERATOR_FILES = (
    "universe.csv",
    "quality_gate.csv",
    "results_calendar.csv",
    "gap_acknowledgements.csv",
    "corporate_actions.csv",
)


class ColdCache(RuntimeError):
    """NIFTY 50 lacks the week's expected last session: no trades (spec 6.2)."""


@dataclass(frozen=True)
class OperatorInputs:
    """The operator CSVs, loaded fail-closed once per run."""

    universe: UniverseFile
    quality: QualityGateFile
    results: ResultsCalendarFile
    acknowledgements: tuple[GapAcknowledgement, ...]
    corporate_actions: tuple[CorporateActionRow, ...]
    fingerprint: str


def load_operator_inputs(config_dir: Path) -> OperatorInputs:
    """Every operator CSV (spec 6.1, 6.3-6.5, 4.14). Raises InputFileError."""
    digest = hashlib.sha256()
    for name in OPERATOR_FILES:
        path = config_dir / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.is_file() else b"<absent>")
    return OperatorInputs(
        universe=load_universe(config_dir / "universe.csv"),
        quality=load_quality_gate(config_dir / "quality_gate.csv"),
        results=load_results_calendar(config_dir / "results_calendar.csv"),
        acknowledgements=load_gap_acknowledgements(config_dir / "gap_acknowledgements.csv"),
        corporate_actions=load_corporate_actions(config_dir / "corporate_actions.csv"),
        fingerprint=digest.hexdigest()[:16],
    )


@dataclass(frozen=True)
class StaleSymbol:
    symbol: str
    reason: str
    #: Held or with a pending order: kept, marked at its last close, undecided.
    kept: bool
    #: Consecutive weeks, ending with this one, without the expected session.
    weeks: int


@dataclass(frozen=True)
class PreparedWeek:
    inputs: WeekInputs
    week: WeekKey
    stale: tuple[StaleSymbol, ...] = ()
    unlisted_sessions: tuple[date, ...] = ()
    warnings: tuple[str, ...] = ()
    #: Every symbol's series, for the report (watchlist, marks).
    series: Mapping[str, IndicatorSeries] = field(default_factory=dict)


def _sunday(week: WeekKey) -> date:
    return date.fromisocalendar(week[0], week[1], 7)


def stale_weeks(bars: Sequence[DailyBar], week: WeekKey, calendar: TradingCalendar) -> int:
    """Consecutive weeks, ending at ``week``, whose expected last session is
    missing from ``bars`` (0 when the week is covered). Decision 5 extension:
    from 2, it is an operator action — a suspended stock cannot hit its stop."""
    sessions = {bar.session for bar in bars}
    count, current = 0, week
    while count < 104:
        expected = calendar.expected_last_session(current)
        if any(s >= expected and s <= _sunday(current) for s in sessions):
            return count
        count += 1
        current = shift(current, -1)
    return count


def prepare_week(
    week: WeekKey,
    *,
    cache: DailyBarCache,
    calendar: TradingCalendar,
    operator: OperatorInputs,
    held: Iterable[str],
    pending: Iterable[str],
    index_symbol: str,
    params: RulesParameters,
) -> PreparedWeek:
    """Everything :func:`~.accounting.run_decision_week` needs for ``week``.

    Raises:
        ColdCache: the index has not published the week's last session.
    """
    week_ending = calendar.expected_last_session(week)
    last_day = _sunday(week)
    release: datetime = week_release_moment(week_ending, calendar=calendar)

    index_daily = [bar for bar in cache.read(index_symbol) if bar.session <= last_day]
    verdict = assess_index_publication(
        index_daily, week=week, expected_session=week_ending, calendar=calendar
    )
    if not verdict.is_published:
        raise ColdCache(verdict.reason)
    index = compute(
        build_weekly_bars(index_daily, as_of=release, calendar=calendar, warn_uncovered=False)
    )

    kept = set(held) | set(pending)
    universe = operator.universe.by_symbol
    quality = operator.quality.by_symbol
    results = operator.results
    symbols: dict[str, SymbolWeek] = {}
    daily: dict[str, list[DailyBar]] = {}
    all_series: dict[str, IndicatorSeries] = {}
    stale: list[StaleSymbol] = []
    keep_from = date.fromordinal(week_ending.toordinal() - _DAILY_KEEP_DAYS)

    for symbol in sorted(set(universe) | kept):
        bars = [bar for bar in cache.read(symbol) if bar.session <= last_day]
        weekly = build_weekly_bars(bars, as_of=release, calendar=calendar, warn_uncovered=False)
        missing = stale_weeks(bars, week, calendar)
        if missing:
            last = max((bar.session for bar in bars), default=None)
            reason = (
                f"{symbol}: no cached sessions"
                if last is None
                else f"{symbol}: last cached session {last} is before the week's last "
                f"session {week_ending}"
            )
            stale.append(StaleSymbol(symbol, reason, symbol in kept, missing))
            if symbol not in kept:
                continue
            # Kept, but never decided on a truncated week (6.2).
            weekly = [bar for bar in weekly if bar.iso_key < week]
            if not weekly:
                # Nothing to mark it at: the book cannot be valued. Fail closed.
                raise ColdCache(
                    f"{symbol} is held or has a pending order but the cache has no "
                    "completed week for it at all; the book cannot be marked"
                )
        series = compute(weekly)
        all_series[symbol] = series
        daily[symbol] = [bar for bar in bars if bar.session >= keep_from]
        symbols[symbol] = SymbolWeek(
            symbol=symbol,
            series=series,
            universe_row=universe.get(symbol),
            quality=quality.get(symbol),
            results_dates=results.dates_for(symbol) if results.knows(symbol) else None,
        )

    warnings: list[str] = []
    if operator.universe.symbols_missing_nifty100:
        warnings.append(
            f"{len(operator.universe.symbols_missing_nifty100)} universe rows have a blank "
            "nifty100 (read as false: not eligible in a Red regime)"
        )
    if results.absent or not len(results):
        warnings.append("results_calendar.csv has no rows: every symbol is 'results date unknown'")

    ctx = WeekContext(
        week=week,
        week_ending=week_ending,
        execution_date=calendar.next_session_after(week_ending),
    )
    inputs = WeekInputs(
        ctx=ctx,
        symbols=symbols,
        index=index,
        daily=daily,
        universe_rows=operator.universe.rows,
        acknowledgements=operator.acknowledgements,
        corporate_actions=operator.corporate_actions,
        fingerprint=f"{operator.fingerprint}:{params!r}"[:200],
    )
    return PreparedWeek(
        inputs=inputs,
        week=week,
        stale=tuple(stale),
        unlisted_sessions=verdict.unlisted_sessions,
        warnings=tuple(warnings),
        series=all_series,
    )
