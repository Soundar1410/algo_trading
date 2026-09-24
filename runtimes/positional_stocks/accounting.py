"""One decided week of the paper book, through persistence (spec 4.13, 9).

:func:`run_decision_week` is the unit Phase 4b's ``weekly_run.py`` will call
once per week (and once per missed week, in order, when catching up). It is
**not** a CLI, fetches nothing and reads no clock: the caller supplies the
week, the indicator series, the operator rows and the cached daily bars.

Order within the week, spec 4.13:

1. Fill every PENDING order that has a session this week, through the rules'
   own ``apply_fill`` (:mod:`.paper_fills`), and **clear filled orders** before
   deciding (v1.2h). An order with no session yet stays PENDING, keeping its
   sizing, sector and group — and so its capacity — for the next run.
2-4. Rebuild the book from the database and call ``decide_week``.
5. Persist the decisions as PENDING orders, and the week's positions, equity,
   brakes, funnel with trigger history, and last-seen universe rows.

**Idempotent per (strategy_id, week_ending).** A COMPLETED week returns
without writing. An earlier week than the latest run is refused, and so is a
new week while an earlier one is still STARTED.

**Crash-safe.** STARTED is committed on its own first; steps 1-5 and the flip to
COMPLETED are **one** transaction. A crash anywhere inside rolls all of it back,
leaving only STARTED, and the next run for that week redoes it from scratch —
the fills of step 1 included, because they were never committed either.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date

from strategies.positional_stocks.wsr1_weekly_stochrsi.gaps import (
    block_window_start,
    blocking_gaps,
    scan,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.indicators import IndicatorSeries
from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import shift
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    DailyBar,
    GapAcknowledgement,
    RulesParameters,
    UniverseRow,
    WeekDecision,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import (
    SymbolWeek,
    WeekContext,
    decide_week,
    traded_value,
    trigger,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar

from .paper_fills import OrderFill, fill_orders
from .repository import StockRepository


class RunOrderError(RuntimeError):
    """The requested week cannot be decided now: it is earlier than the latest
    run, or an earlier week is still unfinished."""


@dataclass(frozen=True, slots=True)
class WeekInputs:
    """Everything one decision week needs, supplied by the caller.

    ``symbols`` carry each symbol's series, current universe row, quality row
    and results dates; this module fills in what only it knows — the gap
    block, the 30-session traded value and the last-seen universe row.
    ``daily`` must include every held symbol and every symbol with a pending
    order. ``universe_rows`` is the whole current ``universe.csv``.
    """

    ctx: WeekContext
    symbols: Mapping[str, SymbolWeek]
    index: IndicatorSeries
    daily: Mapping[str, Sequence[DailyBar]]
    universe_rows: Sequence[UniverseRow]
    acknowledgements: Sequence[GapAcknowledgement] = ()
    fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class WeekOutcome:
    week_ending: date
    #: COMPLETED this call, or ALREADY_COMPLETED (nothing written).
    status: str
    decision: WeekDecision | None = None
    fills: tuple[OrderFill, ...] = ()
    #: This call redid a week an earlier run had STARTED but not finished.
    resumed: bool = False


def run_decision_week(
    repository: StockRepository,
    inputs: WeekInputs,
    *,
    calendar: TradingCalendar,
    params: RulesParameters,
) -> WeekOutcome:
    """Fill, decide and persist one week — or nothing, if it is already done."""
    ctx = inputs.ctx
    status = repository.run_status(ctx.week_ending)
    if status == "COMPLETED":
        return WeekOutcome(ctx.week_ending, "ALREADY_COMPLETED")
    latest = repository.latest_run()
    if latest is not None:
        latest_week, latest_status = latest
        if latest_week > ctx.week_ending:
            raise RunOrderError(
                f"cannot decide {ctx.week_ending}: {latest_week} has already been run"
            )
        if latest_status == "STARTED" and latest_week != ctx.week_ending:
            raise RunOrderError(
                f"{latest_week} was started and not finished; run it again before {ctx.week_ending}"
            )

    repository.mark_started(ctx.week_ending, ctx.week, inputs.fingerprint)
    daily = {
        symbol: [bar for bar in bars if bar.session <= ctx.week_ending]
        for symbol, bars in inputs.daily.items()
    }

    with repository.database.transaction(immediate=True) as conn:
        # Step 1: fills, then clear filled orders (v1.2h).
        positions = repository.positions(conn)
        fills, filled_positions = fill_orders(
            repository.pending_orders(conn),
            positions,
            daily,
            through=ctx.week_ending,
            calendar=calendar,
            params=params,
        )
        for result in fills:
            outcome = result.outcome
            if outcome is None:
                continue  # no session yet: stays PENDING, keeps its capacity
            if outcome.skipped is not None:
                repository.resolve_order(
                    conn, result.order, state="SKIPPED", week=ctx.week, resolution=outcome.skipped
                )
                continue
            assert outcome.position is not None and result.plan is not None
            position = filled_positions[outcome.position.position_id]
            repository.save_position(conn, position, ctx.week)
            repository.resolve_order(
                conn, result.order, state="FILLED", week=ctx.week, resolution="filled"
            )
            repository.save_fill(
                conn,
                result.order,
                position,
                cash_delta=outcome.cash_delta,
                not_traded_on_execution_session=result.plan.not_traded_on_execution_session,
                week=ctx.week,
            )
            if outcome.closed is not None and outcome.closed.is_loss:
                until = shift(outcome.closed.exit_week, params.cooling_off_weeks)
                repository.save_cooling_off(conn, outcome.closed, until)

        # Steps 2-4: decide on the book as it now stands.
        book = repository.book(conn)
        symbols = _complete(inputs, daily, repository.last_seen_rows(conn))
        decision = decide_week(ctx, book, symbols, inputs.index, params)

        # Step 5: persist.
        for position in decision.positions:
            repository.save_position(conn, position, ctx.week)
        for order in decision.orders:
            repository.save_order(conn, order)
        triggered = {
            symbol: _triggered(week_inputs.series, ctx, params)
            for symbol, week_inputs in symbols.items()
        }
        repository.save_decision(
            conn, decision, ctx.week_ending, cash=book.cash, triggered=triggered
        )
        repository.save_universe_seen(conn, inputs.universe_rows, ctx.week)
        repository.mark_completed(conn, ctx.week_ending)

    return WeekOutcome(
        ctx.week_ending, "COMPLETED", decision, tuple(fills), resumed=status == "STARTED"
    )


def _complete(
    inputs: WeekInputs,
    daily: Mapping[str, Sequence[DailyBar]],
    last_seen: Mapping[str, UniverseRow],
) -> dict[str, SymbolWeek]:
    """Fill in the fields of each ``SymbolWeek`` that come from stored state."""
    out: dict[str, SymbolWeek] = {}
    for symbol, week_inputs in inputs.symbols.items():
        bars = daily.get(symbol, ())
        window = block_window_start(week_inputs.series.bars)
        gaps = blocking_gaps(scan(symbol, bars), inputs.acknowledgements, window_start=window)
        out[symbol] = replace(
            week_inputs,
            gap_blocked=bool(gaps),
            traded_value_30d=traded_value(bars),
            last_seen_row=last_seen.get(symbol, week_inputs.last_seen_row),
        )
    return out


def _triggered(series: IndicatorSeries, ctx: WeekContext, params: RulesParameters) -> bool:
    """Spec 4.5's TRIGGER at this week's close — the persisted trigger history."""
    if not series.bars or series.bars[-1].iso_key != ctx.week:
        return False
    return trigger(series, len(series.bars) - 1, params).triggered
