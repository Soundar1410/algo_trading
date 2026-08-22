"""Phase 5C: end-to-end and no-network proofs for ``rolling_strangle_otm1``.

Real config loading, real ``discover_strategies``/``build_supervisor``
composition (alongside ``ema_cross_9_21_buy`` and ``straddle_920``, never in
isolation), real repository migrations through ``0013``, a real temporary
SQLite database, and a complete entry -> roll -> replacement -> restart ->
exit lifecycle through the real ``MultiLegEngine`` — scripted/in-process feed
and broker only, exactly as every other phase of this port has used.

The no-network proof reuses the exact sentinel technique
``tests/end_to_end/test_notification_guard_spawn.py`` already established
for this repository: a ``socket.socket.connect``/``connect_ex`` wrapper that
refuses (and logs) any non-loopback address. Applied here in-process, for
the duration of the whole lifecycle test, rather than across a spawned
process boundary — nothing in this strategy's own test family ever spawns a
worker process or a dashboard; the risk surface being measured is the
in-process one this file's own fixtures/harness exercise, matching what a
real worker process run through this exact engine composition would also do.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from common.broker.base import Quote
from common.config import load_paths
from common.config.loader import discover_enabled_strategies, discover_strategies
from common.config.models import ExecutionMode
from common.engine.config import EngineConfig, SessionConfig
from common.engine.feed import SimulatedFeed
from common.engine.gateway import LifecycleGateway
from common.engine.multi_leg_engine import MultiLegEngine
from common.engine.multi_leg_models import Basket, LegInstance
from common.engine.multi_leg_state import RollLedger
from common.engine.multi_leg_state import persist_basket as _persist_basket_row
from common.engine.multi_leg_state import persist_leg as _persist_leg_row
from common.engine.positions import PositionManager
from common.engine.selection import OptionSelector, SimulatedOptionChainResolver
from common.execution import ExecutionRepository, OrderLifecycle
from common.market_data import RecordedFeedAdapter, load_tick_tape
from common.models import Fill, Order, OrderStatus, Tick
from common.persistence import Database, migrate
from common.process.legacy_guard import LaunchdLabelState, LegacySystemStatus
from runtimes.intraday_options import __main__ as runtime_main
from runtimes.intraday_options.__main__ import build_supervisor
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
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_CONFIG = REPO_ROOT / "config"


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 17, hour, minute, second, tzinfo=IST)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id, instrument=security_id, last_price=price,
        exchange_time=ts, received_at=ts,
    )


# ---------------------------------------------------------- network sentinel
def _is_local(address: object) -> bool:
    if not isinstance(address, tuple):
        return True  # AF_UNIX
    host = str(address[0])
    return host in ("", "localhost", "::1", "0.0.0.0") or host.startswith("127.")


@pytest.fixture
def network_sentinel():  # type: ignore[no-untyped-def]
    """Refuses (and records) any outbound socket connect to a non-loopback
    address for the duration of one test — the same technique ``test_
    notification_guard_spawn.py`` uses across a process boundary, applied
    here in-process. Loopback stays open (nothing here needs it, but SQLite/
    IPC must never be mistaken for the thing being measured)."""
    attempts: list[object] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def connect(self, address):  # type: ignore[no-untyped-def]
        if not _is_local(address):
            attempts.append(address)
            raise OSError("outbound network blocked by the test network sentinel")
        return real_connect(self, address)

    def connect_ex(self, address):  # type: ignore[no-untyped-def]
        if not _is_local(address):
            attempts.append(address)
            return 1
        return real_connect_ex(self, address)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    try:
        yield attempts
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]


# --------------------------------------------------------------- engine harness
@dataclass
class _ScriptedBroker:
    submit_calls: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "scripted-fake"

    def submit(self, intent, quote: Quote) -> Order:  # type: ignore[no-untyped-def]
        self.submit_calls.append(intent.security_id)
        fill = Fill(
            correlation_id=intent.correlation_id, broker_fill_id=f"fake-{intent.correlation_id}",
            strategy_id=intent.strategy_id, execution_mode=intent.execution_mode,
            quantity=intent.quantity, price=quote.last_price, filled_at=datetime.now(UTC),
        )
        return Order(
            correlation_id=intent.correlation_id, strategy_id=intent.strategy_id,
            execution_mode=intent.execution_mode, status=OrderStatus.FILLED,
            updated_at=datetime.now(UTC), filled_quantity=intent.quantity,
            average_fill_price=quote.last_price, fills=(fill,),
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


def _build_engine(
    repository: ExecutionRepository,
) -> tuple[MultiLegEngine, PositionManager, _ScriptedBroker]:
    broker = _ScriptedBroker()
    session = repository.open_session(
        runtime_id=RUNTIME_ID, strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER,
        process_role="worker", pid=1,
    )
    lifecycle = OrderLifecycle(
        repository=repository, broker=broker, runtime_id=RUNTIME_ID, strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER, session_id=session.id,
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
        lots_per_leg=10, entry_time="09:45", stop_new_entries_after="15:10",
        square_off_time="15:15", strike_step=50, otm_distance_points=50,
        roll_trigger_points=60, max_rolls_ce=2, max_rolls_pe=2, single_leg_roll=True,
        combined_stop_per_lot=2000.0,
    )

    def _persist_basket_cb(basket: Basket) -> None:
        _persist_basket_row(repository, basket, runtime_id=RUNTIME_ID)

    def _persist_leg_cb(leg: LegInstance) -> None:
        _persist_leg_row(
            repository, leg, runtime_id=RUNTIME_ID, strategy_id=STRATEGY_ID,
            execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE,
        )

    def _recover() -> Basket | None:
        cfg = _FakeConfig(RUNTIME_ID, STRATEGY_ID, ExecutionMode.PAPER, TRADING_DATE)
        return recover_basket(cfg, repository)  # type: ignore[arg-type]

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
            SimulatedOptionChainResolver("NIFTY", lot_size=75), strike_step=50
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
    engine._start_day()


def _feed(engine: MultiLegEngine, ticks: list[Tick]) -> None:
    for t in ticks:
        engine.on_tick(t)


# ==================================================================== tests
def test_full_lifecycle_with_a_mid_flight_restart_makes_no_network_call(
    tmp_path: Path, network_sentinel: list[object]
) -> None:
    """Entry -> roll -> replacement -> restart -> hard square-off, real
    engine/repository/gateway throughout, ending flat with durable P&L —
    and zero non-loopback socket connects anywhere in the process."""
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
            _tick(NIFTY, 24100.0, _ts(9, 49, 0)),
            _tick(NIFTY, 24100.0, _ts(9, 50, 0)),  # CE roll #1
            _tick(NIFTY, 24100.0, _ts(9, 55, 0)),
            _tick("SIM:NIFTY:WEEKLY:24150:CE", 105.0, _ts(9, 55, 5)),  # replacement fills
        ],
    )
    assert len(positions.positions) == 2

    # Restart mid-lifecycle: dispose of the first engine, build a second one
    # over the same repository.
    engine2, positions2, _broker2 = _build_engine(repository)
    _start(engine2)
    assert len(positions2.positions) == 2  # adopted, not re-entered

    # Hard square-off flattens everything.
    _feed(engine2, [_tick(NIFTY, 24100.0, _ts(15, 15, 0))])
    assert positions2.positions == []

    legs = repository.load_strategy_legs(
        strategy_id=STRATEGY_ID, execution_mode=ExecutionMode.PAPER, trading_date=TRADING_DATE
    )
    assert len(legs) == 3  # original CE, PE, replacement CE
    assert all(leg["state"] == "CLOSED" for leg in legs)
    original_ce = next(leg for leg in legs if leg["leg_id"] == f"{BASKET_ID}:CE:1")
    assert original_ce["realized_gross_pnl"] is not None  # durable, not lost

    rolls = repository.load_basket_rolls(basket_id=BASKET_ID)
    assert len(rolls) == 1
    assert rolls[0]["lifecycle_state"] == "REPLACEMENT_FILLED"

    # Final restart after the confirmed square-off: reconciles cleanly, no
    # exposure recreated.
    engine3, positions3, _broker3 = _build_engine(repository)
    _start(engine3)
    assert positions3.positions == []

    assert network_sentinel == [], f"unexpected outbound connect attempt(s): {network_sentinel}"


def test_strategy_composes_alongside_ema_and_straddle_920_with_no_network_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, network_sentinel: list[object]
) -> None:
    """Real build_supervisor, real committed config, all three intraday_
    options strategies discovered together — this strategy stays isolated
    (Phase 4's own proof) and, separately, produces no network traffic
    while doing so."""
    monkeypatch.setattr(
        runtime_main, "legacy_system_status",
        lambda: LegacySystemStatus(
            launchd_state=LaunchdLabelState.INACTIVE, launchd_detail="stubbed for this test",
            process_running=False, process_detail="stubbed for this test",
        ),
    )
    discovered = {c.strategy.strategy_id for c in discover_strategies(REPO_CONFIG, RUNTIME_ID)}
    assert {STRATEGY_ID, "ema_cross_9_21_buy", "straddle_920"} <= discovered
    enabled = {c.strategy.strategy_id for c in discover_enabled_strategies(REPO_CONFIG, RUNTIME_ID)}
    assert STRATEGY_ID not in enabled  # ships disabled

    tape = REPO_ROOT / "tests" / "fixtures" / "nifty_tick_tape.json"
    adapter = RecordedFeedAdapter(load_tick_tape(tape))
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID, config_root=REPO_CONFIG, paths=load_paths(tmp_path),
        adapter=adapter,
    )
    registrations = {config.strategy_id for config, _channel in supervisor._workers}
    assert registrations == {"ema_cross_9_21_buy", "straddle_920"}  # this strategy stays out

    assert network_sentinel == [], f"unexpected outbound connect attempt(s): {network_sentinel}"


def test_the_committed_configuration_stays_paper_disabled_and_not_live_approved() -> None:
    """A final, cheap belt-and-suspenders safety check for this consolidated
    verification phase — the exact three flags CLAUDE.md requires never to
    move as part of implementation work."""
    text = (REPO_CONFIG / "strategies" / f"{STRATEGY_ID}.yaml").read_text(encoding="utf-8")
    assert "enabled: false" in text
    assert "mode: paper" in text
    assert "live_approved: false" in text
