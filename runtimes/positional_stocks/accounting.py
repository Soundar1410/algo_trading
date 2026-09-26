"""One decided week of the paper book, through persistence (spec 4.13, 9).

:func:`run_decision_week` is the unit Phase 4b's ``weekly_run.py`` will call
once per week (and once per missed week, in order, when catching up). It is
**not** a CLI, fetches nothing and reads no clock: the caller supplies the
week, the indicator series, the operator rows and the cached daily bars.

Order within the week, spec 4.13:

0. **Corporate actions (4.14 v1.2k, D101, D102).**
   - An unacknowledged raw gap of 15% or more after a held position's
     first fill (an action Dhan has not back-adjusted, item 7) **freezes** it.
   - Otherwise its fills are compared with the cached opens of their
     sessions. A restatement with a matching confirmed row in
     ``corporate_actions.csv`` is rescaled: stored as its own record, and
     each pending sell SUPERSEDED by one re-issued in the new units from the
     original execution session. A consolidation to 0 shares closes the
     position on cash in lieu (D101). Without a matching row it is frozen.
   - A frozen position's pending orders are not filled and the rules decide
     nothing for it. A freeze of 3 or more runs that a confirmed row
     explains but cannot resolve is exited at open / factor (D102).
1. Fill every PENDING order that has a session this week, through the rules'
   own ``apply_fill`` (:mod:`.paper_fills`), and **clear filled orders** before
   deciding (v1.2h). An order with no session yet stays PENDING, keeping its
   sizing, sector and group — and so its capacity — for the next run.
2-4. Rebuild the book from the database and call ``decide_week``.
5. Persist the decisions as PENDING orders, and the week's positions, equity,
   brakes, funnel with trigger history, and last-seen universe rows.

**Idempotent per (strategy_id, week_ending).** A COMPLETED week returns
without writing. An earlier week than the latest run is refused, and so is a
new week while an earlier one is still STARTED. **Strict week order (10.2
v1.2j):** week W is decided only as the first run or when W - 1 is COMPLETED —
a skipped week is a skipped stop check.

**Late fills (spec 8 v1.2j).** A fill whose session falls in an earlier week
than this run's is flagged ``late_fill``, and the add-touch memory of a
position bought late is replayed from its fill week before deciding.

**Crash-safe.** STARTED is committed on its own first; steps 1-5 and the flip to
COMPLETED are **one** transaction. A crash anywhere inside rolls all of it back,
leaving only STARTED, and the next run for that week redoes it from scratch —
the fills of step 1 included, because they were never committed either.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from strategies.positional_stocks.wsr1_weekly_stochrsi.corporate_actions import (
    Rescale,
    detect,
    eligible_rows,
    freeze_factor,
    gap_detail,
    price_correction_row,
    resolve,
    stuck_exit_row,
    unadjusted_gaps,
    unverifiable,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.gaps import (
    block_window_start,
    blocking_gaps,
    is_acknowledged,
    scan,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.indicators import IndicatorSeries
from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import shift
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    ClosedTrade,
    CorporateActionKind,
    CorporateActionRow,
    DailyBar,
    Freeze,
    GapAcknowledgement,
    OrderAction,
    PendingOrder,
    Position,
    PositionState,
    RulesParameters,
    UniverseRow,
    WeekDecision,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import (
    SymbolWeek,
    WeekContext,
    decide_week,
    entry_snapshot,
    replay_touch_memory,
    traded_value,
    trigger,
)
from strategies.positional_stocks.wsr1_weekly_stochrsi.trading_calendar import TradingCalendar

from .paper_fills import OrderFill, fill_orders
from .repository import StockRepository, week_text


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
    #: ``corporate_actions.csv`` (spec 4.14 v1.2j).
    corporate_actions: Sequence[CorporateActionRow] = ()
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
    #: Spec 4.14: positions frozen this week, and rescales applied this week.
    frozen: tuple[Freeze, ...] = ()
    rescaled: tuple[Rescale, ...] = ()
    #: Spec 11 v1.2m: every corporate-action event of the week, in order.
    events: tuple[CorporateActionEvent, ...] = ()
    #: Spec 4.14 item 7 v1.2m: freezes that lifted with neither an
    #: acknowledgement nor a rescale — for the operator to check.
    silent_lifts: tuple[CorporateActionEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class CorporateActionEvent:
    """One corporate-action event for the report (spec 11 v1.2m)."""

    #: rescale, reissued, one_share_partial, d101_close, stuck_exit_queued,
    #: stuck_exit_filled, stuck_exit_skipped, waiting, silent_lift.
    kind: str
    symbol: str
    position_id: str
    detail: str


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
    previous = shift(ctx.week, -1)
    if (
        repository.has_runs_other_than(ctx.week_ending)
        and repository.run_status_for_week(previous) != "COMPLETED"
    ):
        raise RunOrderError(
            f"cannot decide {ctx.week_ending}: week {week_text(previous)} is not COMPLETED "
            "(spec 10.2: a skipped week is a skipped stop check)"
        )

    repository.mark_started(ctx.week_ending, ctx.week, inputs.fingerprint)
    daily = {
        symbol: [bar for bar in bars if bar.session <= ctx.week_ending]
        for symbol, bars in inputs.daily.items()
    }

    with repository.database.transaction(immediate=True) as conn:
        # Step 0: corporate actions (4.14 v1.2k, D101, D102), before anything fills.
        found = _Checks(frozen={}, rescaled=[], notes=[])
        held = [
            p for p in repository.positions(conn).values() if p.state is not PositionState.CLOSED
        ]
        _check_positions(repository, conn, held, daily, inputs, params, found)
        frozen = found.frozen

        # Step 1: fills, then clear filled orders (v1.2h). A frozen
        # position's orders wait — an old-unit sell must not fill at a
        # restated price — except its stuck-freeze exit (D102), which carries
        # the factor that puts the open in its own units.
        positions = repository.positions(conn)
        fills, filled_positions = fill_orders(
            [
                o
                for o in repository.pending_orders(conn)
                if o.position_id not in frozen or o.price_factor is not None
            ],
            positions,
            daily,
            through=ctx.week_ending,
            calendar=calendar,
            params=params,
        )
        late_buys: set[str] = set()
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
                late_fill=result.plan.late_fill,
                week=ctx.week,
            )
            if result.plan.late_fill and result.order.action.is_buy:
                late_buys.add(position.position_id)
            if outcome.closed is not None and outcome.closed.is_loss:
                until = shift(outcome.closed.exit_week, params.cooling_off_weeks)
                repository.save_cooling_off(conn, outcome.closed, until)
            if result.order.price_factor is not None:
                found.event(
                    "stuck_exit_filled",
                    position,
                    f"{STUCK_EXIT}: SELL_ALL {result.order.quantity} filled on "
                    f"{result.plan.session} at open / {result.order.price_factor:.4f}",
                )

        # v1.2l: the checks again, for every position that got a fill this
        # run — a position opened today is checked before any decision. And a
        # position that closed this run (its stuck-freeze exit filled) is
        # frozen no longer.
        current = repository.positions(conn)
        for position_id in [i for i in frozen if current[i].state is PositionState.CLOSED]:
            del frozen[position_id]
        touched = {
            result.outcome.position.position_id
            for result in fills
            if result.filled and result.outcome is not None and result.outcome.position is not None
        }
        refill = [
            current[i]
            for i in sorted(touched)
            if current[i].state is not PositionState.CLOSED and i not in frozen
        ]
        _check_positions(repository, conn, refill, daily, inputs, params, found)
        current = repository.positions(conn)
        silent = _silent_lifts(repository, current, daily, inputs, found)
        for lift in silent:
            found.notes.append(f"{lift.symbol}: freeze lifted silently — {lift.detail}")

        # Spec 8 v1.2j: the weeks between a late buy and this one were decided
        # without it; rebuild its touch memory over them. Not while frozen.
        for position_id in sorted(late_buys - set(frozen)):
            position = current[position_id]
            week_inputs = inputs.symbols.get(position.symbol)
            if week_inputs is None or position.state is not PositionState.OPEN:
                continue
            replayed = replay_touch_memory(position, week_inputs.series, before=ctx.week)
            if replayed.touch_week != position.touch_week:
                repository.save_position(conn, replayed, ctx.week)

        # Steps 2-4: decide on the book as it now stands.
        book = repository.book(conn)
        symbols = _complete(inputs, daily, repository.last_seen_rows(conn))
        decision = decide_week(ctx, book, symbols, inputs.index, params, frozen=frozen)
        if found.notes:
            decision = replace(decision, warnings=(*decision.warnings, *found.notes))

        # Step 5: persist.
        for position in decision.positions:
            repository.save_position(conn, position, ctx.week)
        for order in decision.orders:
            repository.save_order(conn, order)
            if order.action is OrderAction.BUY_T1:
                # Spec 11 v1.2m: the journal's trigger-week values, now.
                snapshot = entry_snapshot(
                    symbols[order.symbol].series, inputs.index, ctx.week, decision.regime, params
                )
                if snapshot is not None:
                    repository.save_entry_signal(conn, order, snapshot)
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
        ctx.week_ending,
        "COMPLETED",
        decision,
        tuple(fills),
        resumed=status == "STARTED",
        frozen=tuple(frozen.values()),
        rescaled=tuple(found.rescaled),
        events=tuple(found.events),
        silent_lifts=tuple(silent),
    )


#: Appended to a pending sell's reason when it is re-issued (spec 4.14 item 6).
REISSUED = "re-issued after corporate-action rescale"
#: D102: the reason of a stuck-freeze exit.
STUCK_EXIT = "exit: corporate action not adjusted by Dhan"
#: D101: the resolution of a closed position's pending orders.
CLOSED_IN_LIEU = "closed by consolidation: cash in lieu"
#: Spec 4.14 item 8 (v1.2l): a queued stuck-freeze exit whose freeze was lifted.
FREEZE_LIFTED = "freeze lifted"
#: Spec 4.14 item 6 (v1.2l): a re-issued SELL_HALF that rounds to 0 shares.
ONE_SHARE_PARTIAL = "1-share partial rule: 0 shares after rescale"
#: Spec 4.14 item 7: a confirmed row cannot apply while the cache is unrestated.
WAITING_FOR_RESTATEMENT = "CSV row present; waiting for Dhan restatement"
#: D102: an escalated freeze (item 5: more than 2 weekly runs).
STUCK_AFTER_RUNS = 3


def _check_positions(
    repository: StockRepository,
    conn: sqlite3.Connection,
    positions: Sequence[Position],
    daily: Mapping[str, Sequence[DailyBar]],
    inputs: WeekInputs,
    params: RulesParameters,
    found: _Checks,
) -> None:
    """Spec 4.14 items 1-8 and D101 for ``positions``, into ``found``.

    Run twice a week (v1.2l): over every held position before the fills, and
    over every position that got a fill this run after them — so a position
    opened this run, with a bonus going ex later that week, is checked before
    any decision.

    * An unacknowledged raw gap of 15% or more after the first fill (item 7)
      freezes the position, at its unit factor (v1.2l) — with an item-1
      restatement as well, both reasons are reported and nothing is rescaled.
    * Otherwise a restatement (item 1) is rescaled by a matching confirmed
      row, or freezes the position.
    * Neither: a queued stuck-freeze exit is skipped — the freeze was lifted.
    """
    ctx = inputs.ctx
    rows = inputs.corporate_actions
    for position in sorted(positions, key=lambda p: p.symbol):
        bars = daily.get(position.symbol, ())
        gaps = unadjusted_gaps(position, bars, inputs.acknowledgements)
        restatement = detect(position, bars)
        if gaps:
            unit = freeze_factor(position, restatement.checks if restatement else (), gaps)
            details = [gap_detail(gaps)]
            if restatement is not None:
                details.append(restatement.detail)
            elif eligible_rows(position, rows, ctx.week_ending):
                details.append(WAITING_FOR_RESTATEMENT)
                found.event("waiting", position, WAITING_FOR_RESTATEMENT)
            if unit.mixed:
                factors = ", ".join(f"{u:.4f}" for u in unit.per_fill)
                details.append(f"mixed units: buy-fill unit factors {factors}")
            _freeze(
                repository,
                conn,
                position,
                replace(
                    _NO_FREEZE,
                    position_id=position.position_id,
                    factor=unit.factor,
                    detail="; ".join(details),
                    gap_based=True,
                    mixed_units=unit.mixed,
                    gaps=tuple((gap.session, gap.ratio) for gap in gaps),
                ),
                found,
                inputs,
            )
            continue
        if restatement is None:
            if unverifiable(position, bars):
                found.notes.append(
                    f"{position.symbol}: no cached bar for any fill session; "
                    "corporate-action check impossible this run"
                )
            _skip_lifted_exit(repository, conn, position, ctx, found)
            continue
        result = resolve(position, restatement, rows, bars)
        if isinstance(result, str):
            freeze = replace(
                _NO_FREEZE,
                position_id=position.position_id,
                factor=restatement.factor,
                detail=result,
            )
            _freeze(repository, conn, position, freeze, found, inputs)
            continue
        found.rescaled.append(result)
        found.rescaled_ids.add(position.position_id)
        found.event(
            "rescale",
            position,
            f"{result.row.kind.value} {result.row.ratio} ex {result.row.ex_session}: "
            f"{result.shares_before} -> {result.shares_after} shares, "
            f"cash {result.adjustment.cash}",
        )
        if position.shares_held + result.adjustment.shares_delta == 0:
            _close_in_lieu(repository, conn, position, result, found, ctx, params)
            continue
        repository.save_corporate_action(conn, position, result, ctx.week)
        _reissue_sells(repository, conn, position.position_id, ctx, found)


@dataclass
class _Checks:
    """What the corporate-action checks found this run, across both passes."""

    frozen: dict[str, Freeze]
    rescaled: list[Rescale]
    notes: list[str]
    events: list[CorporateActionEvent] = field(default_factory=list)
    rescaled_ids: set[str] = field(default_factory=set)

    def event(self, kind: str, position: Position, detail: str) -> None:
        self.events.append(
            CorporateActionEvent(kind, position.symbol, position.position_id, detail)
        )


#: A template: every Freeze is built from it with ``replace``.
_NO_FREEZE = Freeze(position_id="", factor=Decimal("1"), detail="")


def _skip_lifted_exit(
    repository: StockRepository,
    conn: sqlite3.Connection,
    position: Position,
    ctx: WeekContext,
    found: _Checks,
) -> None:
    """Spec 4.14 item 8 (v1.2l): a queued stuck-freeze exit whose freeze was
    lifted — neither frozen nor rescaled this run, e.g. the operator
    acknowledged the gap as a real move — is skipped, and decisions resume in
    this run. (A rescale instead re-issues it through item 6.)"""
    for order in repository.pending_orders(conn):
        if order.position_id == position.position_id and order.price_factor is not None:
            repository.resolve_order(
                conn, order, state="SKIPPED", week=ctx.week, resolution=FREEZE_LIFTED
            )
            found.event("stuck_exit_skipped", position, f"queued exit SKIPPED: {FREEZE_LIFTED}")


def _freeze(
    repository: StockRepository,
    conn: sqlite3.Connection,
    position: Position,
    freeze: Freeze,
    found: _Checks,
    inputs: WeekInputs,
) -> None:
    """Freeze ``position`` (item 2) — and exit it if it is stuck (item 8).

    Stuck: frozen for 3 or more consecutive runs, and an **eligible** row
    (ex session after the first fill, on or before this week's last session)
    matches the freeze factor — within 0.5% for item 1 alone, 10% when a gap
    is part of it — but could not be applied: Dhan never restated the
    history, or only partly (MOTHERSON's 1:2 bonus was adjusted back only to
    30 Apr 2024). A SELL_ALL of its stored shares is queued for the next
    session, filling at open / **the row's** price factor — the position's
    own units — with normal sell costs. Its other pending orders give way.
    Mixed units never exit this way.
    """
    ctx = inputs.ctx
    runs = 1 + repository.frozen_streak(position.position_id, ctx.week)
    freeze = replace(freeze, runs=runs)
    # The factor item 8 matches rows against: the freeze's own, before v1.2m's
    # price-correction rule below sets the mark to the close.
    match_factor = freeze.factor
    correction = (
        None
        if freeze.mixed_units
        else price_correction_row(
            position,
            inputs.corporate_actions,
            match_factor,
            through=ctx.week_ending,
            gap_based=freeze.gap_based,
        )
    )
    if correction is not None:
        # v1.2m: a PRICE_CORRECTION changes no units — marked at the close.
        freeze = replace(
            freeze,
            factor=Decimal("1"),
            price_corrected=True,
            detail=f"{freeze.detail}; PRICE_CORRECTION {correction.ratio} ex "
            f"{correction.ex_session} matches: marked at the close",
        )
    found.frozen[position.position_id] = freeze
    if runs < STUCK_AFTER_RUNS or freeze.mixed_units:
        return
    pending = [o for o in repository.pending_orders(conn) if o.position_id == position.position_id]
    if any(o.price_factor is not None for o in pending):
        return  # its exit is already queued
    row = stuck_exit_row(
        position,
        inputs.corporate_actions,
        match_factor,
        through=ctx.week_ending,
        gap_based=freeze.gap_based,
    )
    if row is None:
        return  # nothing eligible explains it: stays frozen and escalated
    # The exit fills at open / the row's price factor — or at the open
    # itself for a PRICE_CORRECTION, which changes no units (v1.2m).
    exit_factor = (
        Decimal("1") if row.kind is CorporateActionKind.PRICE_CORRECTION else row.price_factor
    )
    exit_order = PendingOrder(
        action=OrderAction.SELL_ALL,
        symbol=position.symbol,
        decided_week=ctx.week,
        execute_on_or_after=ctx.execution_date,
        reason=STUCK_EXIT,
        position_id=position.position_id,
        quantity=position.shares_held,
        price_factor=exit_factor,
    )
    exit_id = repository.save_order(conn, exit_order)
    for order in pending:
        if order.action.is_buy:
            repository.resolve_order(
                conn, order, state="SKIPPED", week=ctx.week, resolution=f"{STUCK_EXIT}: {exit_id}"
            )
        else:
            repository.resolve_order(
                conn,
                order,
                state="SUPERSEDED",
                week=ctx.week,
                resolution=f"superseded by {exit_id}",
            )
    detail = (
        f"{STUCK_EXIT} — {row.kind.value} {row.ratio} ex {row.ex_session} matches the freeze "
        f"factor {match_factor:.4f} but cannot be applied; SELL_ALL {position.shares_held} "
        f"queued for {ctx.execution_date} at open / {exit_factor:.4f}"
    )
    found.notes.append(f"{position.symbol}: {detail}")
    found.event("stuck_exit_queued", position, detail)


def _close_in_lieu(
    repository: StockRepository,
    conn: sqlite3.Connection,
    position: Position,
    rescale: Rescale,
    found: _Checks,
    ctx: WeekContext,
    params: RulesParameters,
) -> None:
    """D101: a consolidation that floors the holding to 0 shares pays it all
    as cash in lieu at the adjusted close, and closes the position as a
    normal exit at that price — dated to the ex session's week, with no sell
    costs (the company pays it; nothing is sold on the market). P&L,
    cooling-off and re-entry follow as for any exit."""
    closed = replace(
        position,
        adjustments=(*position.adjustments, rescale.adjustment),
        state=PositionState.CLOSED,
    )
    repository.save_position(conn, closed, ctx.week)
    repository.save_corporate_action(conn, position, rescale, ctx.week)
    for order in repository.pending_orders(conn):
        if order.position_id == position.position_id:
            repository.resolve_order(
                conn, order, state="SKIPPED", week=ctx.week, resolution=CLOSED_IN_LIEU
            )
    assert closed.exit_week is not None
    trade = ClosedTrade(closed.symbol, closed.position_id, closed.exit_week, closed.net_pnl)
    if trade.is_loss:
        repository.save_cooling_off(conn, trade, shift(trade.exit_week, params.cooling_off_weeks))
    detail = (
        f"{rescale.row.kind.value} {rescale.row.ratio} floors {rescale.shares_before} shares "
        f"to 0 — closed on cash in lieu {rescale.adjustment.cash}, net {closed.net_pnl}"
    )
    found.notes.append(f"{position.symbol}: {detail}")
    found.event("d101_close", position, detail)


def _reissue_sells(
    repository: StockRepository,
    conn: sqlite3.Connection,
    position_id: str,
    ctx: WeekContext,
    found: _Checks,
) -> None:
    """Spec 4.14 item 6: the exit was decided on valid data and must still
    happen, in the new units. The re-issued sell keeps the original
    ``execute_on_or_after`` (v1.2k), so the fill model fills it exactly as
    the original would have: at that session's (restated) open, or the next
    session the symbol traded, flagged ``late_fill`` when earlier than this
    run's week. Pending buys are amount-based and proceed unchanged."""
    position = repository.positions(conn)[position_id]
    for order in repository.pending_orders(conn):
        if order.position_id != position_id or order.action.is_buy:
            continue
        held = position.shares_held
        quantity = held if order.action is OrderAction.SELL_ALL else held // 2
        if quantity == 0:
            # v1.2l: a partial of a 1-share holding sells nothing. The 1-share
            # rule of 4.10 item 4 applies instead — half-sold, adds stop, the
            # trail replaces the stop — dated to the original decision: its
            # clock starts the week that SELL_HALF would have filled.
            switched = replace(
                position,
                state=PositionState.HALF_SOLD,
                half_sold_week=shift(order.decided_week, 1),
                touch_week=None,
            )
            repository.save_position(conn, switched, ctx.week)
            repository.resolve_order(
                conn, order, state="SUPERSEDED", week=ctx.week, resolution=ONE_SHARE_PARTIAL
            )
            found.event("one_share_partial", position, ONE_SHARE_PARTIAL)
            continue
        replacement = PendingOrder(
            action=order.action,
            symbol=order.symbol,
            decided_week=ctx.week,
            execute_on_or_after=order.execute_on_or_after,
            reason=f"{order.reason}; {REISSUED}",
            position_id=position_id,
            quantity=quantity,
        )
        replacement_id = repository.save_order(conn, replacement)
        repository.resolve_order(
            conn,
            order,
            state="SUPERSEDED",
            week=ctx.week,
            resolution=f"superseded by {replacement_id}",
        )
        found.event(
            "reissued",
            position,
            f"{order.action.value} {order.quantity} re-issued as {quantity} in the new units "
            f"from {order.execute_on_or_after}",
        )


def _silent_lifts(
    repository: StockRepository,
    positions: Mapping[str, Position],
    daily: Mapping[str, Sequence[DailyBar]],
    inputs: WeekInputs,
    found: _Checks,
) -> list[CorporateActionEvent]:
    """Spec 4.14 item 7 (v1.2m): a freeze that lifted with neither an
    acknowledgement nor a rescale — frozen last run, not frozen now, no
    rescale this run, and no acknowledgement covering any gap of 15% or more
    after the first fill. A partial restatement whose boundary gap is below
    15% can do that; the operator checks the position against the exchange's
    record. Reported only: the lift logic is unchanged."""
    ctx = inputs.ctx
    lifts: list[CorporateActionEvent] = []
    for position in sorted(positions.values(), key=lambda p: p.symbol):
        pid = position.position_id
        if (
            position.state is PositionState.CLOSED
            or pid in found.frozen
            or pid in found.rescaled_ids
            or repository.frozen_streak(pid, ctx.week) == 0
        ):
            continue
        first = position.buys[0].session
        acknowledged = any(
            gap.session > first and is_acknowledged(gap, inputs.acknowledgements)
            for gap in scan(position.symbol, daily.get(position.symbol, ()))
        )
        if acknowledged:
            continue
        lifts.append(
            CorporateActionEvent(
                "silent_lift",
                position.symbol,
                pid,
                "frozen last run, not frozen now, with neither an acknowledgement nor a "
                "rescale — check the position against the exchange's corporate-action record",
            )
        )
    return lifts


def complete_symbols(
    inputs: WeekInputs,
    daily: Mapping[str, Sequence[DailyBar]],
    last_seen: Mapping[str, UniverseRow],
) -> dict[str, SymbolWeek]:
    """The ``SymbolWeek`` of every symbol, completed from stored state — as the
    rules saw them this week. For the report's watchlist and gap sections."""
    return _complete(inputs, daily, last_seen)


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
