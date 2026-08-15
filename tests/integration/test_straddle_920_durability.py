"""P0-1 (fail-closed persistence) and P0-2 (adjustment-close state machine)
failure-injection tests, driven through a real :class:`MultiLegEngine`.

Mirrors ``tests/integration/test_straddle_920_engine.py``'s fixture style —
a real engine, ``InMemoryGateway``/``SimulatedFeed``, no monkeypatching of
the engine itself — but injects failing ``persist_basket``/``persist_leg``
callables and (for the P0-2 tests) a gateway that raises on a specific
leg's closing order, to prove the engine's own reaction to those failures
rather than merely asserting the persisted rows look right when nothing
ever fails.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from common.config.models import ExecutionMode
from common.engine.config import EngineConfig, SessionConfig
from common.engine.feed import SimulatedFeed
from common.engine.models import OptionContract
from common.engine.multi_leg_engine import MultiLegEngine
from common.engine.multi_leg_models import AdjustmentLifecycle, Basket, LegInstance, LegState
from common.engine.positions import ExecutionGateway, FillOutcome, InMemoryGateway, PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.models import ExitReason, Tick
from strategies.intraday_options.straddle_920.strategy import Straddle920Strategy

IST = ZoneInfo("Asia/Kolkata")
NIFTY = "NIFTY_IDX"
CE1 = "SIM:NIFTY:WEEKLY:24000:CE"
PE1 = "SIM:NIFTY:WEEKLY:24000:PE"


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 17, hour, minute, second, tzinfo=IST)  # a Monday


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id,
        instrument=security_id,
        last_price=price,
        exchange_time=ts,
        received_at=ts,
    )


class _RaisingOnLegGateway:
    """Wraps :class:`InMemoryGateway`; raises instead of filling a **closing**
    ``buy()`` (straddle_920 legs are always SELL-entered, so the close side
    is always ``buy()``) whenever the call carries one of
    ``raise_for_leg_ids`` — used to simulate a close order whose submission
    genuinely fails/raises (P0-2), as opposed to a persistence write failing
    (P0-1, which never touches the gateway). ``sell()`` (always the entry
    side here) is never intercepted, so entry is unaffected."""

    def __init__(self, inner: InMemoryGateway, *, raise_for_leg_ids: frozenset[str]) -> None:
        self._inner = inner
        self._raise_for = raise_for_leg_ids
        self.buy_calls: list[str | None] = []

    def buy(
        self,
        contract: OptionContract,
        lots: int,
        *,
        ref_price: float,
        ts: datetime,
        stop_price: float | None = None,
        target_price: float | None = None,
        basket_id: str | None = None,
        leg_id: str | None = None,
    ) -> FillOutcome:
        self.buy_calls.append(leg_id)
        if leg_id in self._raise_for:
            raise RuntimeError(f"simulated close failure for {leg_id}")
        return self._inner.buy(
            contract, lots, ref_price=ref_price, ts=ts, stop_price=stop_price,
            target_price=target_price, basket_id=basket_id, leg_id=leg_id,
        )

    def sell(
        self,
        contract: OptionContract,
        lots: int,
        *,
        ref_price: float,
        ts: datetime,
        stop_price: float | None = None,
        target_price: float | None = None,
        basket_id: str | None = None,
        leg_id: str | None = None,
    ) -> FillOutcome:
        return self._inner.sell(
            contract, lots, ref_price=ref_price, ts=ts, stop_price=stop_price,
            target_price=target_price, basket_id=basket_id, leg_id=leg_id,
        )


def _build_engine(
    *,
    gateway: ExecutionGateway | None = None,
    persist_basket: Callable[[Basket], None] | None = None,
    persist_leg: Callable[[LegInstance], None] | None = None,
    record_incident: Callable[[str, str], None] | None = None,
    daily_loss_amount: float = 10_000_000.0,
    trading_date: str = "2026-08-17",
) -> tuple[MultiLegEngine, PositionManager]:
    positions = PositionManager(gateway or InMemoryGateway(slippage_points=0.0), lots=10)
    strategy = Straddle920Strategy(
        lots_per_leg=10,
        entry_evaluation_time="09:20",
        last_entry_time="15:00",
        vix_threshold=20.0,
        leg_adjustment_multiplier=2.0,
        max_adjustments_per_day=1,
        daily_loss_amount=daily_loss_amount,
        combined_stop_percentage=0.30,
        profit_target_percentage=0.50,
    )
    engine = MultiLegEngine(
        EngineConfig(
            timeframe="5m",
            session=SessionConfig(
                timezone="Asia/Kolkata",
                start_time="09:15",
                end_time="15:00",
                square_off_time="15:15",
            ),
            execution_mode=ExecutionMode.PAPER,
        ),
        feed=SimulatedFeed([]),
        option_selector=OptionSelector(
            SimulatedOptionChainResolver("NIFTY", lot_size=75), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=NIFTY,
        trading_date=trading_date,
        persist_basket=persist_basket,
        persist_leg=persist_leg,
        record_incident=record_incident,
    )
    return engine, positions


def _run(engine: MultiLegEngine, ticks: list[Tick]) -> None:
    engine.feed = SimulatedFeed(ticks)
    engine.run()


_ENTRY_TICKS = [
    _tick(NIFTY, 24000.0, _ts(9, 16)),
    _tick(NIFTY, 24000.0, _ts(9, 21)),  # ENTER_BASKET
    _tick(CE1, 100.0, _ts(9, 21, 5)),
    _tick(PE1, 95.0, _ts(9, 21, 10)),
]


# =============================================================== P0-1 =====
def test_primary_attempt_persist_failure_blocks_entry() -> None:
    """A critical basket-persist failure right after the primary-entry
    signal must suppress that signal entirely — no order for it."""

    def _always_fail(basket: Basket) -> None:
        raise RuntimeError("simulated basket persist failure")

    engine, positions = _build_engine(persist_basket=_always_fail)
    _run(engine, _ENTRY_TICKS)

    assert not positions.has_position()
    assert not positions.trades
    assert engine.entries_blocked is not None


def test_pending_leg_persist_failure_blocks_subscription_and_order() -> None:
    """The basket-level persist succeeds (so the primary attempt is
    genuinely consumed), but the pending leg's own critical persist fails —
    no subscription, no order, for either leg."""

    def _always_fail(leg: LegInstance) -> None:
        raise RuntimeError("simulated leg persist failure")

    engine, positions = _build_engine(persist_leg=_always_fail)
    _run(engine, _ENTRY_TICKS)

    assert not positions.has_position()
    assert not positions.trades
    assert engine.entries_blocked is not None
    assert all(leg.state is LegState.FAILED for leg in engine._basket.legs.values())


def test_adjustment_claim_persist_failure_blocks_the_adjustment_exit() -> None:
    """The doubled leg's adjustment claim cannot be made durable — the leg
    must stay open and untouched rather than be closed with an
    undocumented adjustment."""

    def _fail_at_claim(basket: Basket) -> None:
        if basket.pending_replacement_state == AdjustmentLifecycle.EXIT_SUBMISSION_PENDING.value:
            raise RuntimeError("simulated basket persist failure at adjustment claim")

    engine, positions = _build_engine(persist_basket=_fail_at_claim)
    ticks = [
        *_ENTRY_TICKS,
        _tick(CE1, 205.0, _ts(9, 30)),  # CE doubles -> would normally adjust
    ]
    _run(engine, ticks)

    open_ce = [p for p in positions.positions if p.contract.security_id == CE1]
    assert len(open_ce) == 1, "the doubled leg must remain open — no adjustment exit was made"
    assert not any(t.exit_reason is ExitReason.ADJUSTMENT for t in positions.trades)
    assert engine.entries_blocked is not None


def test_leg_close_projection_failure_does_not_undo_the_close() -> None:
    """The adjustment claim persists fine and the close genuinely happens —
    only the *post-effect* projection write for the closed leg fails. The
    trade must still be recorded (the fill already happened) and the
    failure must be surfaced through the incident path, never silently
    dropped and never used to reverse the trade."""
    incidents: list[tuple[str, str]] = []

    def _fail_on_close_projection(leg: LegInstance) -> None:
        if leg.state is LegState.CLOSED:
            raise RuntimeError("simulated leg persist failure at close")

    engine, positions = _build_engine(
        persist_leg=_fail_on_close_projection,
        record_incident=lambda basket_id, message: incidents.append((basket_id, message)),
    )
    ticks = [
        *_ENTRY_TICKS,
        _tick(CE1, 205.0, _ts(9, 30)),  # CE doubles -> adjustment
    ]
    _run(engine, ticks)

    adjustment_trades = [t for t in positions.trades if t.exit_reason is ExitReason.ADJUSTMENT]
    assert len(adjustment_trades) == 1, "the close itself must still have happened"
    assert incidents, "the projection-write failure must be reported through the incident path"


def test_square_off_still_reduces_exposure_despite_persist_failure() -> None:
    """Every basket/leg persist call fails from square-off onward — the
    actual closes must still happen; an emergency exit is never blocked by
    a failed observability write."""

    def _fail_at_square_off_basket(basket: Basket) -> None:
        if basket.square_off_state in ("IN_PROGRESS", "COMPLETED"):
            raise RuntimeError("simulated basket persist failure at square-off")

    def _fail_at_square_off_leg(leg: LegInstance) -> None:
        if leg.exit_reason is not None and leg.exit_reason.value == "SQUARE_OFF":
            raise RuntimeError("simulated leg persist failure at square-off")

    engine, positions = _build_engine(
        persist_basket=_fail_at_square_off_basket, persist_leg=_fail_at_square_off_leg
    )
    ticks = [*_ENTRY_TICKS, _tick(NIFTY, 24000.0, _ts(15, 16))]  # hard square-off
    _run(engine, ticks)

    assert not positions.has_position(), "square-off must still flatten every leg"
    square_off_trades = [t for t in positions.trades if t.exit_reason is ExitReason.SQUARE_OFF]
    assert len(square_off_trades) == 2


# =============================================================== P0-2 =====
def test_close_submission_failure_leaves_the_leg_unresolved_and_blocks_entries() -> None:
    """The adjustment claim persists fine and the close is attempted, but
    the close order itself raises (a genuine submission failure, not a
    persistence failure) — the leg must land in CLOSE_SUBMISSION_UNKNOWN,
    never CLOSED and never left as plain OPEN, and entries must be
    blocked."""
    engine, positions = _build_engine()
    ce1_leg_id = f"{engine._basket_id}:CE:1"
    gateway = _RaisingOnLegGateway(
        InMemoryGateway(slippage_points=0.0), raise_for_leg_ids=frozenset({ce1_leg_id})
    )
    engine.positions = PositionManager(gateway, lots=10)
    positions = engine.positions

    ticks = [*_ENTRY_TICKS, _tick(CE1, 205.0, _ts(9, 30))]  # CE doubles
    _run(engine, ticks)

    leg = engine._basket.legs[ce1_leg_id]
    assert leg.state is LegState.CLOSE_SUBMISSION_UNKNOWN
    assert not any(t.exit_reason is ExitReason.ADJUSTMENT for t in positions.trades)
    assert engine.entries_blocked is not None
    # The position itself is untouched by the failed close attempt — still
    # open in the book, exactly as it was before the close was attempted.
    assert any(p.contract.security_id == CE1 for p in positions.positions)


def test_a_failed_close_is_never_retried_and_the_other_leg_still_closes() -> None:
    """After a close's outcome is unknown, a later sweep (square-off) must
    not attempt to close that same leg again — and must still close every
    *other* leg it safely can (correction requirement 8, plus "square-off
    still reduces exposure despite one leg's unresolved close")."""
    engine, _ = _build_engine()
    ce1_leg_id = f"{engine._basket_id}:CE:1"
    gateway = _RaisingOnLegGateway(
        InMemoryGateway(slippage_points=0.0), raise_for_leg_ids=frozenset({ce1_leg_id})
    )
    engine.positions = PositionManager(gateway, lots=10)
    positions = engine.positions

    ticks = [
        *_ENTRY_TICKS,
        _tick(CE1, 205.0, _ts(9, 30)),  # CE doubles -> close attempted, fails
        _tick(NIFTY, 24000.0, _ts(15, 16)),  # hard square-off sweeps again
    ]
    _run(engine, ticks)

    # Exactly one close attempt was ever made for the CE leg (the buy-to-close
    # call), never a second one from the square-off sweep.
    assert gateway.buy_calls.count(ce1_leg_id) == 1
    leg = engine._basket.legs[ce1_leg_id]
    assert leg.state is LegState.CLOSE_SUBMISSION_UNKNOWN

    # PE, unaffected by the failure, still closes normally at square-off.
    pe_trades = [t for t in positions.trades if t.contract.option_type.value == "PE"]
    assert len(pe_trades) == 1
    assert pe_trades[0].exit_reason is ExitReason.SQUARE_OFF


def test_replacement_signal_is_never_produced_while_the_close_outcome_is_unknown() -> None:
    """Correction requirement 6: an open-or-unknown adjusted leg must never
    coexist with a newly entered replacement — proven here by an
    unresolved close never reaching AWAITING_NEXT_CANDLE, so the strategy's
    own gate (and the engine's independent check in ``_enter_legs``) never
    produces/accepts a replacement leg for it."""
    engine, _ = _build_engine()
    ce1_leg_id = f"{engine._basket_id}:CE:1"
    gateway = _RaisingOnLegGateway(
        InMemoryGateway(slippage_points=0.0), raise_for_leg_ids=frozenset({ce1_leg_id})
    )
    engine.positions = PositionManager(gateway, lots=10)

    ticks = [
        *_ENTRY_TICKS,
        _tick(CE1, 205.0, _ts(9, 30)),  # doubles; close fails -> EXIT_UNKNOWN
        _tick(NIFTY, 24050.0, _ts(9, 31)),
        _tick(NIFTY, 24050.0, _ts(9, 36)),  # would-be replacement candle
    ]
    _run(engine, ticks)

    basket = engine._basket
    assert basket.pending_replacement_state == AdjustmentLifecycle.EXIT_UNKNOWN.value
    # No replacement CE leg (sequence 2) was ever created.
    assert not any(
        leg.role.value == "CE" and leg.sequence == 2 for leg in basket.legs.values()
    )
