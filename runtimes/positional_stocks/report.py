"""The weekly report (spec 11 v1.2m) — pure: data in, markdown out.

Written to ``data/reports/positional_stocks/<week_ending>.md`` (a dry run to
``<week_ending>-dry-run.md``, marked DRY RUN, so a real report is never
overwritten). When one invocation catches up several weeks, fills, closed
trades, corporate-action events and warnings are listed **per week**;
positions, pending orders, equity, brakes, the funnel and the watchlist show
the final week.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from strategies.positional_stocks.wsr1_weekly_stochrsi.gaps import Gap
from strategies.positional_stocks.wsr1_weekly_stochrsi.iso_weeks import WeekKey
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import (
    Freeze,
    FunnelStage,
    OrderAction,
    PendingOrder,
    Position,
    PositionState,
    WatchEntry,
    WeekDecision,
    money,
)

from .accounting import STUCK_EXIT, CorporateActionEvent, WeekOutcome
from .paper_fills import OrderFill
from .week_inputs import StaleSymbol

#: Spec 4.14 item 5 / decision 5: highlighted for the operator.
FLAG = "⚠"


def week_label(week: WeekKey) -> str:
    return f"{week[0]}-W{week[1]:02d}"


@dataclass(frozen=True)
class OpenPosition:
    position: Position
    #: The mark: the week's close, over the freeze factor when frozen.
    mark: Decimal | None
    weeks_held: int
    freeze: Freeze | None = None
    stale_weeks: int = 0


@dataclass(frozen=True)
class WeekRecord:
    """One processed week, for the per-week sections."""

    week: WeekKey
    week_ending: date
    outcome: WeekOutcome
    stale: tuple[StaleSymbol, ...] = ()
    unlisted_sessions: tuple[date, ...] = ()
    warnings: tuple[str, ...] = ()
    #: Close-to-close gaps of 15% or more inside this week, all symbols.
    week_gaps: tuple[Gap, ...] = ()


@dataclass(frozen=True)
class ReportData:
    strategy_id: str
    generated_at: datetime
    dry_run: bool
    weeks: tuple[WeekRecord, ...]
    decision: WeekDecision
    cash: Decimal
    drawdown_pct: Decimal
    positions: tuple[OpenPosition, ...]
    pending: tuple[PendingOrder, ...]
    #: Held positions by id, for the levels of pending adds and sells.
    held: dict[str, Position] = field(default_factory=dict)
    watchlist: tuple[WatchEntry, ...] = ()
    #: Symbols whose unacknowledged gap blocks new entries (spec 6.1).
    blocking_gaps: tuple[Gap, ...] = ()
    notifier_status: str | None = None
    today: date | None = None


def _rs(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f} pp"


def _money(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _pct(value: float | Decimal | None) -> str:
    return "—" if value is None else f"{float(value) * 100:.2f}%"


def render(data: ReportData) -> str:
    final = data.weeks[-1]
    title = f"# {data.strategy_id} — week {week_label(final.week)} (ending {final.week_ending})"
    lines = [title + (" — DRY RUN" if data.dry_run else ""), ""]
    if data.dry_run:
        lines += ["**DRY RUN** — computed on a copy of the book; nothing was persisted.", ""]
    processed = ", ".join(week_label(w.week) for w in data.weeks)
    lines += [f"Generated {data.generated_at.isoformat()} · weeks processed: {processed}", ""]
    lines += _state(data)
    lines += _fills(data)
    lines += _pending(data)
    lines += _positions(data)
    lines += _closed(data)
    lines += _funnel(data)
    lines += _watchlist(data)
    lines += _corporate_actions(data)
    lines += _flags(data)
    lines += _warnings(data)
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------ 1. state
def _state(data: ReportData) -> list[str]:
    d = data.decision
    b = d.brakes
    return [
        "## 1. Regime, equity and brakes",
        "",
        f"- Regime: **{d.regime.value}**",
        f"- Equity: {_money(d.equity)} (cash {_money(data.cash)}, positions "
        f"{_money(d.equity - data.cash)})",
        f"- Peak: {_money(b.peak)} · drawdown {data.drawdown_pct}%",
        f"- Brake 1: {'paused through ' + week_label(b.brake1_until) if b.brake1_until else 'off'}"
        f" (can fire: {'yes' if b.brake1_can_fire else 'no'})",
        f"- Brake 2: {'ACTIVE since ' + str(b.brake2_fired_on) if b.brake2_fired_on else 'off'}",
        f"- New entries: {d.entries_blocked or 'allowed'}",
        "",
    ]


# ------------------------------------------------------------ 2. fills
def _fill_flags(result: OrderFill) -> str:
    assert result.plan is not None
    flags = []
    if result.plan.late_fill:
        flags.append("late_fill")
    if result.plan.not_traded_on_execution_session:
        flags.append("not traded on the execution session")
    if result.order.price_factor is not None:
        flags.append(f"stuck-freeze exit at open / {result.order.price_factor:.4f}")
    return ", ".join(flags)


def _fill_line(result: OrderFill) -> str | None:
    outcome = result.outcome
    if outcome is None or outcome.skipped is not None or outcome.position is None:
        return None
    assert result.plan is not None
    position = outcome.position
    order = result.order
    if order.action.is_buy:
        buy = position.buys[order.action.tranche - 1]
        shares, price, fees = buy.shares, buy.price, buy.fees
    else:
        sale = position.sales[-1]
        shares, price, fees = sale.shares, sale.price, sale.fees
    return (
        f"| {result.plan.session} | {order.symbol} | {order.action.value} | {shares} | "
        f"{_money(price)} | {_money(fees)} | {_fill_flags(result)} |"
    )


def _fills(data: ReportData) -> list[str]:
    lines = ["## 2. Fills since the last run", ""]
    for record in data.weeks:
        rows = [line for r in record.outcome.fills if (line := _fill_line(r)) is not None]
        skipped = [r for r in record.outcome.fills if r.skipped]
        lines.append(f"### {week_label(record.week)}")
        lines.append("")
        if rows:
            lines += [
                "| Session | Symbol | Action | Shares | Price | Fees | Flags |",
                "|---|---|---|---|---|---|---|",
                *rows,
            ]
        else:
            lines.append("No fills.")
        for r in skipped:
            assert r.outcome is not None
            lines.append(f"- {r.order.symbol} {r.order.action.value} skipped: {r.outcome.skipped}")
        lines.append("")
    return lines


# ---------------------------------------------------- 3. pending orders
def _levels(order: PendingOrder, held: dict[str, Position]) -> str:
    if order.action is OrderAction.BUY_T1:
        assert order.sizing is not None
        return f"s {_pct(order.sizing.spacing)} — levels set at the fill"
    position = held.get(order.position_id or "")
    if position is None:
        return "—"
    return (
        f"P1 {_money(position.p1)} · L1 {_money(position.l1)} · L2 {_money(position.l2)} · "
        f"Stop {_money(position.stop)}"
    )


def _pending(data: ReportData) -> list[str]:
    lines = ["## 3. Pending orders for the next session", ""]
    if not data.pending:
        return [*lines, "None.", ""]
    lines += [
        "| Symbol | Action | On or after | Amount / qty | Levels | Reason |",
        "|---|---|---|---|---|---|",
    ]
    for order in sorted(data.pending, key=lambda o: (o.symbol, o.action.value)):
        size = _money(order.amount) if order.amount is not None else f"{order.quantity} shares"
        lines.append(
            f"| {order.symbol} | {order.action.value} | {order.execute_on_or_after} | {size} | "
            f"{_levels(order, data.held)} | {order.reason} |"
        )
    return [*lines, ""]


# ---------------------------------------------------- 4. open positions
def _positions(data: ReportData) -> list[str]:
    lines = ["## 4. Open positions", ""]
    if not data.positions:
        return [*lines, "None.", ""]
    lines += [
        "| Symbol | State | Shares | P1 | s | L1 | L2 | Stop / trail | Mark | Unrealised | "
        "Weeks held | Flags |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in data.positions:
        p = row.position
        stop = "trail (10W EMA)" if p.state is PositionState.HALF_SOLD else _money(p.stop)
        unrealised = (
            None
            if row.mark is None
            else money(row.mark * p.shares_held - p.average_cost * p.shares_held)
        )
        flags = []
        if row.freeze is not None:
            flags.append(f"{FLAG} FROZEN" if row.freeze.escalated else "frozen")
        if row.stale_weeks:
            mark = FLAG + " " if row.stale_weeks >= 2 else ""
            flags.append(f"{mark}no bar {row.stale_weeks} week(s): cannot hit its stop")
        lines.append(
            f"| {p.symbol} | {p.state.value} | {p.shares_held} | {_money(p.p1)} | "
            f"{_pct(p.sizing.spacing)} | {_money(p.l1)} | {_money(p.l2)} | {stop} | "
            f"{_money(row.mark)} | {_money(unrealised)} | {row.weeks_held} | "
            f"{', '.join(flags)} |"
        )
    return [*lines, ""]


# ---------------------------------------------------- 5. closed trades
def _closed_lines(record: WeekRecord) -> list[str]:
    lines = []
    for result in record.outcome.fills:
        outcome = result.outcome
        if outcome is None or outcome.closed is None or outcome.position is None:
            continue
        assert result.plan is not None
        position = outcome.position
        sale = position.sales[-1]
        line = (
            f"- **{position.symbol}** ({position.position_id}) closed {sale.session} "
            f"@ {_money(sale.price)} x {sale.shares} — net {_money(position.net_pnl)} — "
            f"{result.order.reason}"
        )
        lines.append(line)
        if result.order.reason.startswith("stop:"):
            lines += _stop_detail(position, sale.price, sale.shares)
    for event in record.outcome.events:
        if event.kind == "d101_close":
            lines.append(f"- **{event.symbol}** ({event.position_id}) — {event.detail}")
    return lines


def _stop_detail(position: Position, fill: Decimal, shares: int) -> list[str]:
    """Spec 8: a stop exit shows the planned loss (at the stored Stop) and the
    actual loss (at the fill), and flags a gap through the stop."""
    cost = position.average_cost
    planned = money((position.stop - cost) * shares)
    actual = money((fill - cost) * shares)
    gapped = fill < position.stop
    note = f" — {FLAG} gapped through the stop" if gapped else ""
    return [
        f"  - planned loss at Stop {_money(position.stop)}: {_money(planned)} · actual at the "
        f"fill {_money(fill)}: {_money(actual)} (before costs){note}"
    ]


def _closed(data: ReportData) -> list[str]:
    lines = ["## 5. Closed trades", ""]
    for record in data.weeks:
        lines.append(f"### {week_label(record.week)}")
        lines.append("")
        lines += _closed_lines(record) or ["None."]
        lines.append("")
    return lines


# ------------------------------------------------------------ 6. funnel
_STAGE_ORDER = (
    FunnelStage.TAKEN,
    FunnelStage.NOT_TAKEN,
    FunnelStage.FILTERED,
    FunnelStage.ARMED,
    FunnelStage.UNDEFINED,
    FunnelStage.SKIPPED,
)


def _funnel(data: ReportData) -> list[str]:
    funnel = data.decision.funnel
    lines = ["## 6. Trigger funnel", ""]
    triggered = [e for e in funnel if e.stage in (FunnelStage.TAKEN, FunnelStage.NOT_TAKEN)]
    filtered = [e for e in funnel if e.stage is FunnelStage.FILTERED]
    lines.append(
        f"Triggered {len(triggered) + len(filtered)} → passed filters {len(triggered)} → "
        f"taken {sum(1 for e in funnel if e.stage is FunnelStage.TAKEN)}; "
        f"not armed {sum(1 for e in funnel if e.stage is FunnelStage.NOT_ARMED)}."
    )
    lines.append("")
    for stage in _STAGE_ORDER:
        entries = [e for e in funnel if e.stage is stage]
        if not entries:
            continue
        lines.append(f"**{stage.value}** ({len(entries)})")
        for entry in sorted(entries, key=lambda e: (e.rs is None, -(e.rs or 0.0), e.symbol)):
            flags = f" [{', '.join(entry.flags)}]" if entry.flags else ""
            lines.append(f"- {entry.symbol} (RS {_rs(entry.rs)}) — {entry.reason}{flags}")
        lines.append("")
    return lines


# --------------------------------------------------------- 7. watchlist
def _watchlist(data: ReportData) -> list[str]:
    lines = ["## 7. Watchlist (armed, K ≤ D, K < 50, filters pass — by RS)", ""]
    if not data.watchlist:
        return [*lines, "None.", ""]
    lines += ["| # | Symbol | RS | K | D |", "|---|---|---|---|---|"]
    for n, entry in enumerate(data.watchlist, 1):
        lines.append(f"| {n} | {entry.symbol} | {_rs(entry.rs)} | {entry.k:.2f} | {entry.d:.2f} |")
    return [*lines, ""]


# ------------------------------------------------- 8. corporate actions
def resolution_lines(symbol: str, session: date, ratio: Decimal, today: date) -> tuple[str, str]:
    """The two lines an item-7 freeze can be resolved with (spec 11 v1.2m).

    The acknowledgement is ready to paste — for a **real move only**. The
    corporate-action template is deliberately **not** loadable as it stands:
    its ratio is placeholder text, which the fail-closed loader rejects, so an
    unedited paste can never be read as a confirmation.
    """
    suggested = (1 / ratio).quantize(Decimal("0.01")) if ratio else Decimal("0")
    ack = f"{symbol},{session},{ratio},{today},real move: <why>"
    template = (
        f"{symbol},{session},BONUS_SPLIT,<new shares per old share, ~{suggested}; check the "
        f"exchange record>,{today},<note>"
    )
    return ack, template


_EVENT_TITLES = {
    "rescale": "rescaled",
    "reissued": "sell re-issued in the new units",
    "one_share_partial": "1-share partial rule",
    "d101_close": "consolidation close (D101)",
    "stuck_exit_queued": "stuck-freeze exit QUEUED",
    "stuck_exit_filled": "stuck-freeze exit FILLED",
    "stuck_exit_skipped": "stuck-freeze exit SKIPPED (freeze lifted)",
    "waiting": "waiting for Dhan restatement",
}


def _event_line(event: CorporateActionEvent) -> str:
    title = _EVENT_TITLES.get(event.kind, event.kind)
    return f"- {event.symbol} ({event.position_id}) — {title}: {event.detail}"


def _corporate_actions(data: ReportData) -> list[str]:
    lines = ["## 8. Corporate actions (spec 4.14)", ""]
    frozen = [row for row in data.positions if row.freeze is not None]
    today = data.today or data.generated_at.date()
    if frozen:
        lines.append("**Frozen positions** — no exit, add or partial until resolved:")
        lines.append("")
    for row in frozen:
        freeze = row.freeze
        assert freeze is not None
        mark = f"**{FLAG} ESCALATED — operator action.** " if freeze.escalated else ""
        lines.append(
            f"- {mark}{row.position.symbol} ({row.position.position_id}): factor "
            f"{freeze.factor:.4f}, frozen {freeze.runs} run(s) — {freeze.detail}"
        )
        for session, ratio in freeze.gaps:
            ack, template = resolution_lines(row.position.symbol, session, ratio, today)
            lines += [
                f"  - gap {session} ratio {ratio}. **Real move only** — add to "
                "`gap_acknowledgements.csv`:",
                "    ```",
                f"    {ack}",
                "    ```",
                "    **Corporate action** — add to `corporate_actions.csv` (edit the ratio "
                "first; the placeholder is refused):",
                "    ```",
                f"    {template}",
                "    ```",
            ]
    if frozen:
        lines.append("")
    any_events = False
    for record in data.weeks:
        events = record.outcome.events
        lifts = record.outcome.silent_lifts
        if not events and not lifts:
            continue
        any_events = True
        lines.append(f"### {week_label(record.week)}")
        lines.append("")
        lines += [_event_line(e) for e in events]
        for lift in lifts:
            lines.append(
                f"- **{FLAG} {lift.symbol} ({lift.position_id}) — freeze lifted with neither an "
                f"acknowledgement nor a rescale:** {lift.detail}"
            )
        lines.append("")
    if not frozen and not any_events:
        lines += ["None.", ""]
    return lines


# -------------------------------------------- 9. fill flags and gaps
#: A symbol with more blocking gaps than this is summarised on one line
#: (PATANJALI's corrupt 2016-2020 bars alone are over 100).
_GAPS_LISTED = 3


def _flags(data: ReportData) -> list[str]:
    lines = ["## 9. Fill flags, partials and gaps", ""]
    flagged = [
        f"- {week_label(r.week)}: {line}"
        for r in data.weeks
        for f in r.outcome.fills
        if f.plan is not None
        and f.outcome is not None
        and f.outcome.skipped is None
        and (f.plan.late_fill or f.plan.not_traded_on_execution_session)
        and (
            line := f"{f.order.symbol} {f.order.action.value} on {f.plan.session}: {_fill_flags(f)}"
        )
    ]
    lines.append("**late_fill and not-traded fills:** " + ("" if flagged else "none."))
    lines += flagged
    partial = [
        f"- {r.symbol} ({r.position_id}): {r.reason}"
        for r in data.decision.reviews
        if "partial sold 0" in r.flags
    ]
    lines.append("")
    lines.append('**"partial sold 0":** ' + ("" if partial else "none."))
    lines += partial
    lines.append("")
    lines.append(
        "**Blocking gaps** (unacknowledged, ≥ 30%, within 520 weeks — no new entries): "
        + ("" if data.blocking_gaps else "none.")
    )
    by_symbol: dict[str, list[Gap]] = {}
    for gap in data.blocking_gaps:
        by_symbol.setdefault(gap.symbol, []).append(gap)
    for symbol, gaps in sorted(by_symbol.items()):
        if len(gaps) <= _GAPS_LISTED:
            lines += [f"- {g.symbol} {g.session} ratio {g.ratio}" for g in gaps]
            continue
        latest = max(gaps, key=lambda g: g.session)
        first = min(g.session for g in gaps)
        lines.append(
            f"- {symbol}: {len(gaps)} gaps from {first} to {latest.session} "
            f"(latest ratio {latest.ratio})"
        )
    lines.append("")
    week_gaps = [(r, g) for r in data.weeks for g in r.week_gaps]
    lines.append("**Gaps ≥ 15% this week:** " + ("" if week_gaps else "none."))
    for record, gap in week_gaps:
        kind = "flagged ≥ 30%" if gap.flagged else "reported"
        lines.append(
            f"- {week_label(record.week)}: {gap.symbol} {gap.session} ratio {gap.ratio} ({kind})"
        )
    return [*lines, ""]


# --------------------------------------------------------- 10. warnings
def _warnings(data: ReportData) -> list[str]:
    lines = ["## 10. Data and input warnings", ""]
    for record in data.weeks:
        items: list[str] = []
        for stale in record.stale:
            action = (
                "kept: marked at its last close, no decision" if stale.kept else "skipped this week"
            )
            flag = f"{FLAG} " if stale.kept and stale.weeks >= 2 else ""
            items.append(
                f"{flag}stale ({stale.weeks} week(s)) — {stale.reason}; {action}"
                + (" — a suspended stock cannot hit its stop" if flag else "")
            )
        items += [f"unlisted session present: {day}" for day in record.unlisted_sessions]
        items += list(record.warnings)
        decision = record.outcome.decision
        if decision is not None:
            items += list(decision.warnings)
        lines.append(f"### {week_label(record.week)}")
        lines.append("")
        lines += [f"- {item}" for item in items] or ["None."]
        lines.append("")
    if data.notifier_status:
        lines += [f"- Telegram: {data.notifier_status}", ""]
    return lines


def render_failure(
    *,
    strategy_id: str,
    generated_at: datetime,
    week: WeekKey | None,
    kind: str,
    reason: str,
    dry_run: bool,
    notifier_status: str | None = None,
) -> str:
    """A run that decided nothing: cold cache, deadline, refusal."""
    label = week_label(week) if week is not None else "—"
    lines = [
        f"# {strategy_id} — week {label} — NO TRADES ({kind})" + (" — DRY RUN" if dry_run else ""),
        "",
        f"Generated {generated_at.isoformat()}",
        "",
        f"**{kind}:** {reason}",
        "",
        "Nothing was decided or persisted for this week. The next scheduled attempt retries; "
        "the operator may re-run by hand.",
    ]
    if notifier_status:
        lines += ["", f"- Telegram: {notifier_status}"]
    return "\n".join(lines) + "\n"


__all__ = [
    "STUCK_EXIT",
    "OpenPosition",
    "ReportData",
    "WeekRecord",
    "render",
    "render_failure",
    "resolution_lines",
]
