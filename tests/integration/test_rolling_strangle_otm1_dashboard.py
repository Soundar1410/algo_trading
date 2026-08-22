"""Phase 5B: read-only dashboard integration for ``rolling_strangle_otm1``.

No dashboard page gained a strategy-name branch — ``dashboards/intraday_
options.py`` and ``dashboards/data/multi_leg.py`` already discover and scope
strategies generically (by ``strategy_id`` as an ordinary filter parameter,
and by reading ``config/strategies/*.yaml``). This file proves that against
**real** data: a real roll history is produced through the same
``MultiLegEngine``/``ExecutionRepository``/``RollLedger`` stack Phases 2-4
already use (never a hand-typed row for the roll-specific tables), and the
generic read models — including the new ``RollRow``/``RollAnchorRow``
(migration ``0013``'s ``strategy_basket_rolls``/``strategy_basket_roll_
anchor``, first needed by this strategy but usable by any multi-leg
strategy) — are proven to scope correctly, render honestly, and never
diverge from what a second, unrelated strategy's own data shows in the same
database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from streamlit.testing.v1 import AppTest

from common.broker.base import Quote
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
from common.models import Fill, Order, OrderIntent, OrderStatus, Tick
from common.persistence import Database, MigrationRunner, connect_readonly, migrate
from dashboards.data.multi_leg import (
    load_baskets,
    load_legs_for_basket,
    load_roll_anchor,
    load_rolls_for_basket,
)
from dashboards.data.strategy_scope import DISABLED, discover_strategy_options
from runtimes.intraday_options.multi_leg_engine_worker import recover_basket
from strategies.intraday_options.rolling_strangle_otm1.strategy import RollingStrangleOtm1Strategy

IST = ZoneInfo("Asia/Kolkata")
RUNTIME_ID = "intraday_options"
STRATEGY_ID = "rolling_strangle_otm1"
TRADING_DATE = "2026-08-17"
NIFTY = "NIFTY_IDX"
BASKET_ID = f"{STRATEGY_ID}:{TRADING_DATE}"
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_CONFIG = REPO_ROOT / "config"
DASHBOARDS_DIR = REPO_ROOT / "dashboards"


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 17, hour, minute, second, tzinfo=IST)


def _tick(security_id: str, price: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id, instrument=security_id, last_price=price,
        exchange_time=ts, received_at=ts,
    )


@dataclass
class _ScriptedBroker:
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
    repository: ExecutionRepository, *, trading_date: str = TRADING_DATE
) -> tuple[MultiLegEngine, PositionManager]:
    """``trading_date`` is the *persisted basket label* only — deliberately
    independent of the tick timestamps' own calendar date (always the fixed,
    known-Monday ``_ts()`` dates below): ``MarketSession.is_open`` validates
    a tick's own weekday/holiday, and only a real trading day may be used
    there, whereas ``Basket.trading_date`` is pure bookkeeping identity the
    dashboard's own basket lookup filters by. Decoupling the two lets a
    fixture label its basket "today" (what the dashboard's Baskets tab
    always shows, spec section 15) without making engine behaviour depend on
    which real calendar day the test suite happens to run on."""
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
        trading_date=trading_date, repository=repository, runtime_id=RUNTIME_ID,
    )
    positions = PositionManager(gateway, lots=10)
    roll_ledger = RollLedger(
        repository, runtime_id=RUNTIME_ID, strategy_id=STRATEGY_ID,
        execution_mode=ExecutionMode.PAPER, trading_date=trading_date,
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
            execution_mode=ExecutionMode.PAPER, trading_date=trading_date,
        )

    def _recover() -> Basket | None:
        cfg = _FakeConfig(RUNTIME_ID, STRATEGY_ID, ExecutionMode.PAPER, trading_date)
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
        trading_date=trading_date,
        persist_basket=_persist_basket_cb,
        persist_leg=_persist_leg_cb,
        recover_basket=_recover,
        roll_ledger=roll_ledger,
    )
    return engine, positions


def _write_a_real_roll_history(db_path: Path, *, trading_date: str = TRADING_DATE) -> None:
    """Entry, one confirmed CE roll, and its replacement — through the real
    strategy-driven engine, not a hand-typed row."""
    repository = _repository(db_path)
    engine, _positions = _build_engine(repository, trading_date=trading_date)
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 41)),
        _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
        _tick("SIM:NIFTY:WEEKLY:24050:CE", 100.0, _ts(9, 45, 5)),
        _tick("SIM:NIFTY:WEEKLY:23950:PE", 95.0, _ts(9, 45, 10)),
        _tick(NIFTY, 24100.0, _ts(9, 49, 0)),
        _tick(NIFTY, 24100.0, _ts(9, 50, 0)),  # CE roll #1 claimed and confirmed closed
        _tick(NIFTY, 24100.0, _ts(9, 55, 0)),  # replacement re-enters
        _tick("SIM:NIFTY:WEEKLY:24150:CE", 105.0, _ts(9, 55, 5)),
    ]
    engine.feed = SimulatedFeed(ticks)
    engine.run()


@pytest.fixture
def rolling_strangle_otm1_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "intraday_options.db"
    _write_a_real_roll_history(db_path)
    return db_path


# ============================================================= read models
def test_committed_config_lists_rolling_strangle_otm1_as_disabled() -> None:
    """Generic discovery — no code change needed for a new strategy to
    appear here, since Phase 4's YAML alone is what this reads."""
    options = discover_strategy_options(None, REPO_CONFIG, RUNTIME_ID)
    by_id = {o.strategy_id: o for o in options}
    assert STRATEGY_ID in by_id
    assert by_id[STRATEGY_ID].status_label == DISABLED


def test_roll_history_reads_back_correctly_from_a_read_only_connection(
    rolling_strangle_otm1_db: Path,
) -> None:
    conn = connect_readonly(rolling_strangle_otm1_db)
    try:
        baskets = load_baskets(
            conn, strategy_id=STRATEGY_ID, execution_mode="paper", trading_date=TRADING_DATE
        )
        assert len(baskets) == 1
        legs = load_legs_for_basket(conn, basket_id=BASKET_ID)
        assert {leg.leg_role for leg in legs} == {"CE", "PE"}
        assert len({leg.leg_id for leg in legs}) == 3  # original CE, PE, replacement CE

        rolls = load_rolls_for_basket(conn, basket_id=BASKET_ID)
        assert len(rolls) == 1
        roll = rolls[0]
        assert roll.leg_role == "CE"
        assert roll.roll_sequence == 1
        assert roll.lifecycle_state == "REPLACEMENT_FILLED"
        assert roll.target_strike == 24050.0
        assert roll.replacement_leg_id is not None
        assert roll.replacement_strike == 24150.0
        assert roll.reference_price_at_claim == 24100.0
        assert roll.close_correlation_id is not None

        anchor = load_roll_anchor(conn, basket_id=BASKET_ID)
        assert anchor is not None
        assert anchor.reference_price == 24100.0  # re-anchored at the roll claim
    finally:
        conn.close()


def test_a_role_still_awaiting_replacement_renders_missing_fields_as_none(
    tmp_path: Path,
) -> None:
    """Honest missing-data: a roll not yet replaced has no replacement leg
    or strike — the read model must return None, never fabricate one."""
    db_path = tmp_path / "intraday_options.db"
    repository = _repository(db_path)
    engine, _positions = _build_engine(repository)
    ticks = [
        _tick(NIFTY, 24000.0, _ts(9, 41)),
        _tick(NIFTY, 24000.0, _ts(9, 45, 0)),
        _tick("SIM:NIFTY:WEEKLY:24050:CE", 100.0, _ts(9, 45, 5)),
        _tick("SIM:NIFTY:WEEKLY:23950:PE", 95.0, _ts(9, 45, 10)),
        _tick(NIFTY, 24100.0, _ts(9, 49, 0)),
        _tick(NIFTY, 24100.0, _ts(9, 50, 0)),  # CE roll claimed and confirmed -> AWAITING
    ]
    engine.feed = SimulatedFeed(ticks)
    engine.run()

    conn = connect_readonly(db_path)
    try:
        rolls = load_rolls_for_basket(conn, basket_id=BASKET_ID)
        assert len(rolls) == 1
        assert rolls[0].lifecycle_state == "AWAITING_NEXT_CANDLE"
        assert rolls[0].replacement_leg_id is None
        assert rolls[0].replacement_strike is None
        assert rolls[0].replacement_symbol is None
    finally:
        conn.close()


def test_read_only_connection_refuses_a_write(rolling_strangle_otm1_db: Path) -> None:
    """Structural proof, not a convention: connect_readonly opens SQLite's
    own mode=ro URI, so the driver itself refuses a write."""
    import sqlite3

    conn = connect_readonly(rolling_strangle_otm1_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE strategy_baskets SET lifecycle_state = 'TAMPERED'")
    finally:
        conn.close()


# ================================================== multi-strategy scoping
def _write_a_minimal_straddle_920_basket(db_path: Path) -> str:
    """A second, unrelated strategy's basket in the same database, through
    the real repository API — proves rolling_strangle_otm1's own read
    models never leak another strategy's rows into their result."""
    repository = _repository(db_path)
    other_basket_id = f"straddle_920:{TRADING_DATE}"
    repository.upsert_strategy_basket(
        runtime_id=RUNTIME_ID, strategy_id="straddle_920", execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE, basket_id=other_basket_id, lifecycle_state="OPEN",
        entries_consumed=True, day_blocked_reason=None, adjustment_count=0,
        pending_replacement_role=None, pending_replacement_state=None,
        original_combined_basis=195.0, square_off_state="PENDING",
    )
    repository.upsert_strategy_leg(
        runtime_id=RUNTIME_ID, strategy_id="straddle_920", execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE, basket_id=other_basket_id, leg_id=f"{other_basket_id}:CE:1",
        leg_role="CE", leg_sequence=1, is_replacement=False, replaces_leg_id=None,
        security_id="SIM:24000:CE", symbol="NIFTY 24000 CE", strike=24000.0,
        expiry="2026-08-21", lot_size=75, side="SELL", quantity=750, entry_price=100.0,
        entry_time=f"{TRADING_DATE}T09:21:00", entry_correlation_id="corr_ce_entry",
        exit_price=None, exit_time=None, exit_reason=None, exit_correlation_id=None,
        realized_gross_pnl=None, state="OPEN",
    )
    return other_basket_id


def test_selecting_one_strategy_never_shows_the_others_basket(
    rolling_strangle_otm1_db: Path,
) -> None:
    other_basket_id = _write_a_minimal_straddle_920_basket(rolling_strangle_otm1_db)
    conn = connect_readonly(rolling_strangle_otm1_db)
    try:
        rolling_baskets = load_baskets(
            conn, strategy_id=STRATEGY_ID, execution_mode="paper", trading_date=TRADING_DATE
        )
        assert {b.basket_id for b in rolling_baskets} == {BASKET_ID}

        straddle_baskets = load_baskets(
            conn, strategy_id="straddle_920", execution_mode="paper", trading_date=TRADING_DATE
        )
        assert {b.basket_id for b in straddle_baskets} == {other_basket_id}

        # And the roll ledger is strategy-agnostic per-basket only: the
        # other strategy's basket (which never rolled) has no roll rows,
        # and rolling_strangle_otm1's own rolls never appear under it.
        assert load_rolls_for_basket(conn, basket_id=other_basket_id) == ()
        assert len(load_rolls_for_basket(conn, basket_id=BASKET_ID)) == 1
    finally:
        conn.close()


# ==================================================================== AppTest
def _write_config(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def project_root_with_a_rolling_strangle_otm1_basket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    _write_config(
        tmp_path / "config" / "global.yaml",
        "global:\n  live_trading_enabled: false\n  timezone: Asia/Kolkata\n"
        "runtime_defaults:\n  enabled: false\n  live_execution_allowed: false\n"
        "strategy_defaults:\n  enabled: false\n  mode: paper\n  live_approved: false\n",
    )
    _write_config(
        tmp_path / "config" / "runtimes" / "intraday_options.yaml",
        "runtime_id: intraday_options\nenabled: true\nlive_execution_allowed: false\n",
    )
    _write_config(
        tmp_path / "config" / "strategies" / "rolling_strangle_otm1.yaml",
        "strategy_id: rolling_strangle_otm1\nruntime_id: intraday_options\n"
        "enabled: false\nmode: paper\nlive_approved: false\nengine: multi_leg_engine\n",
    )
    db_path = tmp_path / "data" / "operational" / "intraday_options.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    MigrationRunner(Database(db_path)).run_pending()
    # The Baskets tab always loads strictly today's basket (dashboards/
    # intraday_options.py's own `trading_date = date.today().isoformat()`,
    # unconditional on the "Date range" preset) — see _build_engine's own
    # docstring for why this differs from the tick timestamps' calendar date.
    from datetime import date as _date

    _write_a_real_roll_history(db_path, trading_date=_date.today().isoformat())
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    return tmp_path


def test_the_baskets_tab_renders_this_strategys_real_roll_history(
    project_root_with_a_rolling_strangle_otm1_basket: Path,
) -> None:
    """The real Streamlit runtime, real page, real data — the strongest
    proof available that no strategy-specific branch was needed for this
    to work: the page code is byte-identical to what straddle_920 already
    exercises in test_dashboard_apptest.py."""
    at = AppTest.from_file(str(DASHBOARDS_DIR / "intraday_options.py"), default_timeout=30)
    at.run()
    assert list(at.exception) == []

    strategy_box = at.selectbox(key="io_strategy")
    label = next(opt for opt in strategy_box.options if STRATEGY_ID in opt)
    assert "Disabled" in label
    strategy_box.select(label).run()
    assert list(at.exception) == []

    baskets_tab = at.tabs[2]
    tables = baskets_tab.get("dataframe")
    assert tables, "the Baskets tab rendered no table for a real basket/roll fixture"
    all_rows = [row for table in tables for row in table.value.to_dict("records")]
    roll_rows = [row for row in all_rows if "Roll #" in row]
    assert roll_rows, "no roll-history row rendered for a basket with a real confirmed roll"
    assert roll_rows[0]["State"] == "REPLACEMENT_FILLED"
    assert roll_rows[0]["Role"] == "CE"
