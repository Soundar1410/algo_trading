"""Trading-day calendar and execution-day statistics — pure functions.

No exchange holiday calendar is configured anywhere in this project today
(``common.engine.config.SessionConfig.holidays`` defaults to ``()`` and no
YAML file sets it). Every function here is honest about that: a "trading
day" means **Monday-Friday only**. Every result that depends on this is
paired with :data:`TRADING_DAY_CAVEAT` so a page can surface it rather than
silently overstating how many "trading days" really elapsed.

Deliberately has no database access and no Streamlit import — pure date
arithmetic over rows the caller already fetched, so every rule here (weekend
handling, custom-range boundaries, completeness gating) is unit-testable in
isolation from a fixture database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

TRADING_DAY_CAVEAT = (
    "Trading days are Monday-Friday only; exchange holidays are not modeled "
    "in this dashboard and may overstate the eligible-day count."
)


def is_trading_day(day: date) -> bool:
    """Weekday only — see the module caveat about holidays."""
    return day.weekday() < 5


def trading_days(start: date, end: date) -> tuple[date, ...]:
    """Every trading day in ``[start, end]``, inclusive. Empty if start > end."""
    if start > end:
        return ()
    days = []
    current = start
    while current <= end:
        if is_trading_day(current):
            days.append(current)
        current += timedelta(days=1)
    return tuple(days)


def n_trading_days_back(end: date, n: int) -> date:
    """The date ``n`` trading days before (and including) ``end``.

    ``n=1`` returns ``end`` itself if it is a trading day, else the trading
    day before it. Used for the "last 7 trading days" / "last 30 trading
    days" performance-page presets.
    """
    if n <= 0:
        return end
    remaining = n
    current = end
    # Generous bound: even a long weekend run cannot need more than
    # n * 2 + 7 calendar hops to find n trading days.
    for _ in range(n * 2 + 14):
        if is_trading_day(current):
            remaining -= 1
            if remaining == 0:
                return current
        current -= timedelta(days=1)
    return current  # pragma: no cover - unreachable with a sane n


@dataclass(frozen=True)
class DailyOutcome:
    """One trading date's activity, as observed in the runtime database."""

    trading_date: date
    ran: bool
    trade_count: int
    net_pnl: float


def merge_daily_outcomes(outcomes: tuple[DailyOutcome, ...]) -> tuple[DailyOutcome, ...]:
    """Combine per-strategy ``DailyOutcome`` rows into one platform-wide
    series, one row per date.

    Necessary before handing a multi-strategy outcome list to
    :func:`execution_day_stats` or :func:`build_thirty_day_rollup`: both
    key their internal lookup on ``trading_date`` alone, so a flat list
    with two strategies' rows for the same date would silently let the
    later one overwrite the earlier rather than combining them. ``ran`` is
    true if *any* contributing strategy ran that day; ``trade_count``/
    ``net_pnl`` sum across all of them.
    """
    by_date: dict[date, list[DailyOutcome]] = {}
    for outcome in outcomes:
        by_date.setdefault(outcome.trading_date, []).append(outcome)
    return tuple(
        DailyOutcome(
            trading_date=day,
            ran=any(o.ran for o in rows),
            trade_count=sum(o.trade_count for o in rows),
            net_pnl=sum(o.net_pnl for o in rows),
        )
        for day, rows in sorted(by_date.items())
    )


@dataclass(frozen=True)
class ExecutionDayStats:
    eligible_trading_days: int
    executed_days: int
    skipped_days: int
    execution_pct: float | None


def execution_day_stats(
    outcomes: tuple[DailyOutcome, ...], *, window_start: date, window_end: date
) -> ExecutionDayStats:
    """Eligible vs. executed trading days over ``[window_start, window_end]``.

    "Executed" means at least one trade closed that day, not merely that the
    runtime was up — matching the spec's "executed days vs. eligible trading
    days" distinction between running and actually trading.
    """
    eligible = trading_days(window_start, window_end)
    by_date = {o.trading_date: o for o in outcomes}
    executed = sum(1 for d in eligible if by_date.get(d) and by_date[d].trade_count > 0)
    ran_no_trade = sum(
        1 for d in eligible if by_date.get(d) and by_date[d].ran and by_date[d].trade_count == 0
    )
    pct = (executed / len(eligible) * 100.0) if eligible else None
    return ExecutionDayStats(
        eligible_trading_days=len(eligible),
        executed_days=executed,
        skipped_days=ran_no_trade,
        execution_pct=pct,
    )


@dataclass(frozen=True)
class ThirtyDayRollup:
    """The Home page's "30-day paper-forward-testing" section."""

    window_start: date
    window_end: date
    trading_days_elapsed: int
    days_executed: int
    days_with_trades: int
    no_trade_days: int
    profitable_days: int
    loss_making_days: int
    cumulative_net_pnl: float
    data_complete: bool
    completeness_note: str


def build_thirty_day_rollup(
    outcomes: tuple[DailyOutcome, ...],
    *,
    inception_date: date | None,
    today: date,
) -> ThirtyDayRollup | None:
    """Roll up the 30 calendar days up to ``today``, or since ``inception_date``
    if forward testing started more recently than that.

    Returns ``None`` when ``inception_date`` is ``None`` — no runtime
    activity has ever been recorded, which is a different, stronger claim
    than "zero trades so far in an active window".
    """
    if inception_date is None:
        return None

    window_start = max(inception_date, today - timedelta(days=29))
    window_end = today
    eligible = trading_days(window_start, window_end)
    by_date = {o.trading_date: o for o in outcomes}

    days_executed = 0
    days_with_trades = 0
    no_trade_days = 0
    profitable_days = 0
    loss_making_days = 0
    cumulative = 0.0
    missing_days = 0

    for day in eligible:
        outcome = by_date.get(day)
        if outcome is None:
            missing_days += 1
            continue
        if outcome.ran:
            days_executed += 1
        if outcome.trade_count > 0:
            days_with_trades += 1
            cumulative += outcome.net_pnl
            if outcome.net_pnl > 0:
                profitable_days += 1
            elif outcome.net_pnl < 0:
                loss_making_days += 1
        elif outcome.ran:
            no_trade_days += 1

    calendar_days_elapsed = (today - inception_date).days + 1
    if calendar_days_elapsed < 30:
        complete = False
        note = (
            f"Only {calendar_days_elapsed} of 30 calendar days have elapsed since "
            "forward testing began — this is not yet complete 30-day evidence."
        )
    elif missing_days:
        complete = False
        note = (
            f"{missing_days} eligible trading day(s) in this window have no recorded "
            "runtime activity — treat this window as incomplete evidence."
        )
    else:
        complete = True
        note = "30-day window complete: every eligible trading day has recorded activity."

    return ThirtyDayRollup(
        window_start=window_start,
        window_end=window_end,
        trading_days_elapsed=len(eligible),
        days_executed=days_executed,
        days_with_trades=days_with_trades,
        no_trade_days=no_trade_days,
        profitable_days=profitable_days,
        loss_making_days=loss_making_days,
        cumulative_net_pnl=cumulative,
        data_complete=complete,
        completeness_note=note,
    )
