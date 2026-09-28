"""The Telegram summary (spec 11 v1.2m): short, no tables, no secrets.

One message per invocation, covering every week it processed: the final
regime, equity and drawdown, the orders for the next session, the exits of
all the weeks, and **the number of operator actions** — frozen or escalated
positions, freezes lifted with neither an acknowledgement nor a rescale,
candidates waiting for a quality row, and held or pending symbols stale for
2 or more weeks (decision 5: a suspended stock cannot hit its stop).

Sent through the existing notifier, wrapped in ``SafeNotifier``: a missing
or failing channel is non-fatal (spec 10.3 rule 4).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from common.config.models import ExecutionMode
from common.notifications.base import NotificationEvent, Notifier
from strategies.positional_stocks.wsr1_weekly_stochrsi.rules import NEEDS_QUALITY as NEEDS_QUALITY

from .journal import exit_type
from .report import ReportData, failed_kept, week_label


@dataclass(frozen=True)
class OperatorActions:
    frozen: int
    escalated: int
    silent_lifts: int
    needs_quality: int
    stale: int
    #: Fetch mode's preview: symbols whose refresh failed (Phase 4b-2).
    fetch_failed: int = 0

    @property
    def total(self) -> int:
        return self.frozen + self.silent_lifts + self.needs_quality + self.stale + self.fetch_failed


def operator_actions(data: ReportData) -> OperatorActions:
    frozen = [row.freeze for row in data.positions if row.freeze is not None]
    stale = {
        s.symbol for record in data.weeks[-1:] for s in record.stale if s.kept and s.weeks >= 2
    }
    return OperatorActions(
        frozen=len(frozen),
        escalated=sum(1 for f in frozen if f.escalated),
        silent_lifts=sum(len(r.outcome.silent_lifts) for r in data.weeks),
        needs_quality=sum(1 for e in data.decision.funnel if e.reason == NEEDS_QUALITY),
        stale=len(stale),
        fetch_failed=len(data.preview.failed) if data.preview is not None else 0,
    )


def exits(data: ReportData) -> list[str]:
    """Every exit filled in the weeks processed, plus consolidation closes."""
    out: list[str] = []
    for record in data.weeks:
        for result in record.outcome.fills:
            outcome = result.outcome
            if outcome is None or outcome.closed is None:
                continue
            label = exit_type(result.order.reason).replace("_", " ")
            out.append(f"{outcome.closed.symbol} {label} {_signed(outcome.closed.net_pnl)}")
        out += [
            f"{e.symbol} consolidation close"
            for e in record.outcome.events
            if e.kind == "d101_close"
        ]
    return out


def _signed(value: Decimal) -> str:
    return f"{'+' if value >= 0 else '-'}Rs {abs(value):,.2f}"


def summary_text(data: ReportData) -> str:
    decision = data.decision
    weeks = ", ".join(week_label(r.week) for r in data.weeks)
    orders = Counter(o.action.value for o in data.pending)
    order_parts = []
    for action, count in sorted(orders.items()):
        symbols = ", ".join(sorted(o.symbol for o in data.pending if o.action.value == action))
        order_parts.append(f"{count} {action} ({symbols})")
    order_text = "; ".join(order_parts) or "none"
    actions = operator_actions(data)
    lines = [
        ("PREVIEW — " if data.preview is not None else "") + f"Weekly run — {weeks}",
        f"Regime: {decision.regime.value} | Equity Rs {decision.equity:,.2f} "
        f"(drawdown {data.drawdown_pct}%)",
        f"Next session: {order_text}",
        f"Exits: {', '.join(exits(data)) or 'none'}",
        f"Operator actions: {actions.total} (frozen {actions.frozen}, escalated "
        f"{actions.escalated}, silent freeze lifts {actions.silent_lifts}, waiting for a "
        f"quality row {actions.needs_quality}, stale 2+ weeks {actions.stale}"
        + (f", fetch failed {actions.fetch_failed}" if data.preview is not None else "")
        + ")",
    ]
    if data.preview is not None:
        for symbol, held in failed_kept(data, data.preview):
            what = "held: its stop cannot be checked" if held else "pending order"
            lines.insert(1, f"⚠ FETCH FAILED {symbol} ({what})")
    if data.execution_note:
        lines.insert(2, data.execution_note[0].upper() + data.execution_note[1:])
    if decision.entries_blocked:
        lines.append(f"Entries blocked: {decision.entries_blocked}")
    if data.stopped:
        lines.append(f"STOPPED: {data.stopped}")
    return "\n".join(lines)


def send_summary(notifier: Notifier, data: ReportData, *, runtime_id: str, strategy_id: str) -> str:
    """Send once; return a status line for the report. Never raises."""
    actions = operator_actions(data)
    required = (
        f"{actions.total} operator action(s): see the weekly report" if actions.total else None
    )
    if data.stopped:
        required = "the run stopped part-way: see the weekly report, then re-run"
    event = NotificationEvent(
        event_type="weekly_preview" if data.preview is not None else "weekly_summary",
        message=summary_text(data),
        runtime_id=runtime_id,
        strategy_id=strategy_id,
        execution_mode=ExecutionMode.PAPER,
        required_action=required,
    )
    return _deliver(notifier, event)


def send_alert(notifier: Notifier, message: str, *, runtime_id: str, strategy_id: str) -> str:
    event = NotificationEvent(
        event_type="weekly_run_no_trades",
        message=message,
        runtime_id=runtime_id,
        strategy_id=strategy_id,
        execution_mode=ExecutionMode.PAPER,
        required_action="check the weekly report; re-run by hand once the cause is fixed",
    )
    return _deliver(notifier, event)


def _deliver(notifier: Notifier, event: NotificationEvent) -> str:
    try:
        ok = notifier.send(event)
    except Exception as exc:
        return f"NOT sent — {type(exc).__name__} (the run itself is unaffected)"
    if ok:
        return f"sent via {notifier.channel}"
    return f"NOT sent — channel {notifier.channel} did not deliver (the run itself is unaffected)"
