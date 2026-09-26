"""Corporate actions on held positions (spec 4.14 v1.2j) — pure.

Dhan back-adjusts history for bonuses, splits and (sometimes) demergers, but a
held position's P1, levels, share count and fill prices are in the units of
the fill. Left alone, a 1:1 bonus halves the adjusted close under an unchanged
stop: a false stop, a false loss, a false cooling-off, a false drawdown. The
gap scan cannot see it — a correctly adjusted series has no gap.

* :func:`detect` compares every fill of an open position, in current units,
  with the cached open of its session. More than 0.5% apart means the history
  was restated, by ``f = cached open / fill price``.
* Until confirmed, the position is **frozen** (the rules make no exit, add or
  partial decision for it and mark it at ``close / f``).
* :func:`resolve` turns an operator row of ``corporate_actions.csv`` that
  matches the detected factor into a :class:`Rescale`. ``DEMERGER`` covers
  any price-only restatement (a special dividend Dhan adjusts for, say).

No I/O, no clock: the runtime supplies the cached bars and the rows.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal

from .models import (
    BuyFill,
    CorporateActionKind,
    CorporateActionRow,
    DailyBar,
    OrderAction,
    Position,
    PositionState,
    SaleFill,
    ShareAdjustment,
    money,
)

#: Spec 4.14: a relative difference above 0.5% is a restatement, and a
#: confirmed ratio must match the detected factor within the same 0.5%.
TOLERANCE = Decimal("0.005")


def _action(fill: BuyFill | SaleFill) -> OrderAction:
    return fill.action if isinstance(fill, SaleFill) else OrderAction.buy(fill.tranche)


def _close_to(a: Decimal, b: Decimal) -> bool:
    return abs(a / b - 1) <= TOLERANCE


@dataclass(frozen=True, slots=True)
class FillCheck:
    """One fill against the cached open of its session."""

    action: OrderAction
    session: date
    price: Decimal
    #: ``None`` when the cache has no bar for the session.
    cached_open: Decimal | None

    @property
    def factor(self) -> Decimal | None:
        return None if self.cached_open is None else self.cached_open / self.price

    @property
    def restated(self) -> bool:
        factor = self.factor
        return factor is not None and abs(factor - 1) > TOLERANCE


@dataclass(frozen=True, slots=True)
class Restatement:
    """A held position whose cached history no longer matches its fills."""

    position_id: str
    symbol: str
    #: ``f`` of the earliest restated fill.
    factor: Decimal
    checks: tuple[FillCheck, ...]

    @property
    def consistent(self) -> bool:
        """Every restated fill shows the same factor (within 0.5%). Two
        stacked actions do not, and one CSV row cannot resolve them."""
        return all(
            _close_to(check.factor, self.factor)  # type: ignore[arg-type]
            for check in self.checks
            if check.restated
        )

    @property
    def detail(self) -> str:
        sessions = ", ".join(str(c.session) for c in self.checks if c.restated)
        text = f"history restated by f={self.factor:.4f} at fill session(s) {sessions}"
        if not self.consistent:
            text += "; the restated fills disagree (more than one action?)"
        return text


def checks(position: Position, daily: Iterable[DailyBar]) -> tuple[FillCheck, ...]:
    """Every fill of ``position``, in current units, against the cache."""
    opens = {bar.session: Decimal(str(bar.open)) for bar in daily}
    fills: list[BuyFill | SaleFill] = [*position.buys, *position.sales]
    return tuple(
        FillCheck(
            _action(fill), fill.session, position.current_price(fill), opens.get(fill.session)
        )
        for fill in sorted(fills, key=lambda f: f.session)
    )


def detect(position: Position, daily: Iterable[DailyBar]) -> Restatement | None:
    """Spec 4.14 item 1, for one open position; ``None`` if nothing moved."""
    if position.state is PositionState.CLOSED:
        return None
    found = checks(position, daily)
    restated = [check for check in found if check.restated]
    if not restated:
        return None
    factor = restated[0].factor
    assert factor is not None
    return Restatement(position.position_id, position.symbol, factor, found)


def unverifiable(position: Position, daily: Iterable[DailyBar]) -> bool:
    """No fill session of an open position is in the cache: nothing to compare."""
    if position.state is PositionState.CLOSED:
        return False
    return all(check.cached_open is None for check in checks(position, daily))


@dataclass(frozen=True, slots=True)
class Rescale:
    """A matched confirmation, ready to store (spec 4.14 item 4)."""

    row: CorporateActionRow
    adjustment: ShareAdjustment
    detected_factor: Decimal
    shares_before: int
    shares_after: int
    #: The cached (restated) close of the last session before the ex session.
    reference_close: Decimal


def resolve(
    position: Position,
    restatement: Restatement,
    rows: Iterable[CorporateActionRow],
    daily: Sequence[DailyBar],
) -> Rescale | str:
    """The rescale a confirmed row justifies, or why the position stays frozen.

    A row matches only when its factor agrees with the detected one within
    0.5% (BONUS_SPLIT: 1 / ratio; DEMERGER: ratio), every fill before its ex
    session was restated and every fill on or after it was not, it is later
    than any action already applied, and the cache has a session before it.
    """
    if not restatement.consistent:
        return restatement.detail
    applied = max((a.ex_session for a in position.adjustments), default=date.min)
    candidates = sorted(
        (
            row
            for row in rows
            if row.symbol == position.symbol
            and row.ex_session > applied
            and row.ex_session > position.buys[0].session
        ),
        key=lambda row: row.ex_session,
    )
    if not candidates:
        return f"{restatement.detail}; no confirmed row in corporate_actions.csv"
    refusals: list[str] = []
    for row in candidates:
        why = _mismatch(row, restatement, daily)
        if why is None:
            return _rescale(position, restatement, row, daily)
        refusals.append(f"{row.kind.value} {row.ratio} ex {row.ex_session}: {why}")
    return f"{restatement.detail}; confirmed row does not match — " + "; ".join(refusals)


def _pre_ex_bar(daily: Sequence[DailyBar], ex_session: date) -> DailyBar | None:
    before = [bar for bar in daily if bar.session < ex_session]
    return max(before, key=lambda bar: bar.session) if before else None


def _mismatch(
    row: CorporateActionRow, restatement: Restatement, daily: Sequence[DailyBar]
) -> str | None:
    if not _close_to(restatement.factor, row.price_factor):
        return f"expects f={row.price_factor:.4f}, detected f={restatement.factor:.4f}"
    for check in restatement.checks:
        if check.cached_open is None:
            continue
        if check.session < row.ex_session and not check.restated:
            return f"the fill on {check.session}, before the ex session, was not restated"
        if check.session >= row.ex_session and check.restated:
            return f"the fill on {check.session}, on or after the ex session, was restated"
    if _pre_ex_bar(daily, row.ex_session) is None:
        return "no cached session before the ex session"
    return None


def _rescale(
    position: Position, restatement: Restatement, row: CorporateActionRow, daily: Sequence[DailyBar]
) -> Rescale:
    applies_to = tuple(c.action for c in restatement.checks if c.session < row.ex_session)
    # Shares held in the old units: fills on or after the ex session are
    # already in the new ones.
    later_buys = sum(b.shares for b in position.buys if _action(b) not in applies_to)
    later_sales = sum(s.shares for s in position.sales if _action(s) not in applies_to)
    before = position.shares_held - later_buys + later_sales
    bar = _pre_ex_bar(daily, row.ex_session)
    assert bar is not None
    reference = Decimal(str(bar.close))
    if row.kind is CorporateActionKind.BONUS_SPLIT:
        exact = before * row.ratio
        after = int(exact.to_integral_value(rounding=ROUND_FLOOR))
        # Cash in lieu of the fractional share, at the adjusted close.
        cash = money((exact - after) * reference)
    else:
        after = before
        # The value the demerger removed, at the actual pre-ex close (the
        # cache is already scaled by the ratio): it stands in for the
        # demerged company's shares, which the paper book does not hold.
        cash = money(before * (reference / row.ratio) * (1 - row.ratio))
    adjustment = ShareAdjustment(
        ex_session=row.ex_session,
        kind=row.kind,
        ratio=row.ratio,
        applies_to=applies_to,
        shares_delta=after - before,
        cash=cash,
    )
    return Rescale(row, adjustment, restatement.factor, before, after, reference)


def rescale_levels(level: Decimal, adjustments: Iterable[ShareAdjustment]) -> Decimal:
    """P1, L1, L2 or Stop in current units: every applied action's price
    factor, rounded to the paisa once."""
    factor = Decimal("1")
    for adjustment in adjustments:
        factor *= adjustment.price_factor
    return level if factor == 1 else money(level * factor)
