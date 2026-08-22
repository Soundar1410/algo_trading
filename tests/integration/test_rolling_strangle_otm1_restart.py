"""Phase 5A: the full §17.6 restart/reconciliation acceptance matrix for
``rolling_strangle_otm1`` — real production wiring below the network
boundary, real temporary SQLite, never a hand-fabricated basket/leg/roll/
position/order-intent/trade-ledger/anchor/strategy-state row.

A "restart" is represented exactly as specified: the first ``MultiLegEngine``
(and its own ``PositionManager``/``LifecycleGateway``/``OrderLifecycle``/
``RollLedger``) is discarded, and a **second**, independently constructed
instance is built over the **same** ``ExecutionRepository``/SQLite file —
``_restart`` below. Recovery/reconciliation runs inside
``MultiLegEngine._start_day`` (via the injected ``recover_basket`` callable,
``runtimes.intraday_options.multi_leg_engine_worker.recover_basket``), the
same real function a production worker calls; a row that must fail closed
asserts ``UnmanageableBasketState`` propagating from exactly that call.

Ticks are fed through the real ``MultiLegEngine.on_tick`` directly, one at a
time, rather than through ``run()``/``SimulatedFeed`` — deliberately: this
file needs to express a crash at an exact tick boundary, and ``run()`` wraps
``feed.run()`` in a broad ``except Exception: ... force square-off ...
raise``, which is production's own "still alive enough to attempt cleanup"
behaviour, not a genuine process death. Calling ``on_tick`` in a bare loop
(after ``_start_day()``, which is what ``run()`` itself calls first) is the
same real dispatch path with no recovery wrapper in between — an exception
from it propagates exactly as a killed process would leave things: whatever
was durably committed before the failure stays committed, and nothing after
it happened. See ``tests/integration/test_rolling_multi_leg_engine.py``
(Phase 2) for the identical, already-approved technique of driving the real
engine's own methods directly rather than only through the full worker.

A very small number of rows (an atomic claim committed with no close
attempt yet dispatched at all — "the process died between the claim
transaction committing and the very next statement running") have no tick
sequence that can express them, because ``_close_adjusted_legs_with_ledger``
performs the claim-then-close-attempt as one uninterruptible Python call for
a single-target request. Those seed the claim via ``RollLedger.
commit_claims`` directly — the same real, generic repository-backed method
``_close_adjusted_legs_with_ledger`` itself calls, and the same technique
Phase 2's own suite uses for this exact state — never a hand-typed row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
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
    AdjustmentTarget,
    AnchorUpdate,
    Basket,
    LegInstance,
    LegRole,
    UnmanageableBasketState,
)
from common.engine.multi_leg_state import RollLedger
from common.engine.multi_leg_state import persist_basket as _persist_basket_row
from common.engine.multi_leg_state import persist_leg as _persist_leg_row
from common.engine.positions import PositionManager
from common.engine.selection import (
    OptionChainResolver,
    OptionSelector,
    SimulatedOptionChainResolver,
)
from common.execution import ExecutionRepository, OrderLifecycle
from common.models import OrderSide, Tick
from common.persistence import Database, migrate
from runtimes.intraday_options.multi_leg_engine_worker import recover_basket
from strategies.intraday_options.rolling_strangle_otm1.strategy import RollingStrangleOtm1Strategy

IST = ZoneInfo("Asia/Kolkata")
RUNTIME_ID = "intraday_options"
STRATEGY_ID = "rolling_strangle_otm1"
TRADING_DATE = "2026-08-17"  # a Monday
NIFTY = "NIFTY_IDX"
BASKET_ID = f"{STRATEGY_ID}:{TRADING_DATE}"
CE1 = "SIM:NIFTY:WEEKLY:24050:CE"
PE1 = "SIM:NIFTY:WEEKLY:23950:PE"


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 17, hour, minute, second, tzinfo=IST)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id, instrument=security_id, last_price=price,
        exchange_time=ts, received_at=ts,
    )


@dataclass
class _ScriptedBroker:
    """``outcomes[security_id]``: ``"fill"`` (default), ``"reject"``
    (``BrokerError`` -> a durable, definitive ``REJECTED`` order —
    ``TERMINAL_NO_FILL``), or ``"raise"`` (a plain ``RuntimeError``,
    uncaught by ``OrderLifecycle`` — no resolvable order row at all, the
    genuine ``UNKNOWN`` case). Mutable mid-run so one test can change a
    security's outcome between ticks."""

    outcomes: dict[str, str] = field(default_factory=dict)
    submit_calls: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "scripted-fake"

    def submit(self, intent, quote: Quote):  # type: ignore[no-untyped-def]
        self.submit_calls.append(intent.security_id)
        outcome = self.outcomes.get(intent.security_id, "fill")
        if outcome == "raise":
            raise RuntimeError(f"simulated transport failure for {intent.security_id}")
        if outcome == "reject":
            raise BrokerError(f"simulated rejection for {intent.security_id}")
        from common.models import Fill, Order, OrderStatus

        fill = Fill(
            correlation_id=intent.correlation_id,
            broker_fill_id=f"fake-{intent.correlation_id}",
            strategy_id=intent.strategy_id,
            execution_mode=intent.execution_mode,
            quantity=intent.quantity,
            price=quote.last_price,
            filled_at=datetime.now(UTC),
        )
        return Order(
            correlation_id=intent.correlation_id,
            strategy_id=intent.strategy_id,
            execution_mode=intent.execution_mode,
            status=OrderStatus.FILLED,
            updated_at=datetime.now(UTC),
            filled_quantity=intent.quantity,
            average_fill_price=quote.last_price,
            fills=(fill,),
        )

    def order_by_correlation_id(self, correlation_id: str):  # type: ignore[no-untyped-def]
        return None

    def modify(self, correlation_id: str, *, quantity=None, limit_price=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def cancel(self, correlation_id: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def fetch_order_book(self):  # type: ignore[no-untyped-def]
        return ()

    def fetch_trades(self):  # type: ignore[no-untyped-def]
        return ()

    def fetch_positions(self):  # type: ignore[no-untyped-def]
        return ()

    def is_healthy(self) -> bool:
        return True


class _MismatchedLotSizeResolver(OptionChainResolver):
    def resolve(self, strike: int, option_type: OptionType, expiry: str | None = None):  # type: ignore[no-untyped-def]
        lot_size = 75 if option_type is OptionType.CE else 65
        return OptionContract(
            symbol=f"NIFTY {strike} {option_type.value}",
            security_id=f"SIM:NIFTY:WEEKLY:{strike}:{option_type.value}",
            strike=float(strike), option_type=option_type,
            expiry=expiry or "WEEKLY", lot_size=lot_size,
        )


def _repository(db_path: Path) -> ExecutionRepository:
    db = Database(db_path)
    migrate(db)
    return ExecutionRepository(db)


@dataclass
class _FakeConfig:
    runtime_id: str
    strategy_id: str
    execution_mode: ExecutionMode
    trading_date: str


def _config() -> _FakeConfig:
    return _FakeConfig(RUNTIME_ID, STRATEGY_ID, ExecutionMode.PAPER, TRADING_DATE)


def _build_engine(
    repository: ExecutionRepository,
    *,
    broker: _ScriptedBroker | None = None,
    resolver: OptionChainResolver | None = None,
    max_rolls_ce: int = 2,
    max_rolls_pe: int = 2,
    single_leg_roll: bool = True,
    combined_stop_per_lot: float = 2000.0,
    entry_time: str = "09:45",
) -> tuple[MultiLegEngine, PositionManager, _ScriptedBroker]:
    broker = broker or _ScriptedBroker()
    session = repository.open_session(
        runtime_id=RUNTIME_ID, strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER, process_role="worker", pid=1,
    )
    lifecycle = OrderLifecycle(
        repository=repository, broker=broker, runtime_id=RUNTIME_ID,
        strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER, session_id=session.id,
    )
    gateway = LifecycleGateway(
        lifecycle, strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE, repository=repository, runtime_id=RUNTIME_ID,
    )
    positions = PositionManager(gateway, lots=10)
    roll_ledger = RollLedger(
        repository, runtime_id=RUNTIME_ID, strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE,
    )
    strategy = RollingStrangleOtm1Strategy(
        lots_per_leg=10, entry_time=entry_time, stop_new_entries_after="15:10",
        square_off_time="15:15", strike_step=50, otm_distance_points=50,
        roll_trigger_points=60, max_rolls_ce=max_rolls_ce, max_rolls_pe=max_rolls_pe,
        single_leg_roll=single_leg_roll, combined_stop_per_lot=combined_stop_per_lot,
    )

    def _persist_basket_cb(basket: Basket) -> None:
        _persist_basket_row(repository, basket, runtime_id=RUNTIME_ID)

    def _persist_leg_cb(leg: LegInstance) -> None:
        _persist_leg_row(
            repository, leg, runtime_id=RUNTIME_ID, strategy_id=STRATEGY_ID,
            execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE,
        )

    def _recover() -> Basket | None:
        return recover_basket(_config(), repository)  # type: ignore[arg-type]

    engine = MultiLegEngine(
        EngineConfig(
            timeframe="5m",
            session=SessionConfig(
                timezone="Asia/Kolkata", start_time="09:15", end_time="15:10",
                square_off_time="15:15",
            ),
            execution_mode=ExecutionMode.PAPER,
        ),
        feed=SimulatedFeed([]),
        option_selector=OptionSelector(
            resolver or SimulatedOptionChainResolver("NIFTY", lot_size=75), strike_step=50
        ),
        strategy=strategy,
        position_manager=positions,
        underlying_security_id=NIFTY,
        trading_date=TRADING_DATE,
        persist_basket=_persist_basket_cb,
        persist_leg=_persist_leg_cb,
        recover_basket=_recover,
        roll_ledger=roll_ledger,
    )
    return engine, positions, broker


def _start(engine: MultiLegEngine) -> None:
    """The real recovery/adoption entrypoint — see module docstring."""
    engine._start_day()


def _feed(engine: MultiLegEngine, ticks: list[Tick]) -> None:
    for t in ticks:
        engine.on_tick(t)


def _restart(
    repository: ExecutionRepository, *, broker: _ScriptedBroker | None = None, **kwargs
) -> tuple[MultiLegEngine, PositionManager, _ScriptedBroker]:
    """Dispose of nothing explicitly (Python GC does that) — the point is
    that this is an **independently constructed** engine/gateway/lifecycle/
    positions/roll-ledger instance, over the same repository, with recovery
    run via ``_start`` exactly as a fresh worker process would."""
    engine, positions, broker = _build_engine(repository, broker=broker, **kwargs)
    _start(engine)
    return engine, positions, broker


def _rolls(repository: ExecutionRepository) -> list:  # type: ignore[type-arg]
    return list(repository.load_basket_rolls(basket_id=BASKET_ID))


def _leg_rows(repository: ExecutionRepository) -> list:  # type: ignore[type-arg]
    return list(
        repository.load_strategy_legs(
            strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE
        )
    )


def _basket_row(repository: ExecutionRepository):  # type: ignore[no-untyped-def]
    return repository.load_strategy_basket(
        strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE
    )


# ======================================================================
# Smoke test — the harness itself
# ======================================================================
def test_harness_smoke_entry_then_restart_then_roll(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "test.db")
    engine, positions, _broker = _build_engine(repository)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
        ],
    )
    assert len(positions.positions) == 2

    engine2, positions2, _broker2 = _restart(repository)
    assert len(positions2.positions) == 2  # adopted, not re-entered
    _feed(
        engine2,
        [
            _tick(NIFTY, 24100.0, _ts(9, 49, 0)),
            _tick(NIFTY, 24100.0, _ts(9, 50, 0)),  # CE roll triggers
        ],
    )
    rolls = _rolls(repository)
    assert len(rolls) == 1
    assert rolls[0]["leg_role"] == "CE"



# ======================================================================
# A. Primary entry (spec section 8; matrix section "Primary entry")
# ======================================================================
def test_restart_before_any_entry_decision_starts_fresh(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "test.db")
    engine, _positions, _broker = _build_engine(repository)
    _start(engine)
    _feed(engine, [_tick(NIFTY, 24000.0, _ts(9, 30))])  # before 09:45, no decision yet
    assert _basket_row(repository) is None

    engine2, positions2, _broker2 = _restart(repository)
    assert _basket_row(repository) is None
    _feed(
        engine2,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
        ],
    )
    assert len(positions2.positions) == 2


def test_restart_with_entry_consumed_but_no_leg_filled_resumes_the_pending_legs(
    tmp_path: Path,
) -> None:
    """Entry state (entries_consumed + anchor) commits atomically with
    contract resolution/subscription in one candle-close call (spec section
    8 step 1) — there is no tick sequence expressing "consumed but nothing
    subscribed"; the earliest observable checkpoint is exactly this one:
    both legs durably PENDING_ORDER, subscribed, no fill yet."""
    repository = _repository(tmp_path / "test.db")
    engine, positions, _broker = _build_engine(repository)
    _start(engine)
    _feed(engine, [_tick(NIFTY, 24000.0, _ts(9, 41)), _tick(NIFTY, 24000.0, _ts(9, 45, 0))])
    basket = _basket_row(repository)
    assert basket is not None
    assert bool(basket["entries_consumed"]) is True
    legs = _leg_rows(repository)
    assert len(legs) == 2
    assert all(leg["state"] == "PENDING_ORDER" for leg in legs)
    assert not positions.positions

    engine2, positions2, _broker2 = _restart(repository)
    # No duplicate ENTER_BASKET: entries_consumed already true, and the
    # legs adopted as still-pending, not re-created.
    _feed(engine2, [_tick(CE1, 100.0, _ts(9, 45, 5)), _tick(PE1, 95.0, _ts(9, 45, 10))])
    assert len(positions2.positions) == 2
    assert len(_leg_rows(repository)) == 2  # never duplicated
    intents = repository.leg_order_history(leg_id=f"{BASKET_ID}:CE:1")
    assert len(intents) == 1  # exactly one SELL intent for CE, not two


def test_restart_with_one_leg_filled_and_the_other_still_pending(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "test.db")
    engine, positions, _broker = _build_engine(repository)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),  # only CE fills
        ],
    )
    assert len(positions.positions) == 1

    engine2, positions2, _broker2 = _restart(repository)
    assert len(positions2.positions) == 1  # CE adopted, not re-entered
    _feed(engine2, [_tick(PE1, 95.0, _ts(9, 45, 10))])
    assert len(positions2.positions) == 2
    assert repository.leg_order_history(leg_id=f"{BASKET_ID}:CE:1").__len__() == 1


def test_restart_with_both_legs_filled_never_duplicates_the_entry(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "test.db")
    engine, positions, _broker = _build_engine(repository)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
        ],
    )
    assert len(positions.positions) == 2

    engine2, positions2, _broker2 = _restart(repository)
    assert len(positions2.positions) == 2
    # Fresh candles must not re-propose entry — the day's one attempt stays
    # consumed across the restart.
    _feed(engine2, [_tick(NIFTY, 24000.0, _ts(9, 50, 0))])
    assert len(_leg_rows(repository)) == 2
    for leg_id in (f"{BASKET_ID}:CE:1", f"{BASKET_ID}:PE:1"):
        assert len(repository.leg_order_history(leg_id=leg_id)) == 1


def test_restart_after_a_definitively_rejected_initial_leg_resumes_it_safely(
    tmp_path: Path,
) -> None:
    """A rejected entry (unlike a rejected roll close) carries no consumed
    budget to protect — the shared, pre-existing, generic ``_reconcile_leg``
    contract deliberately leaves a PENDING_ORDER leg whose only entry order
    is a definitive TERMINAL_NO_FILL exactly as PENDING_ORDER, so the engine
    legitimately places one fresh, legitimate order for it on the next tick
    (see that function's own comment) — this is "resume safely", not
    "unwind"; nothing here is specific to rolling_strangle_otm1."""
    repository = _repository(tmp_path / "test.db")
    engine, positions, broker = _build_engine(repository)
    broker.outcomes[CE1] = "reject"
    _start(engine)
    _feed(engine, [_tick(NIFTY, 24000.0, _ts(9, 41)), _tick(NIFTY, 24000.0, _ts(9, 45, 0))])
    with pytest.raises(Exception, match="did not trade"):
        _feed(engine, [_tick(CE1, 100.0, _ts(9, 45, 5))])  # the "crash"
    assert not positions.positions
    legs = {leg["leg_role"]: leg for leg in _leg_rows(repository)}
    assert legs["CE"]["state"] == "PENDING_ORDER"  # never silently marked terminal

    engine2, positions2, _broker2 = _restart(repository)  # a fresh, healthy broker
    assert not positions2.positions  # nothing was ever confirmed to adopt
    assert engine2.entries_blocked is None  # a resolvable TERMINAL_NO_FILL never blocks
    _feed(engine2, [_tick(CE1, 100.0, _ts(9, 46, 0)), _tick(PE1, 95.0, _ts(9, 46, 5))])
    assert len(positions2.positions) == 2  # the retry, then PE, complete the basket


def test_restart_after_an_ambiguous_initial_leg_submission_fails_closed(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "test.db")
    engine, _positions, broker = _build_engine(repository)
    broker.outcomes[CE1] = "raise"
    _start(engine)
    _feed(engine, [_tick(NIFTY, 24000.0, _ts(9, 41)), _tick(NIFTY, 24000.0, _ts(9, 45, 0))])
    with pytest.raises(RuntimeError, match="simulated transport failure"):
        _feed(engine, [_tick(CE1, 100.0, _ts(9, 45, 5))])  # the "crash"

    with pytest.raises(UnmanageableBasketState, match="cannot be established"):
        _restart(repository)  # never guessed into a healthy or a failed state


def test_mismatched_lot_sizes_fail_closed_before_any_subscription_and_survive_restart(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "test.db")
    engine, positions, _broker = _build_engine(
        repository, resolver=_MismatchedLotSizeResolver()
    )
    _start(engine)
    _feed(engine, [_tick(NIFTY, 24000.0, _ts(9, 41)), _tick(NIFTY, 24000.0, _ts(9, 45, 0))])
    assert not positions.positions
    assert _leg_rows(repository) == []  # neither leg was ever created/subscribed

    _engine2, positions2, _broker2 = _restart(repository)
    assert not positions2.positions
    basket = _basket_row(repository)
    assert basket is not None and bool(basket["entries_consumed"]) is True  # not retried later


# ======================================================================
# B. Single-leg roll — full lifecycle across repeated restarts
# ======================================================================
def test_single_leg_roll_full_lifecycle_across_repeated_restarts(tmp_path: Path) -> None:
    """Five successive restarts of the same database, one per required
    durable checkpoint: before any claim; a claim committed with no close
    attempt dispatched at all (seeded — see module docstring); confirmed
    closed and AWAITING_NEXT_CANDLE; replacement pending; replacement
    filled. Never re-anchors, never reclaims the role's roll count, never
    duplicates a close or replacement."""
    import uuid

    repository = _repository(tmp_path / "test.db")
    engine, _positions, _broker = _build_engine(repository)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
        ],
    )
    ce_leg_id = f"{BASKET_ID}:CE:1"

    # Checkpoint 1: restart before any roll claim exists.
    engine2, positions2, _broker2 = _restart(repository)
    assert _rolls(repository) == []
    assert len(positions2.positions) == 2

    # Checkpoint 2 setup: seed CLAIMED with no close attempt dispatched —
    # unreachable via ticks alone for a single-target request (module
    # docstring) — via the real RollLedgerPort.commit_claims, the same
    # method _close_adjusted_legs_with_ledger itself calls.
    engine2._roll_ledger.commit_claims(
        engine2._basket,
        claim_group_id=f"{BASKET_ID}:{uuid.uuid4().hex[:8]}",
        targets=(AdjustmentTarget(leg_id=ce_leg_id, role=LegRole.CE),),
        anchor=AnchorUpdate(price=24100.0, candle_ts=_ts(9, 50, 0)),
        claim_candle_ts=_ts(9, 50, 0),
        claimed_at=_ts(9, 50, 0),
    )

    # Checkpoint 2: restart adopting a CLAIMED-with-no-intent target.
    engine3, _positions3, _broker3 = _restart(repository)
    rolls = _rolls(repository)
    assert len(rolls) == 1
    assert rolls[0]["lifecycle_state"] == "CLAIMED"
    assert rolls[0]["roll_sequence"] == 1
    assert rolls[0]["close_intent_id"] is None
    assert engine3.entries_blocked is None  # a bare CLAIMED row never blocks

    # The strategy naturally re-proposes the SAME CE leg once the (now
    # re-anchored-by-the-seed) reference spot is threatened again;
    # _find_resumable_claim_group must RESUME this exact claim, not double-
    # claim a second roll_sequence for CE.
    _feed(
        engine3,
        [_tick(NIFTY, 24170.0, _ts(9, 54, 0)), _tick(NIFTY, 24170.0, _ts(9, 55, 0))],
    )
    rolls = _rolls(repository)
    assert len(rolls) == 1  # never a second row for CE roll_sequence 1
    assert rolls[0]["roll_sequence"] == 1
    assert rolls[0]["lifecycle_state"] == "AWAITING_NEXT_CANDLE"
    # The resume path never re-anchors: the reference spot stays exactly
    # what the seed set it to (24100), not the 24170 that triggered resume.
    anchor = repository.load_basket_roll_anchor(
        strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE, basket_id=BASKET_ID,
    )
    assert anchor is not None and anchor["reference_price"] == 24100.0
    ce_leg = next(leg for leg in _leg_rows(repository) if leg["leg_id"] == ce_leg_id)
    assert ce_leg["state"] == "CLOSED"
    assert ce_leg["exit_reason"] == "ADJUSTMENT"

    # Checkpoint 3: restart with the group genuinely AWAITING_NEXT_CANDLE.
    engine4, positions4, _broker4 = _restart(repository)
    rolls = _rolls(repository)
    assert len(rolls) == 1 and rolls[0]["lifecycle_state"] == "AWAITING_NEXT_CANDLE"
    assert len(positions4.positions) == 1  # only PE open; CE genuinely closed

    # Next completed candle -> replacement enters (not on the trigger candle).
    _feed(
        engine4,
        [_tick(NIFTY, 24170.0, _ts(9, 59, 0)), _tick(NIFTY, 24170.0, _ts(10, 0, 0))],
    )
    replacement_ce = next(
        leg
        for leg in _leg_rows(repository)
        if leg["leg_id"] != ce_leg_id and leg["leg_role"] == "CE"
    )
    assert replacement_ce["state"] == "PENDING_ORDER"
    assert _rolls(repository)[0]["lifecycle_state"] == "REPLACEMENT_PENDING"

    # Checkpoint 4: restart with the replacement leg pending (subscribed,
    # not yet filled).
    engine5, positions5, _broker5 = _restart(repository)
    assert len(positions5.positions) == 1  # replacement not yet filled
    _feed(engine5, [_tick(replacement_ce["security_id"], 105.0, _ts(10, 0, 5))])
    assert len(positions5.positions) == 2
    rolls = _rolls(repository)
    assert rolls[0]["lifecycle_state"] == "REPLACEMENT_FILLED"
    assert rolls[0]["replacement_leg_id"] == replacement_ce["leg_id"]

    # Checkpoint 5: restart after REPLACEMENT_FILLED — no duplicate roll
    # claim, no duplicate replacement, no re-entry of the closed original.
    _engine6, positions6, _broker6 = _restart(repository)
    assert len(positions6.positions) == 2
    assert len(_rolls(repository)) == 1
    assert len(_leg_rows(repository)) == 3  # original CE, PE, replacement CE — never 4


def test_ce_budget_exhausted_after_two_rolls_survives_restart(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "test.db")
    engine, _positions, _broker = _build_engine(repository)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
            _tick(NIFTY, 24100.0, _ts(9, 49, 0)),
            _tick(NIFTY, 24100.0, _ts(9, 50, 0)),  # CE roll #1 claimed+confirmed
            _tick(NIFTY, 24100.0, _ts(9, 55, 0)),  # replacement #1 re-enters
            _tick("SIM:NIFTY:WEEKLY:24150:CE", 105.0, _ts(9, 55, 5)),
        ],
    )
    assert _rolls(repository)[0]["roll_sequence"] == 1

    # Restart mid-cycle, then complete roll #2.
    engine2, _positions2, _broker2 = _restart(repository)
    _feed(
        engine2,
        [
            _tick(NIFTY, 24200.0, _ts(9, 59, 0)),
            _tick(NIFTY, 24200.0, _ts(10, 0, 0)),  # CE roll #2 claimed+confirmed
            _tick(NIFTY, 24200.0, _ts(10, 5, 0)),  # replacement #2 re-enters
            _tick("SIM:NIFTY:WEEKLY:24250:CE", 110.0, _ts(10, 5, 5)),
        ],
    )
    ce_rolls = [r for r in _rolls(repository) if r["leg_role"] == "CE"]
    assert len(ce_rolls) == 2
    assert {r["roll_sequence"] for r in ce_rolls} == {1, 2}

    # Restart again with the budget exhausted; a third qualifying move must
    # never produce a third claim.
    engine3, positions3, _broker3 = _restart(repository)
    _feed(
        engine3,
        [_tick(NIFTY, 24300.0, _ts(10, 9, 0)), _tick(NIFTY, 24300.0, _ts(10, 10, 0))],
    )
    ce_rolls = [r for r in _rolls(repository) if r["leg_role"] == "CE"]
    assert len(ce_rolls) == 2  # unchanged — no third roll
    open_strikes = {p.contract.strike for p in positions3.positions}
    assert 24250.0 in open_strikes  # the roll-#2 CE, never rolled a third time


def test_replacement_expiry_at_cutoff_survives_restart(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "test.db")
    engine, _positions, _broker = _build_engine(repository, entry_time="15:00")
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(14, 56)),
            _tick(NIFTY, 24000.0, _ts(15, 0, 0)),
            _tick(CE1, 100.0, _ts(15, 0, 5)),
            _tick(PE1, 95.0, _ts(15, 0, 10)),
            _tick(NIFTY, 24100.0, _ts(15, 4, 0)),
            _tick(NIFTY, 24100.0, _ts(15, 5, 0)),  # CE roll claimed and confirmed closed
        ],
    )
    assert _rolls(repository)[0]["lifecycle_state"] == "AWAITING_NEXT_CANDLE"

    # Restart while genuinely awaiting a replacement, strictly before the
    # cutoff — an eligible replacement must remain resumable.
    engine2, _positions2, _broker2 = _restart(repository)
    assert _rolls(repository)[0]["lifecycle_state"] == "AWAITING_NEXT_CANDLE"

    # A restarted engine's candle builder has no in-progress bar to recall —
    # one tick to (re-)open the still-forming 15:05-15:10 bucket, then one
    # to complete it exactly at the cutoff.
    _feed(
        engine2,
        [_tick(NIFTY, 24100.0, _ts(15, 9, 0)), _tick(NIFTY, 24100.0, _ts(15, 10, 0))],
    )
    assert _rolls(repository)[0]["lifecycle_state"] == "REPLACEMENT_EXPIRED"

    # Restart after the expiry: it must stay expired, never retried, and the
    # untouched PE leg remains the only exposure.
    _engine3, positions3, _broker3 = _restart(repository)
    assert _rolls(repository)[0]["lifecycle_state"] == "REPLACEMENT_EXPIRED"
    open_strikes = {p.contract.strike for p in positions3.positions}
    assert open_strikes == {23950.0}


# ======================================================================
# C. Both-leg roll support (spec section 9.3; generic since Phase 2)
# ======================================================================
def test_both_leg_claim_and_anchor_commit_atomically_and_survive_restart(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "test.db")
    engine, _positions, _broker = _build_engine(repository, single_leg_roll=False)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
            _tick(NIFTY, 24100.0, _ts(9, 49, 0)),
            _tick(NIFTY, 24100.0, _ts(9, 50, 0)),  # both-leg claim (move=100)
        ],
    )
    rolls = _rolls(repository)
    assert len(rolls) == 2
    assert {r["leg_role"] for r in rolls} == {"CE", "PE"}
    group_ids = {r["claim_group_id"] for r in rolls}
    assert len(group_ids) == 1  # one shared claim_group_id, not two independent ones
    anchor = repository.load_basket_roll_anchor(
        strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE, basket_id=BASKET_ID,
    )
    assert anchor is not None and anchor["reference_price"] == 24100.0

    _engine2, _positions2, _broker2 = _restart(repository)
    rolls2 = _rolls(repository)
    assert len(rolls2) == 2  # neither member ever split or lost
    assert {r["claim_group_id"] for r in rolls2} == group_ids


def test_one_confirmed_one_rejected_close_blocks_replacement_for_the_whole_group(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "test.db")
    engine, _positions, broker = _build_engine(repository, single_leg_roll=False)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
        ],
    )
    broker.outcomes[PE1] = "reject"  # CE will confirm; PE will definitively fail
    _feed(engine, [_tick(NIFTY, 24100.0, _ts(9, 49, 0)), _tick(NIFTY, 24100.0, _ts(9, 50, 0))])

    rolls = {r["leg_role"]: r for r in _rolls(repository)}
    assert rolls["CE"]["lifecycle_state"] == "EXIT_CONFIRMED"
    assert rolls["PE"]["lifecycle_state"] == "FAILED"
    # Neither reaches AWAITING_NEXT_CANDLE — the group needs every member
    # confirmed, and PE's own leg is reconstructed OPEN (roll_count for PE
    # is NOT refunded, but the leg keeps trading normally).
    pe_leg = next(leg for leg in _leg_rows(repository) if leg["leg_role"] == "PE")
    assert pe_leg["state"] == "OPEN"

    # Restart: the partially-confirmed group must not be silently advanced,
    # and no replacement is ever proposed for either role from this group.
    engine2, _positions2, _broker2 = _restart(repository)
    rolls2 = {r["leg_role"]: r for r in _rolls(repository)}
    assert rolls2["CE"]["lifecycle_state"] == "EXIT_CONFIRMED"  # not silently advanced
    assert rolls2["PE"]["lifecycle_state"] == "FAILED"
    _feed(engine2, [_tick(NIFTY, 24100.0, _ts(9, 55, 0))])  # a later candle, still no move
    # No ENTER_LEG ever fires for either role from this group.
    assert len([leg for leg in _leg_rows(repository) if leg["leg_role"] == "CE"]) == 1
    assert len([leg for leg in _leg_rows(repository) if leg["leg_role"] == "PE"]) == 1


def test_both_leg_replacement_consumption_is_atomic_and_survives_restart(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "test.db")
    engine, _positions, _broker = _build_engine(repository, single_leg_roll=False)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
            _tick(NIFTY, 24100.0, _ts(9, 49, 0)),
            _tick(NIFTY, 24100.0, _ts(9, 50, 0)),  # both-leg claim, both confirmed
        ],
    )
    rolls = {r["leg_role"]: r for r in _rolls(repository)}
    assert rolls["CE"]["lifecycle_state"] == "AWAITING_NEXT_CANDLE"
    assert rolls["PE"]["lifecycle_state"] == "AWAITING_NEXT_CANDLE"

    # Restart while genuinely awaiting both replacements.
    engine2, _positions2, _broker2 = _restart(repository)
    rolls2 = {r["leg_role"]: r for r in _rolls(repository)}
    assert rolls2["CE"]["lifecycle_state"] == "AWAITING_NEXT_CANDLE"
    assert rolls2["PE"]["lifecycle_state"] == "AWAITING_NEXT_CANDLE"

    _feed(
        engine2,
        [_tick(NIFTY, 24100.0, _ts(9, 54, 0)), _tick(NIFTY, 24100.0, _ts(9, 55, 0))],
    )  # next completed candle (a restarted candle builder recalls no in-progress bar)
    legs_by_role = {}
    for leg in _leg_rows(repository):
        legs_by_role.setdefault(leg["leg_role"], []).append(leg)
    new_ce = next(leg for leg in legs_by_role["CE"] if leg["leg_sequence"] == 2)
    new_pe = next(leg for leg in legs_by_role["PE"] if leg["leg_sequence"] == 2)
    assert new_ce["state"] == "PENDING_ORDER"
    assert new_pe["state"] == "PENDING_ORDER"
    rolls3 = {r["leg_role"]: r for r in _rolls(repository)}
    assert rolls3["CE"]["lifecycle_state"] == "REPLACEMENT_PENDING"
    assert rolls3["PE"]["lifecycle_state"] == "REPLACEMENT_PENDING"

    # Restart with both replacements pending, then fill both.
    engine3, positions3, _broker3 = _restart(repository)
    _feed(
        engine3,
        [
            _tick(new_ce["security_id"], 105.0, _ts(9, 55, 5)),
            _tick(new_pe["security_id"], 90.0, _ts(9, 55, 10)),
        ],
    )
    assert len(positions3.positions) == 2
    rolls4 = {r["leg_role"]: r for r in _rolls(repository)}
    assert rolls4["CE"]["lifecycle_state"] == "REPLACEMENT_FILLED"
    assert rolls4["PE"]["lifecycle_state"] == "REPLACEMENT_FILLED"

    # Final restart: no duplicate group, no duplicate replacement.
    _engine4, positions4, _broker4 = _restart(repository)
    assert len(positions4.positions) == 2
    assert len(_rolls(repository)) == 2


# ======================================================================
# D. Multiple exit attempts / close_intent_id-scoped reconciliation
# ======================================================================
def test_rejected_roll_close_then_a_later_square_off_stays_distinguishable(
    tmp_path: Path,
) -> None:
    """Spec section 10.2 / migration 0013's own required lifecycle: a
    definitively rejected roll close leaves the leg OPEN, trading normally
    (roll_sequence not refunded); hard square-off later closes that same
    leg through the ordinary sell/buy path — a second, unrelated exit-side
    order_intents row for one leg_id. Reconciliation must never treat this
    as a contradiction, and the roll claim's own outcome must be resolved
    strictly from its own close_intent_id, never from the later attempt."""
    repository = _repository(tmp_path / "test.db")
    engine, _positions, broker = _build_engine(repository)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
        ],
    )
    broker.outcomes[CE1] = "reject"
    _feed(engine, [_tick(NIFTY, 24100.0, _ts(9, 49, 0)), _tick(NIFTY, 24100.0, _ts(9, 50, 0))])
    roll = _rolls(repository)[0]
    assert roll["lifecycle_state"] == "FAILED"
    ce_leg = next(leg for leg in _leg_rows(repository) if leg["leg_role"] == "CE")
    assert ce_leg["state"] == "OPEN"  # reconstructed OPEN, trading normally

    # Restart mid-day with the rejected roll and the still-open leg.
    engine2, positions2, broker2 = _restart(repository)
    assert len(positions2.positions) == 2
    roll2 = _rolls(repository)[0]
    assert roll2["lifecycle_state"] == "FAILED"

    # Hard square-off at 15:15 closes both legs, including the same CE leg
    # the rejected roll never actually closed — through the ordinary
    # sell/buy path, a genuinely separate exit-side order_intents row.
    broker2.outcomes.pop(CE1, None)  # square-off must succeed this time
    _feed(engine2, [_tick(NIFTY, 24100.0, _ts(15, 15, 0))])
    assert positions2.positions == []
    ce_exits = [
        r for r in repository.leg_order_history(leg_id=ce_leg["leg_id"]) if r["side"] != "SELL"
    ]
    assert len(ce_exits) == 2  # the rejected roll attempt + the square-off

    # Final restart after the confirmed square-off: reconciles cleanly
    # despite two exit-side rows for the same leg_id; the roll claim's own
    # recovery is scoped strictly to its own close_intent_id and is
    # unaffected by the later, unrelated square-off attempt.
    _engine3, positions3, _broker3 = _restart(repository)
    assert positions3.positions == []
    roll3 = _rolls(repository)[0]
    assert roll3["lifecycle_state"] == "FAILED"


_SEQ = iter(range(500_000, 600_000))


def _place_confirmed_exit(
    repository: ExecutionRepository, *, session_id: int, security_id: str, leg_id: str,
    fill_price: float,
) -> None:
    """A second, independent confirmed closing fill for a leg the real
    engine already closed once — structurally unreachable through
    PositionManager itself (it refuses to close an already-closed
    position), so this is written through the same real repository API
    ``test_straddle_920_reconciliation.py``'s own ``_place_order`` helper
    uses (reserve_intent/record_submission/apply_fill — never raw SQL) to
    model the only way such a row could exist: corrupted/concurrent-writer
    historical data, exactly the case the over-close defence exists for."""
    from common.models import Fill, Order, OrderIntent, OrderStatus, OrderType

    seq = next(_SEQ)
    correlation_id = f"p_test_overclose_{seq}"
    intent = OrderIntent(
        correlation_id=correlation_id, strategy_id=STRATEGY_ID, runtime_id=RUNTIME_ID,
        execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE, sequence_number=seq,
        instrument="NIFTY", security_id=security_id, side=OrderSide.BUY, quantity=750,
        order_type=OrderType.MARKET, product_type="INTRADAY", created_at=datetime.now(UTC),
        basket_id=BASKET_ID, leg_id=leg_id,
    )
    intent_id = repository.reserve_intent(session_id=session_id, intent=intent)
    order = Order(
        correlation_id=correlation_id, strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER,
        status=OrderStatus.FILLED, updated_at=datetime.now(UTC), filled_quantity=750,
        average_fill_price=fill_price,
    )
    order_id = repository.record_submission(intent_id=intent_id, order=order, runtime_id=RUNTIME_ID)
    fill = Fill(
        correlation_id=correlation_id, broker_fill_id=f"fill_{seq}", strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER, quantity=750, price=fill_price,
        filled_at=datetime.now(UTC),
    )
    repository.apply_fill(
        order_id=order_id, runtime_id=RUNTIME_ID, fill=fill, order_status=OrderStatus.FILLED,
        instrument="NIFTY", security_id=security_id, side=OrderSide.BUY, trading_date=TRADING_DATE,
    )


def test_two_confirmed_fills_for_one_leg_is_an_over_close_that_fails_closed(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "test.db")
    engine, _positions, _broker = _build_engine(repository)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
            _tick(NIFTY, 24100.0, _ts(9, 49, 0)),
            _tick(NIFTY, 24100.0, _ts(9, 50, 0)),  # CE roll closes it for real, once
        ],
    )
    ce_leg = next(leg for leg in _leg_rows(repository) if leg["leg_role"] == "CE")
    assert ce_leg["state"] == "CLOSED"

    session_id = repository.open_session(
        runtime_id=RUNTIME_ID, strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER,
        process_role="worker", pid=999,
    ).id
    _place_confirmed_exit(
        repository, session_id=session_id, security_id=CE1, leg_id=ce_leg["leg_id"],
        fill_price=50.0,
    )

    with pytest.raises(UnmanageableBasketState, match="over-close contradiction"):
        _restart(repository)


def test_an_unrecognised_roll_lifecycle_state_fails_the_whole_basket_closed(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "test.db")
    engine, _positions, _broker = _build_engine(repository)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
            _tick(NIFTY, 24100.0, _ts(9, 49, 0)),
            _tick(NIFTY, 24100.0, _ts(9, 50, 0)),
        ],
    )
    # A durable lifecycle_state this code does not (yet) recognise — the
    # real repository API, not raw SQL, modelling a future vocabulary
    # extension or corrupted data reconciliation must never guess about.
    repository.update_basket_roll_outcome(
        basket_id=BASKET_ID, leg_role="CE", roll_sequence=1,
        lifecycle_state="SOME_FUTURE_STATE_THIS_CODE_DOES_NOT_KNOW",
    )
    with pytest.raises(UnmanageableBasketState, match="unrecognised lifecycle_state"):
        _restart(repository)


# ======================================================================
# E. Durable strategy state preservation across restart
# ======================================================================
def test_no_same_day_reentry_after_the_basket_has_been_blocked(tmp_path: Path) -> None:
    """Once the combined stop blocks the day, a restart must never let a
    fresh candle re-propose a primary entry or a roll — day_blocked_reason
    is durable and adopted, not just an in-memory latch."""
    repository = _repository(tmp_path / "test.db")
    engine, positions, _broker = _build_engine(repository)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
            # unrealised on CE alone: (100 - 127) * 750 = -20250 <= -20000
            _tick(CE1, 127.0, _ts(9, 46, 0)),
        ],
    )
    assert not positions.positions
    basket = _basket_row(repository)
    assert basket is not None and basket["day_blocked_reason"] is not None

    engine2, _positions2, _broker2 = _restart(repository)
    assert engine2.entries_blocked is not None
    _feed(engine2, [_tick(NIFTY, 24000.0, _ts(9, 50, 0)), _tick(NIFTY, 24100.0, _ts(9, 55, 0))])
    assert _basket_row(repository) is not None
    assert _leg_rows(repository).__len__() == 2  # no new leg ever created after the block


def test_a_same_day_restart_does_not_reset_durable_fields(tmp_path: Path) -> None:
    """spec section 17.7: same-day restart preserves entries_consumed, roll
    counts and the reference spot; only a genuinely new trading day (a
    fresh Basket, per MultiLegEngine._start_day) resets them — which
    RollingStrangleOtm1Strategy.reset() deliberately never does itself."""
    repository = _repository(tmp_path / "test.db")
    engine, _positions, _broker = _build_engine(repository)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
            _tick(NIFTY, 24100.0, _ts(9, 49, 0)),
            _tick(NIFTY, 24100.0, _ts(9, 50, 0)),  # CE roll #1
            _tick(NIFTY, 24100.0, _ts(9, 55, 0)),
            _tick("SIM:NIFTY:WEEKLY:24150:CE", 105.0, _ts(9, 55, 5)),
        ],
    )
    _engine2, _positions2, _broker2 = _restart(repository)
    basket = _basket_row(repository)
    assert basket is not None and bool(basket["entries_consumed"]) is True
    anchor = repository.load_basket_roll_anchor(
        strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE, basket_id=BASKET_ID,
    )
    assert anchor is not None and anchor["reference_price"] == 24100.0
    ce_rolls = [r for r in _rolls(repository) if r["leg_role"] == "CE"]
    assert len(ce_rolls) == 1 and ce_rolls[0]["roll_sequence"] == 1


# ======================================================================
# F. Time and forced-exit recovery
# ======================================================================
def test_hard_square_off_flattens_every_open_leg_and_survives_restart(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "test.db")
    engine, positions, _broker = _build_engine(repository)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
        ],
    )
    _feed(engine, [_tick(NIFTY, 24000.0, _ts(15, 15, 0))])  # >= 15:15 -> hard square-off
    assert positions.positions == []
    legs = _leg_rows(repository)
    assert all(leg["state"] == "CLOSED" for leg in legs)
    assert all(leg["exit_reason"] == "SQUARE_OFF" for leg in legs)

    # Restart after a confirmed square-off: no exposure is recreated.
    _engine2, positions2, _broker2 = _restart(repository)
    assert positions2.positions == []
    basket2 = _basket_row(repository)
    assert basket2 is not None and basket2["square_off_state"] != "PENDING"


def test_a_one_sided_partial_basket_is_still_flattened_at_hard_square_off(
    tmp_path: Path,
) -> None:
    """Spec section 10.2 / 17.5: hard square-off closes one-sided/partial
    baskets too — here only CE ever filled."""
    repository = _repository(tmp_path / "test.db")
    engine, positions, _broker = _build_engine(repository)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),  # PE never fills
        ],
    )
    assert len(positions.positions) == 1

    engine2, positions2, _broker2 = _restart(repository)
    assert len(positions2.positions) == 1  # CE adopted
    _feed(engine2, [_tick(NIFTY, 24000.0, _ts(15, 15, 0))])
    assert positions2.positions == []
    ce_leg = next(leg for leg in _leg_rows(repository) if leg["leg_role"] == "CE")
    assert ce_leg["state"] == "CLOSED"
    pe_leg = next(leg for leg in _leg_rows(repository) if leg["leg_role"] == "PE")
    assert pe_leg["state"] in ("PENDING_ORDER", "FAILED", "EXPIRED")  # never fabricated OPEN


def test_hard_square_off_still_fires_with_no_prior_underlying_candle_data(
    tmp_path: Path,
) -> None:
    """Spec section 10.2: hard square-off must remain available even when
    selection/quote/underlying data is degraded — it is engine/session-
    owned, evaluated at the very top of on_tick, independent of the
    candle/strategy machinery."""
    repository = _repository(tmp_path / "test.db")
    engine, _positions, _broker = _build_engine(repository)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
        ],
    )
    engine2, positions2, _broker2 = _restart(repository)
    # The very first tick this restarted engine ever sees is already past
    # the square-off boundary -- no underlying candle was rebuilt at all.
    _feed(engine2, [_tick(NIFTY, 24000.0, _ts(15, 20, 0))])
    assert positions2.positions == []



# ======================================================================
# G. Combined-stop boundary, with restart
# ======================================================================
def test_minus_19999_does_not_trigger_and_survives_restart(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "test.db")
    engine, positions, _broker = _build_engine(repository)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
            # unrealised on CE alone: (100 - 126.665) * 750 = -19998.75
            _tick(CE1, 126.665, _ts(9, 46, 0)),
        ],
    )
    assert len(positions.positions) == 2  # not triggered
    basket = _basket_row(repository)
    assert basket is not None and basket["day_blocked_reason"] is None

    engine2, positions2, _broker2 = _restart(repository)
    assert len(positions2.positions) == 2
    assert engine2.entries_blocked is None


def test_minus_20000_triggers_inclusively_and_survives_restart(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "test.db")
    engine, positions, _broker = _build_engine(repository)
    _start(engine)
    _feed(
        engine,
        [
            _tick(NIFTY, 24000.0, _ts(9, 41)),
            _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
            _tick(CE1, 100.0, _ts(9, 45, 5)),
            _tick(PE1, 95.0, _ts(9, 45, 10)),
            # unrealised on CE alone: (100 - 126.6667) * 750 = -20000.025
            _tick(CE1, 126.6667, _ts(9, 46, 0)),
        ],
    )
    assert positions.positions == []  # triggered, everything closed

    _engine2, positions2, _broker2 = _restart(repository)
    assert positions2.positions == []
    basket = _basket_row(repository)
    assert basket is not None and basket["day_blocked_reason"] is not None
