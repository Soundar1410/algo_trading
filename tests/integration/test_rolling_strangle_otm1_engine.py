"""``rolling_strangle_otm1`` driven through a real :class:`MultiLegEngine`, a
real ``ExecutionRepository``/``RollLedger``, and a real
:class:`LifecycleGateway`/``OrderLifecycle`` over a scripted (but otherwise
real) broker — no monkeypatching, no real network.

Complements ``tests/unit/test_rolling_strangle_otm1_strategy.py``'s pure
decision-level coverage with everything only a real engine run proves:
contract resolution (OTM strike/lot-size), the mismatched-lot-size fail-
closed entry check, fresh-tick fill gating, and end-to-end atomic roll-
claim/replacement/both-leg/combined-stop cycles through the durable roll
ledger this strategy actually depends on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from common.broker.base import Quote
from common.config.models import ExecutionMode
from common.engine.config import EngineConfig, SessionConfig
from common.engine.feed import SimulatedFeed
from common.engine.gateway import LifecycleGateway
from common.engine.models import OptionContract, OptionType
from common.engine.multi_leg_engine import MultiLegEngine
from common.engine.multi_leg_models import Basket, LegInstance
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
from common.models import ExitReason, Fill, Order, OrderIntent, OrderStatus, Tick
from common.persistence import Database, migrate
from runtimes.intraday_options.multi_leg_engine_worker import recover_basket
from strategies.intraday_options.rolling_strangle_otm1.strategy import RollingStrangleOtm1Strategy

IST = ZoneInfo("Asia/Kolkata")
RUNTIME_ID = "intraday_options"
STRATEGY_ID = "rolling_strangle_otm1"
TRADING_DATE = "2026-08-17"
NIFTY = "NIFTY_IDX"


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 17, hour, minute, second, tzinfo=IST)  # a Monday


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id, instrument=security_id, last_price=price,
        exchange_time=ts, received_at=ts,
    )


@dataclass
class _ScriptedBroker:
    """Fills every order at the quote's last price unless overridden. See
    ``tests/integration/test_rolling_multi_leg_engine.py``'s own
    ``_ScriptedBroker`` for the full outcome vocabulary this mirrors."""

    submit_calls: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "scripted-fake"

    def submit(self, intent: OrderIntent, quote: Quote) -> Order:
        self.submit_calls.append(intent.security_id)
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


class _MismatchedLotSizeResolver(OptionChainResolver):
    """CE and PE resolve to different lot sizes — proving spec section 6.4
    point 8's fail-closed entry check without needing real scrip-master
    data."""

    def resolve(
        self, strike: int, option_type: OptionType, expiry: str | None = None
    ) -> OptionContract:
        lot_size = 75 if option_type is OptionType.CE else 65
        return OptionContract(
            symbol=f"NIFTY {strike} {option_type.value}",
            security_id=f"SIM:NIFTY:WEEKLY:{strike}:{option_type.value}",
            strike=float(strike),
            option_type=option_type,
            expiry=expiry or "WEEKLY",
            lot_size=lot_size,
        )


def _repository(tmp_path) -> ExecutionRepository:  # type: ignore[no-untyped-def]
    db = Database(tmp_path / "test.db")
    migrate(db)
    return ExecutionRepository(db)


@dataclass
class _FakeConfig:
    runtime_id: str
    strategy_id: str
    execution_mode: ExecutionMode
    trading_date: str


def _config() -> _FakeConfig:
    return _FakeConfig(
        runtime_id=RUNTIME_ID, strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE,
    )


def _build_engine(
    repository: ExecutionRepository,
    *,
    resolver: OptionChainResolver | None = None,
    lot_size: int = 75,
    single_leg_roll: bool = True,
    max_rolls_ce: int = 2,
    max_rolls_pe: int = 2,
    combined_stop_per_lot: float = 2000.0,
    lots_per_leg: int = 10,
    entry_time: str = "09:45",
) -> tuple[MultiLegEngine, PositionManager, _ScriptedBroker]:
    broker = _ScriptedBroker()
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
    positions = PositionManager(gateway, lots=lots_per_leg)
    roll_ledger = RollLedger(
        repository, runtime_id=RUNTIME_ID, strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE,
    )
    strategy = RollingStrangleOtm1Strategy(
        lots_per_leg=lots_per_leg,
        entry_time=entry_time,
        stop_new_entries_after="15:10",
        square_off_time="15:15",
        strike_step=50,
        otm_distance_points=50,
        roll_trigger_points=60,
        max_rolls_ce=max_rolls_ce,
        max_rolls_pe=max_rolls_pe,
        single_leg_roll=single_leg_roll,
        combined_stop_per_lot=combined_stop_per_lot,
    )

    def _persist_basket_cb(basket: Basket) -> None:
        _persist_basket_row(repository, basket, runtime_id=RUNTIME_ID)

    def _persist_leg_cb(leg: LegInstance) -> None:
        _persist_leg_row(
            repository, leg, runtime_id=RUNTIME_ID, strategy_id=STRATEGY_ID,
            execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE,
        )

    def _recover() -> Basket | None:
        return recover_basket(_config(), repository)

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
            resolver or SimulatedOptionChainResolver("NIFTY", lot_size=lot_size), strike_step=50
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


def _run(engine: MultiLegEngine, ticks: list[Tick]) -> None:
    engine.feed = SimulatedFeed(ticks)
    engine.run()


# ---------------------------------------------------------------- 1. entry
def test_primary_entry_sells_one_step_otm_ce_and_pe(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    engine, positions, _broker = _build_engine(repository)
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 41)),
        _tick(NIFTY, 24000.0, _ts(9, 45, 0)),  # closes the 09:40-09:45 candle -> ENTER_BASKET
        _tick("SIM:NIFTY:WEEKLY:24050:CE", 100.0, _ts(9, 45, 5)),
        _tick("SIM:NIFTY:WEEKLY:23950:PE", 95.0, _ts(9, 45, 10)),
    ]
    _run(engine, ticks)

    assert len(positions.positions) == 2
    strikes = {p.contract.strike for p in positions.positions}
    assert strikes == {24050.0, 23950.0}  # ATM +/- one 50pt OTM step


def test_quantity_is_lots_times_resolved_lot_size(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    engine, positions, _broker = _build_engine(repository, lot_size=65, lots_per_leg=10)
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 41)),
        _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
        _tick("SIM:NIFTY:WEEKLY:24050:CE", 100.0, _ts(9, 45, 5)),
        _tick("SIM:NIFTY:WEEKLY:23950:PE", 95.0, _ts(9, 45, 10)),
    ]
    _run(engine, ticks)
    assert all(p.quantity == 650 for p in positions.positions)  # 10 lots x 65


def test_mismatched_lot_sizes_fail_closed_before_entry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    engine, positions, _broker = _build_engine(
        repository, resolver=_MismatchedLotSizeResolver()
    )
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 41)),
        _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
        _tick("SIM:NIFTY:WEEKLY:24050:CE", 100.0, _ts(9, 45, 5)),
        _tick("SIM:NIFTY:WEEKLY:23950:PE", 95.0, _ts(9, 45, 10)),
    ]
    _run(engine, ticks)
    assert not positions.positions
    assert not positions.trades


def test_no_fill_before_a_fresh_contract_tick(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    engine, positions, _broker = _build_engine(repository)
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 41)),
        _tick(NIFTY, 24000.0, _ts(9, 45, 0)),  # ENTER_BASKET queued, no leg ticks yet
    ]
    _run(engine, ticks)
    assert not positions.positions


# ------------------------------------------------------------- 2. single-leg roll
def test_single_leg_roll_end_to_end_claims_closes_and_replaces(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    engine, positions, _broker = _build_engine(repository)
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 41)),
        _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
        _tick("SIM:NIFTY:WEEKLY:24050:CE", 100.0, _ts(9, 45, 5)),
        _tick("SIM:NIFTY:WEEKLY:23950:PE", 95.0, _ts(9, 45, 10)),
        # Same 09:45-09:50 bucket: an intra-bucket tick moves the running
        # close to 24100 (move=100 >= 60) *before* the bucket rolls over —
        # the rollover tick's own price only opens the *next* bucket, so
        # the trigger candle's close must be set here, not there.
        _tick(NIFTY, 24100.0, _ts(9, 49, 0)),
        _tick(NIFTY, 24100.0, _ts(9, 50, 0)),  # completes 09:45-09:50 @ 24100 -> CE roll
        # Next completed candle -> replacement re-enters CE at the new ATM
        # (spot 24100 at the moment the replacement is processed).
        _tick(NIFTY, 24100.0, _ts(9, 55, 0)),
        _tick("SIM:NIFTY:WEEKLY:24150:CE", 105.0, _ts(9, 55, 5)),
    ]
    _run(engine, ticks)

    rolls = repository.load_basket_rolls(basket_id=f"{STRATEGY_ID}:{TRADING_DATE}")
    assert len(rolls) == 1
    assert rolls[0]["leg_role"] == "CE"
    assert rolls[0]["roll_sequence"] == 1
    assert rolls[0]["lifecycle_state"] == "REPLACEMENT_FILLED"

    open_strikes = {p.contract.strike for p in positions.positions}
    assert 24150.0 in open_strikes  # new CE, one OTM step above the new ATM
    assert 23950.0 in open_strikes  # original, unrolled PE
    assert 24050.0 not in open_strikes  # old CE is gone


def test_ce_budget_exhausted_after_two_rolls_refuses_a_third(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    engine, positions, _broker = _build_engine(repository, max_rolls_ce=2)
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 41)),
        _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
        _tick("SIM:NIFTY:WEEKLY:24050:CE", 100.0, _ts(9, 45, 5)),
        _tick("SIM:NIFTY:WEEKLY:23950:PE", 95.0, _ts(9, 45, 10)),
        _tick(NIFTY, 24100.0, _ts(9, 49, 0)),
        _tick(NIFTY, 24100.0, _ts(9, 50, 0)),  # roll #1 claimed (move=100 vs ref 24000)
        _tick(NIFTY, 24100.0, _ts(9, 55, 0)),  # replacement #1 (spot 24100)
        _tick("SIM:NIFTY:WEEKLY:24150:CE", 105.0, _ts(9, 55, 5)),
        _tick(NIFTY, 24200.0, _ts(9, 59, 0)),
        _tick(NIFTY, 24200.0, _ts(10, 0, 0)),  # roll #2 claimed (move=100 vs ref 24100)
        _tick(NIFTY, 24200.0, _ts(10, 5, 0)),  # replacement #2 (spot 24200)
        _tick("SIM:NIFTY:WEEKLY:24250:CE", 110.0, _ts(10, 5, 5)),
        _tick(NIFTY, 24300.0, _ts(10, 9, 0)),
        _tick(NIFTY, 24300.0, _ts(10, 10, 0)),  # would-be roll #3 -- budget exhausted
    ]
    _run(engine, ticks)

    rolls = repository.load_basket_rolls(basket_id=f"{STRATEGY_ID}:{TRADING_DATE}")
    ce_rolls = [r for r in rolls if r["leg_role"] == "CE"]
    assert len(ce_rolls) == 2
    open_strikes = {p.contract.strike for p in positions.positions}
    assert 24250.0 in open_strikes  # still the roll-#2 CE, never rolled a third time


def test_replacement_expires_when_next_candle_reaches_cutoff(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    # entry_time moved to 15:00 so the whole scenario fits in one short,
    # gap-free 5-min tick chain up to the 15:10 cutoff, instead of a long
    # (and irrelevant) chain of empty candles from 09:45.
    engine, positions, _broker = _build_engine(repository, entry_time="15:00")
    ticks = [
        _tick(NIFTY, 24000.0, _ts(14, 56)),
        _tick(NIFTY, 24000.0, _ts(15, 0, 0)),  # ENTER_BASKET, anchor=24000
        _tick("SIM:NIFTY:WEEKLY:24050:CE", 100.0, _ts(15, 0, 5)),
        _tick("SIM:NIFTY:WEEKLY:23950:PE", 95.0, _ts(15, 0, 10)),
        _tick(NIFTY, 24100.0, _ts(15, 4, 0)),
        _tick(NIFTY, 24100.0, _ts(15, 5, 0)),  # CE roll claimed and confirmed closed
        _tick(NIFTY, 24100.0, _ts(15, 10, 0)),  # next candle is at the cutoff -> expires
    ]
    _run(engine, ticks)

    rolls = repository.load_basket_rolls(basket_id=f"{STRATEGY_ID}:{TRADING_DATE}")
    ce_rolls = [r for r in rolls if r["leg_role"] == "CE"]
    assert len(ce_rolls) == 1
    assert ce_rolls[0]["lifecycle_state"] == "REPLACEMENT_EXPIRED"
    open_strikes = {p.contract.strike for p in positions.positions}
    assert 23950.0 in open_strikes  # PE untouched, still managed
    assert 24050.0 not in open_strikes


# ------------------------------------------------------------- 3. both-leg mode
def test_both_leg_mode_closes_and_replaces_both_atomically(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    engine, positions, _broker = _build_engine(repository, single_leg_roll=False)
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 41)),
        _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
        _tick("SIM:NIFTY:WEEKLY:24050:CE", 100.0, _ts(9, 45, 5)),
        _tick("SIM:NIFTY:WEEKLY:23950:PE", 95.0, _ts(9, 45, 10)),
        _tick(NIFTY, 24100.0, _ts(9, 49, 0)),
        _tick(NIFTY, 24100.0, _ts(9, 50, 0)),  # both-leg claim + close (move=100)
        _tick(NIFTY, 24100.0, _ts(9, 55, 0)),  # both replacements re-enter (spot 24100)
        _tick("SIM:NIFTY:WEEKLY:24150:CE", 105.0, _ts(9, 55, 5)),
        _tick("SIM:NIFTY:WEEKLY:24050:PE", 90.0, _ts(9, 55, 10)),
    ]
    _run(engine, ticks)

    rolls = repository.load_basket_rolls(basket_id=f"{STRATEGY_ID}:{TRADING_DATE}")
    assert len(rolls) == 2
    assert {r["leg_role"] for r in rolls} == {"CE", "PE"}
    assert all(r["lifecycle_state"] == "REPLACEMENT_FILLED" for r in rolls)
    open_strikes = {p.contract.strike for p in positions.positions}
    assert open_strikes == {24150.0, 24050.0}


# ------------------------------------------------------------- 4. risk
def test_combined_stop_closes_every_open_leg_and_blocks_further_rolls(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    engine, positions, _broker = _build_engine(
        repository, lots_per_leg=10, combined_stop_per_lot=2000.0
    )
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 41)),
        _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
        _tick("SIM:NIFTY:WEEKLY:24050:CE", 100.0, _ts(9, 45, 5)),
        _tick("SIM:NIFTY:WEEKLY:23950:PE", 95.0, _ts(9, 45, 10)),
        # unrealised on the CE leg alone: (100 - 127) * 750 = -20250 <= -20000
        _tick("SIM:NIFTY:WEEKLY:24050:CE", 127.0, _ts(9, 46, 0)),
    ]
    _run(engine, ticks)

    assert not positions.positions  # everything closed
    assert positions.trades  # via a real close, not silently dropped
    exit_reasons = {t.exit_reason for t in positions.trades}
    assert exit_reasons == {ExitReason.DAILY_LOSS_LIMIT}
