"""``PositionalMultiLegEngine._checkpoint_leg_marks`` — the per-evaluation write.

Driven directly rather than through a full engine run: the checkpoint's whole
contract is *which* legs it writes and *what it does when the write fails*,
and a real feed/strategy/broker composition would obscure both.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from common.engine.multi_leg_models import LegInstance, LegRole, LegState
from common.engine.positional.positional_engine import PositionalMultiLegEngine
from common.models import OrderSide

IST = ZoneInfo("Asia/Kolkata")
CYCLE_ID = "positional_options:weekly_delta_neutral:paper:NIFTY:2026-09-15"
TS = datetime(2026, 9, 9, 10, 58, tzinfo=IST)


def _leg(
    leg_id: str,
    *,
    state: LegState = LegState.OPEN,
    last_price: float | None = 20.0,
    side: OrderSide = OrderSide.SELL,
) -> LegInstance:
    leg = LegInstance(
        leg_id=leg_id,
        basket_id=CYCLE_ID,
        role=LegRole.SHORT_CALL,
        sequence=1,
        is_replacement=False,
        side=side,
        quantity=1300,
        state=state,
        entry_price=38.55,
    )
    if last_price is not None:
        leg.update_price(last_price)
    return leg


class _FakeCycle:
    """Only what the checkpoint touches."""

    def __init__(self, legs: list[LegInstance]) -> None:
        self.cycle_id = CYCLE_ID
        self._legs = legs

    def open_legs(self) -> list[LegInstance]:
        return [leg for leg in self._legs if leg.state is LegState.OPEN]


def _engine(callback, legs: list[LegInstance] | None) -> PositionalMultiLegEngine:
    """A bare instance whose only wired collaborators are the two the
    checkpoint reads. ``__init__`` is bypassed deliberately — constructing a
    real engine needs a feed, broker, strategy and repository, none of which
    this method touches."""
    engine = PositionalMultiLegEngine.__new__(PositionalMultiLegEngine)
    engine._persist_leg_mark_cb = callback  # type: ignore[attr-defined]
    engine._cycle = _FakeCycle(legs) if legs is not None else None  # type: ignore[attr-defined]
    engine.label = "weekly_delta_neutral"  # type: ignore[attr-defined]
    return engine


def test_writes_one_mark_per_open_leg() -> None:
    written: list[tuple[str, str]] = []
    legs = [_leg("leg-1"), _leg("leg-2")]
    engine = _engine(lambda cycle_id, leg: written.append((cycle_id, leg.leg_id)), legs)

    engine._checkpoint_leg_marks(TS)

    assert written == [(CYCLE_ID, "leg-1"), (CYCLE_ID, "leg-2")]


@pytest.mark.parametrize(
    "state", [LegState.PENDING_CONTRACT, LegState.PENDING_ORDER, LegState.CLOSED]
)
def test_skips_a_leg_that_is_not_open(state: LegState) -> None:
    """A mark for a closed or not-yet-open leg would claim a position that
    does not exist."""
    written: list[str] = []
    engine = _engine(lambda _c, leg: written.append(leg.leg_id), [_leg("leg-1", state=state)])

    engine._checkpoint_leg_marks(TS)

    assert written == []


def test_skips_an_open_leg_with_no_price_seen_yet() -> None:
    """Its ``unrealised_pnl`` would be a structural zero, not a measurement —
    writing it would be indistinguishable from a real break-even mark."""
    written: list[str] = []
    engine = _engine(
        lambda _c, leg: written.append(leg.leg_id), [_leg("leg-1", last_price=None)]
    )

    engine._checkpoint_leg_marks(TS)

    assert written == []


def test_does_nothing_without_a_cycle() -> None:
    written: list[str] = []
    engine = _engine(lambda _c, leg: written.append(leg.leg_id), None)

    engine._checkpoint_leg_marks(TS)

    assert written == []


def test_does_nothing_when_no_callback_is_wired() -> None:
    """A composition that never wires the callback (a test harness, an
    offline replay) must not crash on evaluation."""
    engine = _engine(None, [_leg("leg-1")])

    engine._checkpoint_leg_marks(TS)  # must not raise


def test_a_failing_write_never_escapes_the_checkpoint() -> None:
    """The property that keeps this observability feature from breaking the
    exit ladder. The evaluation this sits in front of is what closes a losing
    cycle; failing to record a number for the dashboard must never stop it."""

    def boom(_cycle_id: str, _leg: LegInstance) -> None:
        raise RuntimeError("database is locked")

    engine = _engine(boom, [_leg("leg-1")])

    engine._checkpoint_leg_marks(TS)  # must not raise


def test_one_failing_leg_does_not_stop_the_others() -> None:
    written: list[str] = []

    def flaky(_cycle_id: str, leg: LegInstance) -> None:
        if leg.leg_id == "leg-1":
            raise RuntimeError("transient")
        written.append(leg.leg_id)

    engine = _engine(flaky, [_leg("leg-1"), _leg("leg-2"), _leg("leg-3")])

    engine._checkpoint_leg_marks(TS)

    assert written == ["leg-2", "leg-3"]


def test_the_mark_carries_the_pnl_the_exit_ladder_would_see() -> None:
    """The point of the whole feature: what gets persisted is the same
    ``unrealised_pnl`` the strategy evaluates, not a dashboard re-derivation."""
    captured: list[float] = []
    leg = _leg("leg-1", last_price=20.0)
    engine = _engine(lambda _c, lg: captured.append(lg.unrealised_pnl), [leg])

    engine._checkpoint_leg_marks(TS)

    assert captured == [pytest.approx((38.55 - 20.0) * 1300)]
