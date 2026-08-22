"""Phase 2 (strategy-rolling-strangle-otm1): the generic multi-leg engine's
durable repeated-roll lifecycle — atomic claims, per-target reserve/submit,
outcome resolution, replacement gating, and restart reconciliation — proven
against a real :class:`MultiLegEngine`, a real ``ExecutionRepository``/
``RollLedger``, and a real :class:`LifecycleGateway`/``OrderLifecycle``
driving a scripted (but otherwise real) broker.

Generic on purpose: nothing here names ``rolling_strangle_otm1`` — this
proves the *shared* machinery ``straddle_920``'s own ``EXIT_LEG`` +
``ExitReason.ADJUSTMENT`` is normalised into as well (see
``test_straddle_920_*`` for that strategy's own, unchanged, regression
suite). ``_apply_signal``/internal engine state is driven and inspected
directly, the same established style ``test_straddle_920_durability.py``
already uses for engine-internal testing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from common.broker.base import BrokerError, Quote
from common.config.models import ExecutionMode
from common.engine.config import EngineConfig, SessionConfig
from common.engine.feed import SimulatedFeed
from common.engine.gateway import LifecycleGateway
from common.engine.models import OptionContract, OptionType
from common.engine.multi_leg_engine import MultiLegEngine
from common.engine.multi_leg_models import (
    AdjustmentRequest,
    AdjustmentTarget,
    AnchorUpdate,
    Basket,
    BasketAction,
    BasketSignal,
    LegInstance,
    LegIntent,
    LegRole,
    LegState,
)
from common.engine.multi_leg_state import RollLedger
from common.engine.multi_leg_state import load_basket as _load_basket
from common.engine.multi_leg_state import persist_basket as _persist_basket_row
from common.engine.multi_leg_state import persist_leg as _persist_leg_row
from common.engine.multi_leg_strategy import BaseMultiLegStrategy
from common.engine.positions import PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.execution import ExecutionRepository, OrderLifecycle
from common.models import ExitReason, Fill, Order, OrderIntent, OrderSide, OrderStatus
from common.persistence import Database, migrate
from runtimes.intraday_options.multi_leg_engine_worker import (
    _reconcile_basket_rolls,
    recover_basket,
)

IST = ZoneInfo("Asia/Kolkata")
RUNTIME_ID = "intraday_options"
STRATEGY_ID = "rolling_multi_leg_fixture"
TRADING_DATE = "2026-08-17"
BASKET_ID = f"{STRATEGY_ID}:{TRADING_DATE}"
CE1 = "SIM:NIFTY:WEEKLY:24000:CE"
PE1 = "SIM:NIFTY:WEEKLY:24000:PE"
CE1_LEG_ID = f"{BASKET_ID}:CE:1"
PE1_LEG_ID = f"{BASKET_ID}:PE:1"
CE2_LEG_ID = f"{BASKET_ID}:CE:2"


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 17, hour, minute, second, tzinfo=IST)


class _NoOpStrategy(BaseMultiLegStrategy):
    """A strategy the tests never actually drive signals through — every
    scenario calls ``engine._apply_signal``/internal methods directly, the
    same style ``test_straddle_920_durability.py`` already uses. Only
    ``name``/``reset``/``on_candle`` are required to construct the engine."""

    name = STRATEGY_ID

    def on_candle(self, candle, timestamp, *, basket, vix):  # type: ignore[no-untyped-def]
        return None

    def reset(self) -> None:
        return None

    @property
    def quantity_lots(self) -> int:
        return 10


@dataclass
class _ScriptedBroker:
    """A minimal, real ``Broker`` implementation (only ``submit`` is ever
    called by ``OrderLifecycle``) driving precise Order/Fill outcomes per
    ``security_id`` for these tests. ``"fill"`` (default): fills at the
    quote's last price. ``"reject"``: raises ``BrokerError`` — caught by
    ``OrderLifecycle.submit``, which records a definitive ``REJECTED``
    order (``TERMINAL_NO_FILL``). ``"raise"``: raises a plain
    ``RuntimeError`` — propagates out of ``OrderLifecycle.submit``
    uncaught, so nothing is ever recorded after the reservation
    (``order_status IS NULL`` — genuinely ``UNKNOWN``, the transport-
    unknown case)."""

    outcomes: dict[str, str] = field(default_factory=dict)
    fill_prices: dict[str, float] = field(default_factory=dict)
    submit_calls: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "scripted-fake"

    def submit(self, intent: OrderIntent, quote: Quote) -> Order:
        self.submit_calls.append(intent.security_id)
        outcome = self.outcomes.get(intent.security_id, "fill")
        if outcome == "raise":
            raise RuntimeError(f"simulated transport failure for {intent.security_id}")
        if outcome == "reject":
            raise BrokerError(f"simulated rejection for {intent.security_id}")
        price = self.fill_prices.get(intent.security_id, quote.last_price)
        fill = Fill(
            correlation_id=intent.correlation_id,
            broker_fill_id=f"fake-{intent.correlation_id}",
            strategy_id=intent.strategy_id,
            execution_mode=intent.execution_mode,
            quantity=intent.quantity,
            price=price,
            filled_at=datetime.now(UTC),
        )
        return Order(
            correlation_id=intent.correlation_id,
            strategy_id=intent.strategy_id,
            execution_mode=intent.execution_mode,
            status=OrderStatus.FILLED,
            updated_at=datetime.now(UTC),
            filled_quantity=intent.quantity,
            average_fill_price=price,
            fills=(fill,),
        )

    def order_by_correlation_id(self, correlation_id: str) -> Order | None:
        return None

    def modify(self, correlation_id: str, *, quantity=None, limit_price=None) -> Order:  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def cancel(self, correlation_id: str) -> Order:
        raise NotImplementedError

    def fetch_order_book(self) -> tuple[Order, ...]:
        return ()

    def fetch_trades(self) -> tuple[Fill, ...]:
        return ()

    def fetch_positions(self) -> tuple[object, ...]:
        return ()

    def is_healthy(self) -> bool:
        return True


def _repository(tmp_path) -> ExecutionRepository:  # type: ignore[no-untyped-def]
    db = Database(tmp_path / "test.db")
    migrate(db)
    return ExecutionRepository(db)


def _build_engine(
    repository: ExecutionRepository, *, broker: _ScriptedBroker | None = None
) -> tuple[MultiLegEngine, PositionManager, _ScriptedBroker]:
    broker = broker or _ScriptedBroker()
    session = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        process_role="worker",
        pid=1,
    )
    lifecycle = OrderLifecycle(
        repository=repository,
        broker=broker,
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        session_id=session.id,
    )
    gateway = LifecycleGateway(
        lifecycle,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        repository=repository,
        runtime_id=RUNTIME_ID,
    )
    positions = PositionManager(gateway, lots=10)
    roll_ledger = RollLedger(
        repository,
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )

    def _persist_basket_cb(basket: Basket) -> None:
        _persist_basket_row(repository, basket, runtime_id=RUNTIME_ID)

    def _persist_leg_cb(leg: LegInstance) -> None:
        _persist_leg_row(
            repository,
            leg,
            runtime_id=RUNTIME_ID,
            strategy_id=STRATEGY_ID,
            execution_mode=ExecutionMode.PAPER,
            trading_date=TRADING_DATE,
        )

    def _recover() -> Basket | None:
        return recover_basket(_config(), repository)

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
        strategy=_NoOpStrategy(cfg=None),
        position_manager=positions,
        underlying_security_id="NIFTY_IDX",
        trading_date=TRADING_DATE,
        persist_basket=_persist_basket_cb,
        persist_leg=_persist_leg_cb,
        recover_basket=_recover,
        roll_ledger=roll_ledger,
    )
    return engine, positions, broker


@dataclass
class _FakeConfig:
    runtime_id: str
    strategy_id: str
    execution_mode: ExecutionMode
    trading_date: str


def _config() -> _FakeConfig:
    return _FakeConfig(
        runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )


def _seed_primary_basket(
    engine: MultiLegEngine, positions: PositionManager, broker: _ScriptedBroker
) -> None:
    """Seed a two-leg OPEN basket (CE1/PE1) directly, matching production
    field-for-field, without going through strategy-driven entry — this
    file tests the roll-claim machinery, not entry. Clears ``broker.
    submit_calls`` at the end: the two entry SELLs this seeding submits
    would otherwise contaminate every test's own roll-close assertions."""
    ce_contract = OptionContract(
        symbol="NIFTY24000CE", security_id=CE1, strike=24000.0,
        option_type=OptionType.CE, expiry="WEEKLY", lot_size=75,
    )
    pe_contract = OptionContract(
        symbol="NIFTY24000PE", security_id=PE1, strike=24000.0,
        option_type=OptionType.PE, expiry="WEEKLY", lot_size=75,
    )
    ts = _ts(9, 21)
    for contract, leg_id, role in (
        (ce_contract, CE1_LEG_ID, LegRole.CE),
        (pe_contract, PE1_LEG_ID, LegRole.PE),
    ):
        positions.open(contract, OrderSide.SELL, 100.0, ts, basket_id=BASKET_ID, leg_id=leg_id)
        position = positions.get(contract.security_id)
        assert position is not None
        leg = LegInstance(
            leg_id=leg_id, basket_id=BASKET_ID, role=role, sequence=1, is_replacement=False,
            side=OrderSide.SELL, contract=contract, state=LegState.OPEN,
            quantity=position.quantity, entry_price=position.entry_price, entry_time=ts,
            last_price=position.entry_price, entry_correlation_id=position.entry_correlation_id,
        )
        engine._basket.legs[leg_id] = leg
    engine._basket.entries_consumed = True
    engine._basket.original_combined_basis = 200.0
    engine._spot = 24000.0
    broker.submit_calls.clear()


def _adjust(
    engine: MultiLegEngine, *targets: AdjustmentTarget, anchor: AnchorUpdate | None = None
) -> None:
    signal = BasketSignal(
        action=BasketAction.ADJUST_LEGS,
        timestamp=_ts(9, 30),
        adjustment=AdjustmentRequest(targets=tuple(targets), anchor=anchor),
    )
    engine._apply_signal(signal, _ts(9, 30))


# ============================================================== 1. atomic claim
def test_claim_rows_are_durable_before_any_close(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    engine, positions, broker = _build_engine(repository)
    _seed_primary_basket(engine, positions, broker)

    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))

    rolls = repository.load_basket_rolls(basket_id=BASKET_ID)
    assert len(rolls) == 1
    assert rolls[0]["leg_role"] == "CE"
    assert rolls[0]["roll_sequence"] == 1
    # The claim landed durably, and the close was attempted (this scripted
    # broker always fills) — proving the claim-before-close ordering by the
    # row existing with a real close_intent_id, not merely CLAIMED forever.
    # A single-target group with its one member confirmed advances itself
    # to AWAITING_NEXT_CANDLE in the same call (matching the legacy
    # single-adjustment engine's own immediate-advance behaviour).
    assert rolls[0]["close_intent_id"] is not None
    assert rolls[0]["lifecycle_state"] == "AWAITING_NEXT_CANDLE"
    assert broker.submit_calls == [CE1]


def test_both_targets_of_a_two_leg_claim_commit_atomically(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    engine, positions, broker = _build_engine(repository)
    _seed_primary_basket(engine, positions, broker)

    _adjust(
        engine,
        AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE),
        AdjustmentTarget(leg_id=PE1_LEG_ID, role=LegRole.PE),
    )

    rolls = {r["leg_role"]: r for r in repository.load_basket_rolls(basket_id=BASKET_ID)}
    assert set(rolls) == {"CE", "PE"}
    assert rolls["CE"]["claim_group_id"] == rolls["PE"]["claim_group_id"]
    # Both targets confirmed in the same call, so the group already
    # advanced together (_close_adjusted_legs_with_ledger's own
    # _maybe_advance_claim_group call at the end) — group confirmation
    # never depends on which order the targets were submitted in.
    assert rolls["CE"]["lifecycle_state"] == "AWAITING_NEXT_CANDLE"
    assert rolls["PE"]["lifecycle_state"] == "AWAITING_NEXT_CANDLE"


def test_injected_mid_transaction_failure_leaves_no_partial_claim_and_submits_no_close(  # type: ignore[no-untyped-def]
    tmp_path, monkeypatch
) -> None:
    repository = _repository(tmp_path)
    engine, positions, broker = _build_engine(repository)
    _seed_primary_basket(engine, positions, broker)

    def _flaky_commit(basket, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("injected roll claim commit failure")

    monkeypatch.setattr(engine._roll_ledger, "commit_claims", _flaky_commit)

    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))

    assert repository.load_basket_rolls(basket_id=BASKET_ID) == []
    assert broker.submit_calls == [], "no close may be attempted when the claim never committed"
    assert engine.entries_blocked is not None
    # Leg untouched — still OPEN, exactly as before the attempt.
    assert engine._basket.legs[CE1_LEG_ID].state is LegState.OPEN


def test_durability_failure_rehydrates_in_memory_state(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """After a failed commit, in-memory state must be re-fetched from
    durable storage — not merely rolled back locally — so memory can never
    diverge from the ledger."""
    repository = _repository(tmp_path)
    engine, positions, broker = _build_engine(repository)
    _seed_primary_basket(engine, positions, broker)
    # A durable basket row must exist for recover_basket to find on rehydrate.
    engine._persist_basket()
    for leg in engine._basket.legs.values():
        engine._persist_leg(leg)

    def _flaky_commit(basket, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("injected roll claim commit failure")

    monkeypatch.setattr(engine._roll_ledger, "commit_claims", _flaky_commit)
    basket_before_id = id(engine._basket)

    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))

    assert id(engine._basket) != basket_before_id, "the basket object must be replaced, not patched"
    assert engine._basket.legs[CE1_LEG_ID].state is LegState.OPEN


# ==================================================== 2. reservation / crash
def test_crash_while_claimed_resumes_without_another_roll_claim(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A target still CLAIMED (claim committed, close never reserved) must
    be resumed by a later request for the same leg — never claiming a
    second roll for it."""
    repository = _repository(tmp_path)
    engine, positions, broker = _build_engine(repository)
    _seed_primary_basket(engine, positions, broker)

    # Simulate "crashed right after commit_claims, before reserve_close" by
    # calling commit_claims directly and stopping there.
    assigned = engine._roll_ledger.commit_claims(
        engine._basket,
        claim_group_id=f"{BASKET_ID}:manual-1",
        targets=(AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE),),
        anchor=None,
        claim_candle_ts=_ts(9, 30),
        claimed_at=_ts(9, 30),
    )
    engine._append_local_claims(
        f"{BASKET_ID}:manual-1",
        AdjustmentRequest(targets=(AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE),)),
        _ts(9, 30),
        _ts(9, 30),
        assigned,
    )
    assert repository.load_basket_rolls(basket_id=BASKET_ID)[0]["lifecycle_state"] == "CLAIMED"

    # A later request for the same leg must resume, not re-claim.
    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))

    rolls = repository.load_basket_rolls(basket_id=BASKET_ID)
    assert len(rolls) == 1, "resuming must not create a second claim row"
    assert rolls[0]["roll_sequence"] == 1
    assert rolls[0]["lifecycle_state"] == "AWAITING_NEXT_CANDLE"
    assert broker.submit_calls == [CE1]


def test_exit_intent_is_reserved_before_broker_submission(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A definitive rejection proves reservation happened first: the
    reserved order_intents row exists (with its own correlation id) even
    though the broker call itself failed."""
    repository = _repository(tmp_path)
    broker = _ScriptedBroker()
    engine, positions, _ = _build_engine(repository, broker=broker)
    _seed_primary_basket(engine, positions, broker)
    broker.outcomes[CE1] = "reject"

    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))

    rolls = repository.load_basket_rolls(basket_id=BASKET_ID)
    assert rolls[0]["close_correlation_id"] is not None
    assert rolls[0]["close_intent_id"] is not None
    intent_row = repository.order_intent_by_id(rolls[0]["close_intent_id"])
    assert intent_row is not None
    assert intent_row["order_status"] == "REJECTED"


# ======================================================= 3. outcome handling
def test_pending_ambiguous_close_is_never_treated_as_safe_to_retry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    broker = _ScriptedBroker()
    engine, positions, _ = _build_engine(repository, broker=broker)
    _seed_primary_basket(engine, positions, broker)
    broker.outcomes[CE1] = "raise"

    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))

    rolls = repository.load_basket_rolls(basket_id=BASKET_ID)
    assert rolls[0]["lifecycle_state"] == "EXIT_UNKNOWN"
    leg = engine._basket.legs[CE1_LEG_ID]
    assert leg.state is LegState.CLOSE_SUBMISSION_UNKNOWN
    assert engine.entries_blocked is not None
    # Never retried: a second identical request must not attempt another
    # close for this leg (it is no longer OPEN, so _close_adjusted_legs_
    # with_ledger's own target validation refuses it).
    broker.submit_calls.clear()
    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))
    assert broker.submit_calls == []


def test_definitive_rejection_leaves_leg_open_consumes_roll_forbids_replacement(  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    broker = _ScriptedBroker()
    engine, positions, _ = _build_engine(repository, broker=broker)
    _seed_primary_basket(engine, positions, broker)
    broker.outcomes[CE1] = "reject"

    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))

    rolls = repository.load_basket_rolls(basket_id=BASKET_ID)
    assert rolls[0]["lifecycle_state"] == "FAILED"
    leg = engine._basket.legs[CE1_LEG_ID]
    assert leg.state is LegState.OPEN, "a definitively rejected close must leave the leg OPEN"
    assert engine._basket.roll_state.roll_count(LegRole.CE) == 1, "the roll budget is consumed"
    assert engine._basket.roll_state.active_claim(LegRole.CE) is None, "no eligible replacement"


def test_transport_unknown_blocks_replacement_and_entries(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    broker = _ScriptedBroker()
    engine, positions, _ = _build_engine(repository, broker=broker)
    _seed_primary_basket(engine, positions, broker)
    broker.outcomes[CE1] = "raise"

    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))

    assert engine.entries_blocked is not None
    # EXIT_UNKNOWN is itself terminal for this claim (nothing further will
    # ever touch this specific attempt) — so active_claim correctly returns
    # None; the assertion belongs on the claim's own recorded outcome.
    assert engine._basket.roll_state.active_claim(LegRole.CE) is None
    claims = engine._basket.roll_state.claims_for_group(  # type: ignore[union-attr]
        engine._basket.roll_state.claims[0].claim_group_id  # type: ignore[union-attr]
    )
    assert claims[0].lifecycle_state == "EXIT_UNKNOWN"


def test_confirmed_fill_self_heals_from_authoritative_records(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A confirmed fill recovered purely from order_intents/orders/fills —
    the projection write for it was lost — self-heals via reconciliation,
    without a duplicate close attempt."""
    repository = _repository(tmp_path)
    engine, positions, broker = _build_engine(repository)
    _seed_primary_basket(engine, positions, broker)
    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))
    assert (
        repository.load_basket_rolls(basket_id=BASKET_ID)[0]["lifecycle_state"]
        == "AWAITING_NEXT_CANDLE"
    )

    # Simulate the best-effort post-close roll-outcome write having been
    # lost: revert the ledger row back to EXIT_SUBMISSION_PENDING by hand.
    repository.update_basket_roll_outcome(
        basket_id=BASKET_ID, leg_role="CE", roll_sequence=1,
        lifecycle_state="EXIT_SUBMISSION_PENDING",
    )

    basket = _load_basket(
        repository, strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )
    assert basket is not None
    mismatches = _reconcile_basket_rolls(repository, basket)
    assert mismatches == []
    # Resolved back to EXIT_CONFIRMED and, since this is a single-member
    # group, immediately advanced to AWAITING_NEXT_CANDLE again — the same
    # outcome as if the original write had never been lost.
    claim = basket.roll_state.active_claim(LegRole.CE)  # type: ignore[union-attr]
    assert claim is not None
    assert claim.lifecycle_state == "AWAITING_NEXT_CANDLE"
    rolls_after = repository.load_basket_rolls(basket_id=BASKET_ID)
    assert rolls_after[0]["lifecycle_state"] == "AWAITING_NEXT_CANDLE"
    assert broker.submit_calls.count(CE1) == 1, "self-heal must never resubmit a close"


# ================================================ 4. both-leg group / replacement
def test_first_target_confirmed_second_still_claimed_resumes_only_the_second(  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    engine, positions, broker = _build_engine(repository)
    _seed_primary_basket(engine, positions, broker)

    group_id = f"{BASKET_ID}:manual-both"
    assigned = engine._roll_ledger.commit_claims(
        engine._basket, claim_group_id=group_id,
        targets=(
            AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE),
            AdjustmentTarget(leg_id=PE1_LEG_ID, role=LegRole.PE),
        ),
        anchor=None, claim_candle_ts=_ts(9, 30), claimed_at=_ts(9, 30),
    )
    request = AdjustmentRequest(
        targets=(
            AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE),
            AdjustmentTarget(leg_id=PE1_LEG_ID, role=LegRole.PE),
        )
    )
    engine._append_local_claims(group_id, request, _ts(9, 30), _ts(9, 30), assigned)
    # First target (CE) confirms now; second (PE) stays CLAIMED — simulating
    # "confirmed then crashed before the second was even attempted".
    engine._close_one_roll_target(
        engine._basket.legs[CE1_LEG_ID], request.targets[0], group_id,
        assigned[CE1_LEG_ID], _ts(9, 30),
    )
    rolls = {r["leg_role"]: r for r in repository.load_basket_rolls(basket_id=BASKET_ID)}
    assert rolls["CE"]["lifecycle_state"] == "EXIT_CONFIRMED"
    assert rolls["PE"]["lifecycle_state"] == "CLAIMED"
    broker.submit_calls.clear()

    # A later request for both roles must resume only PE — CE is CLOSED
    # (not OPEN), so it is not even a valid target any more; PE resumes its
    # existing CLAIMED claim.
    _adjust(engine, AdjustmentTarget(leg_id=PE1_LEG_ID, role=LegRole.PE))

    rolls2 = {r["leg_role"]: r for r in repository.load_basket_rolls(basket_id=BASKET_ID)}
    # Both members are now confirmed, so the group (never repeating CE's
    # already-confirmed close) advances together.
    assert rolls2["CE"]["lifecycle_state"] == "AWAITING_NEXT_CANDLE", "never repeated"
    assert rolls2["PE"]["lifecycle_state"] == "AWAITING_NEXT_CANDLE"
    assert broker.submit_calls == [PE1]
    assert rolls2["CE"]["roll_sequence"] == rolls["CE"]["roll_sequence"]


def test_two_target_replacement_waits_for_both_confirmed_closes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    engine, positions, broker = _build_engine(repository)
    _seed_primary_basket(engine, positions, broker)

    group_id = f"{BASKET_ID}:manual-both2"
    request = AdjustmentRequest(
        targets=(
            AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE),
            AdjustmentTarget(leg_id=PE1_LEG_ID, role=LegRole.PE),
        )
    )
    assigned = engine._roll_ledger.commit_claims(
        engine._basket, claim_group_id=group_id, targets=request.targets,
        anchor=None, claim_candle_ts=_ts(9, 30), claimed_at=_ts(9, 30),
    )
    engine._append_local_claims(group_id, request, _ts(9, 30), _ts(9, 30), assigned)
    # Only CE confirms.
    engine._close_one_roll_target(
        engine._basket.legs[CE1_LEG_ID], request.targets[0], group_id,
        assigned[CE1_LEG_ID], _ts(9, 30),
    )
    engine._maybe_advance_claim_group(group_id)

    rolls = {r["leg_role"]: r for r in repository.load_basket_rolls(basket_id=BASKET_ID)}
    assert rolls["CE"]["lifecycle_state"] == "EXIT_CONFIRMED"
    assert rolls["CE"]["lifecycle_state"] != "AWAITING_NEXT_CANDLE", (
        "the group must not advance until every member confirms"
    )


def test_replacement_cannot_coexist_with_the_adjusted_out_leg(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The engine's own ENTER_LEG gate refuses a replacement for a role
    whose claim is not genuinely AWAITING_NEXT_CANDLE — including while the
    adjusted-out leg is still OPEN (claim stuck at CLAIMED/EXIT_SUBMISSION_
    PENDING/FAILED/EXIT_UNKNOWN)."""
    repository = _repository(tmp_path)
    broker = _ScriptedBroker()
    engine, positions, _ = _build_engine(repository, broker=broker)
    _seed_primary_basket(engine, positions, broker)
    broker.outcomes[CE1] = "reject"
    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))
    assert engine._basket.legs[CE1_LEG_ID].state is LegState.OPEN  # rejected -> still open

    signal = BasketSignal(
        action=BasketAction.ENTER_LEG,
        timestamp=_ts(9, 35),
        legs=(LegIntent(role=LegRole.CE, side=OrderSide.SELL),),
    )
    before = len(engine._basket.legs)
    engine._apply_signal(signal, _ts(9, 35))
    assert len(engine._basket.legs) == before, "no replacement leg may be created"


# =========================================== 5. multiple exit attempts per leg
def test_rejected_adjustment_close_then_square_off_creates_a_separate_exit_attempt(  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    broker = _ScriptedBroker()
    engine, positions, _ = _build_engine(repository, broker=broker)
    _seed_primary_basket(engine, positions, broker)
    broker.outcomes[CE1] = "reject"
    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))
    assert engine._basket.legs[CE1_LEG_ID].state is LegState.OPEN

    # Now let the same leg close cleanly via the ordinary (non-roll) path —
    # a separate exit attempt with its own correlation id.
    broker.outcomes[CE1] = "fill"
    engine._close_leg_safely(
        engine._basket.legs[CE1_LEG_ID], 90.0, _ts(15, 15), ExitReason.SQUARE_OFF
    )

    assert engine._basket.legs[CE1_LEG_ID].state is LegState.CLOSED
    history = repository.leg_order_history(leg_id=CE1_LEG_ID)
    exit_rows = [r for r in history if r["side"] != "SELL"]
    assert len(exit_rows) == 2, "the leg now legitimately carries two exit attempts"


def test_restart_after_that_square_off_reconciles_cleanly_with_multiple_exit_intents(  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    broker = _ScriptedBroker()
    engine, positions, _ = _build_engine(repository, broker=broker)
    _seed_primary_basket(engine, positions, broker)
    broker.outcomes[CE1] = "reject"
    for leg in engine._basket.legs.values():
        engine._persist_leg(leg)
    engine._persist_basket()
    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))
    broker.outcomes[CE1] = "fill"
    engine._close_leg_safely(
        engine._basket.legs[CE1_LEG_ID], 90.0, _ts(15, 15), ExitReason.SQUARE_OFF
    )
    engine._basket.legs[PE1_LEG_ID].state = LegState.OPEN  # still open, no contradiction
    engine._persist_leg(engine._basket.legs[PE1_LEG_ID])
    engine._persist_basket()

    basket = recover_basket(_config(), repository)

    assert basket is not None
    assert basket.legs[CE1_LEG_ID].state is LegState.CLOSED
    assert basket.legs[PE1_LEG_ID].state is LegState.OPEN


def test_one_rejected_exit_plus_one_filled_exit_resolves_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    broker = _ScriptedBroker()
    engine, positions, _ = _build_engine(repository, broker=broker)
    _seed_primary_basket(engine, positions, broker)
    broker.outcomes[CE1] = "reject"
    for leg in engine._basket.legs.values():
        engine._persist_leg(leg)
    engine._persist_basket()
    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))
    broker.outcomes[CE1] = "fill"
    engine._close_leg_safely(
        engine._basket.legs[CE1_LEG_ID], 90.0, _ts(15, 15), ExitReason.SQUARE_OFF
    )
    engine._persist_leg(engine._basket.legs[CE1_LEG_ID])
    engine._persist_basket()

    history = repository.leg_order_history(leg_id=CE1_LEG_ID)
    exit_rows = [r for r in history if r["side"] != "SELL"]
    assert len(exit_rows) == 2

    basket = recover_basket(_config(), repository)
    assert basket is not None
    assert basket.legs[CE1_LEG_ID].state is LegState.CLOSED


def test_two_confirmed_closing_fills_fail_closed_as_an_over_close_contradiction(  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    engine, positions, broker = _build_engine(repository)
    _seed_primary_basket(engine, positions, broker)
    for leg in engine._basket.legs.values():
        engine._persist_leg(leg)
    engine._persist_basket()

    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))
    engine._persist_leg(engine._basket.legs[CE1_LEG_ID])
    engine._persist_basket()

    # Force a second, independent CONFIRMED closing fill for the same leg —
    # structurally impossible in a correct system, but reconciliation must
    # refuse to guess which one is real rather than silently pick one.
    from datetime import UTC as _UTC

    from common.models import Fill as _Fill
    from common.models import Order as _Order
    from common.models import OrderIntent as _OrderIntent
    from common.models import OrderType as _OrderType

    intent = _OrderIntent(
        correlation_id="manual-second-close",
        strategy_id=STRATEGY_ID,
        runtime_id=RUNTIME_ID,
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        sequence_number=999,
        instrument=CE1,
        security_id=CE1,
        side=OrderSide.BUY,
        quantity=750,
        order_type=_OrderType.MARKET,
        product_type="INTRADAY",
        created_at=datetime.now(_UTC),
        basket_id=BASKET_ID,
        leg_id=CE1_LEG_ID,
    )
    intent_id = repository.reserve_intent(session_id=1, intent=intent)
    order = _Order(
        correlation_id="manual-second-close", strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER, status=OrderStatus.FILLED,
        updated_at=datetime.now(_UTC), filled_quantity=750, average_fill_price=95.0,
    )
    order_id = repository.record_submission(intent_id=intent_id, order=order, runtime_id=RUNTIME_ID)
    repository.apply_fill(
        order_id=order_id, runtime_id=RUNTIME_ID,
        fill=_Fill(
            correlation_id="manual-second-close", broker_fill_id="manual-fill-2",
            strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER, quantity=750,
            price=95.0, filled_at=datetime.now(_UTC),
        ),
        order_status=OrderStatus.FILLED, instrument=CE1, security_id=CE1,
        side=OrderSide.SELL, trading_date=TRADING_DATE,
    )

    from common.engine.multi_leg_models import UnmanageableBasketState

    with pytest.raises(UnmanageableBasketState):
        recover_basket(_config(), repository)


def test_roll_reconciliation_uses_exact_close_intent_id_not_leg_history(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A roll claim's own recovery must be scoped strictly to its own
    close_intent_id — a later, unrelated square-off attempt on the same
    leg must not affect the roll claim's already-resolved outcome."""
    repository = _repository(tmp_path)
    broker = _ScriptedBroker()
    engine, positions, _ = _build_engine(repository, broker=broker)
    _seed_primary_basket(engine, positions, broker)
    broker.outcomes[CE1] = "reject"
    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))
    roll_before = repository.load_basket_rolls(basket_id=BASKET_ID)[0]
    assert roll_before["lifecycle_state"] == "FAILED"

    broker.outcomes[CE1] = "fill"
    engine._close_leg_safely(
        engine._basket.legs[CE1_LEG_ID], 90.0, _ts(15, 15), ExitReason.SQUARE_OFF
    )

    basket = _load_basket(
        repository, strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
    )
    assert basket is not None
    mismatches = _reconcile_basket_rolls(repository, basket)
    assert mismatches == []
    claim = basket.roll_state.claims[0]  # type: ignore[union-attr]
    assert claim.lifecycle_state == "FAILED", "the roll claim's own outcome must not change"


# ============================================== 6. realised gross P&L reconstruction
def test_realised_gross_pnl_is_restored_from_authoritative_data(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    engine, positions, broker = _build_engine(repository)
    _seed_primary_basket(engine, positions, broker)
    _adjust(engine, AdjustmentTarget(leg_id=CE1_LEG_ID, role=LegRole.CE))
    leg = engine._basket.legs[CE1_LEG_ID]
    assert leg.state is LegState.CLOSED
    assert leg.realized_gross_pnl is not None
    true_pnl = leg.realized_gross_pnl

    # Simulate the best-effort projection write having been lost.
    leg.realized_gross_pnl = None
    engine._persist_leg(leg)
    for other in engine._basket.legs.values():
        if other.leg_id != leg.leg_id:
            engine._persist_leg(other)
    engine._persist_basket()

    basket = recover_basket(_config(), repository)

    assert basket is not None
    assert basket.legs[CE1_LEG_ID].realized_gross_pnl == pytest.approx(true_pnl)


# ================================================== 7. unknown lifecycle state
def test_unknown_lifecycle_state_fails_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    engine, positions, broker = _build_engine(repository)
    _seed_primary_basket(engine, positions, broker)
    for leg in engine._basket.legs.values():
        engine._persist_leg(leg)
    engine._persist_basket()

    repository.upsert_basket_roll(
        runtime_id=RUNTIME_ID, strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE, basket_id=BASKET_ID, claim_group_id="grp-x",
        leg_role="CE", roll_sequence=1, lifecycle_state="SOME_FUTURE_STATE_NOT_YET_KNOWN",
        target_leg_id=CE1_LEG_ID, close_correlation_id=None, close_intent_id=None,
        replacement_leg_id=None, reference_price_at_claim=24000.0,
        claim_candle_ts="2026-08-17T09:30:00+05:30", claimed_at="2026-08-17T09:30:01+05:30",
    )

    from common.engine.multi_leg_models import UnmanageableBasketState

    with pytest.raises(UnmanageableBasketState):
        recover_basket(_config(), repository)
