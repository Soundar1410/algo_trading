"""``dashboards.positional_options`` against a real, migrated database with
one real cycle — the counterpart of
``test_dashboard_positional_and_stocks.py``'s stub-behaviour tests, proving
the page's *other* branch: real data through ``run_bounded``, never SQL
written in the page itself.

The fixture cycle is built through the exact same production path
``tests/integration/test_weekly_delta_neutral_entry.py`` proves —
``runtimes.positional_options.worker.build_engine`` — so the rows this test
renders are the same shape a real paper run would produce, not hand-crafted
INSERTs that could drift from the real schema.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from _dashboard_fakes import FakeStreamlit

import dashboards.positional_options as positional_page
from common.config.models import ExecutionMode
from common.engine.config import SessionConfig
from common.engine.feed import SimulatedFeed
from common.execution import ExecutionRepository
from common.margin import LegMarginRequest, MarginEstimator
from common.models import Tick
from common.persistence import Database, MigrationRunner
from runtimes.positional_options.config_adapter import WorkerConfig
from runtimes.positional_options.worker import build_engine


def _fake_margin_fetcher(_leg: LegMarginRequest) -> float:
    return 20_000.0

IST = ZoneInfo("Asia/Kolkata")
NIFTY_SECURITY_ID = "13"
ENTRY_DATE = "2026-08-19"
EXPIRY_DATE = "2026-08-26"

HEDGE_PUT_STRIKE = 23150.0
SHORT_PUT_STRIKE = 23500.0
SHORT_CALL_STRIKE = 24500.0
HEDGE_CALL_STRIKE = 24850.0

_SECURITY_IDS = {
    (HEDGE_PUT_STRIKE, "PE"): "90001",
    (SHORT_PUT_STRIKE, "PE"): "90002",
    (SHORT_CALL_STRIKE, "CE"): "90003",
    (HEDGE_CALL_STRIKE, "CE"): "90004",
}


def _scrip_master_csv() -> str:
    rows = [
        [
            "SEM_SMST_SECURITY_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL",
            "SEM_CUSTOM_SYMBOL", "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE",
            "SEM_OPTION_TYPE", "SEM_LOT_UNITS", "SEM_EXM_EXCH_ID", "SEM_SEGMENT",
        ]
    ]
    for (strike, option_type), security_id in _SECURITY_IDS.items():
        rows.append(
            [
                security_id, "OPTIDX", f"NIFTY-26AUG2026-{strike:.0f}-{option_type}",
                f"NIFTY {strike:.0f} {option_type}", f"{EXPIRY_DATE} 00:00:00",
                f"{strike:.0f}", option_type, "75", "NSE", "D",
            ]
        )
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue()


def _leg(delta: float, bid: float, ask: float) -> dict[str, Any]:
    return {
        "greeks": {"delta": delta, "gamma": 0.001, "theta": -5.0, "vega": 10.0},
        "implied_volatility": 14.0,
        "last_price": (bid + ask) / 2.0,
        "oi": 500_000,
        "top_bid_price": bid,
        "top_ask_price": ask,
        "volume": 25_000,
    }


def _chain_fetcher(_security_id: int, _segment: str, _expiry: str) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "last_price": 24000.0,
            "oc": {
                f"{HEDGE_PUT_STRIKE:.6f}": {"pe": _leg(-0.06, 18.0, 20.0)},
                f"{SHORT_PUT_STRIKE:.6f}": {"pe": _leg(-0.20, 80.0, 82.0)},
                f"{SHORT_CALL_STRIKE:.6f}": {"ce": _leg(0.20, 78.0, 80.0)},
                f"{HEDGE_CALL_STRIKE:.6f}": {"ce": _leg(0.06, 17.0, 19.0)},
            },
        },
    }


def _ts(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 19, hour, minute, second, tzinfo=IST)


def _underlying_tick(ts: datetime, price: float = 24000.0) -> Tick:
    return Tick(
        security_id=NIFTY_SECURITY_ID, instrument="NIFTY", last_price=price,
        exchange_time=ts, received_at=ts,
    )


def _leg_tick(security_id: str, bid: float, ask: float, ts: datetime) -> Tick:
    return Tick(
        security_id=security_id, instrument=security_id, last_price=(bid + ask) / 2.0,
        exchange_time=ts, received_at=ts, bid_price=bid, ask_price=ask,
    )


def _build_worker_config(strategy_id: str = "weekly_delta_neutral") -> WorkerConfig:
    return WorkerConfig(
        runtime_id="positional_options",
        strategy_id=strategy_id,
        strategy_ref=(
            "strategies.positional_options.weekly_delta_neutral.strategy:"
            "WeeklyDeltaNeutralStrategy"
        ),
        trading_date=ENTRY_DATE,
        lots=1,
        timezone="Asia/Kolkata",
        underlying_security_id=NIFTY_SECURITY_ID,
        underlying_instrument="NIFTY",
        underlying_segment="IDX_I",
        option_segment="NSE_FNO",
        session=SessionConfig(
            timezone="Asia/Kolkata", start_time="09:25", end_time="09:40",
            square_off_time="15:15", holidays=(),
        ),
        risk_free_rate=0.065,
        dividend_yield=0.0,
        quote_max_age_seconds=30.0,
        evaluation_interval_seconds=0.0,
        max_adjustments_per_day=1,
        max_adjustments_per_cycle=3,
        min_minutes_between_adjustments=90,
        parameters={
            "underlying": "NIFTY",
            "index_security_id": NIFTY_SECURITY_ID,
            "index_segment": "IDX_I",
            "fno_segment": "NSE_FNO",
        },
    )


def _build_fixture_database(
    db_path: Path, *, strategy_id: str = "weekly_delta_neutral", pid: int = 1234
) -> None:
    from common.market_data.scrip_master import ScripMaster

    database = Database(db_path)
    MigrationRunner(database).run_pending()
    repository = ExecutionRepository(database)
    try:
        config = _build_worker_config(strategy_id)
        session = repository.open_session(
            runtime_id=config.runtime_id, strategy_id=config.strategy_id,
            execution_mode=ExecutionMode.PAPER, process_role="worker", pid=pid,
        )
        entry_ts = _ts(9, 26, 0)
        ticks = [
            _leg_tick(_SECURITY_IDS[(HEDGE_PUT_STRIKE, "PE")], 18.0, 20.0, entry_ts),
            _leg_tick(_SECURITY_IDS[(HEDGE_CALL_STRIKE, "CE")], 17.0, 19.0, entry_ts),
            _leg_tick(_SECURITY_IDS[(SHORT_PUT_STRIKE, "PE")], 80.0, 82.0, entry_ts),
            _leg_tick(_SECURITY_IDS[(SHORT_CALL_STRIKE, "CE")], 78.0, 80.0, entry_ts),
            _underlying_tick(entry_ts),
        ]
        feed = SimulatedFeed(ticks)
        scrip_master = ScripMaster("NIFTY", exchange="NSE").load_from_text(_scrip_master_csv())
        built = build_engine(
            config, repository=repository, session_id=session.id, feed=feed,
            chain_fetcher=_chain_fetcher, scrip_master=scrip_master,
            margin_estimator=MarginEstimator(margin_fetcher=_fake_margin_fetcher),
            clock=lambda: entry_ts,
        )
        built.engine.run()
    finally:
        database.close()


def test_positional_options_page_shows_real_cycle_and_leg_data(tmp_path: Path) -> None:
    db_path = tmp_path / "positional_options.db"
    _build_fixture_database(db_path)

    st = FakeStreamlit()
    st.selectbox_returns = {"po_strategy": "weekly_delta_neutral"}
    positional_page.render(st, config_root=None, database_path=db_path)

    assert st.tab_labels == [
        [
            "Overview", "Active Cycles", "Legs", "Adjustments",
            "Orders & Fills", "History", "Performance", "Health",
        ]
    ]
    assert not any(positional_page.NOT_CONFIGURED in w for w in st.warnings)

    # Overview: a metric row for the one open (ACTIVE) cycle.
    assert any(label == "State" for label, _value in st.metrics)

    # Legs: a real dataframe with all four legs, never a fabricated one.
    leg_tables = [rows for rows in st.dataframes if rows and "Role" in rows[0]]
    assert len(leg_tables) == 1
    roles = {row["Role"] for row in leg_tables[0]}
    assert roles == {"HEDGE_PUT", "HEDGE_CALL", "SHORT_PUT", "SHORT_CALL"}
    assert all(row["State"] == "OPEN" for row in leg_tables[0])


def test_positional_options_page_history_and_performance_are_empty_for_an_open_cycle(
    tmp_path: Path,
) -> None:
    """The cycle just entered is still ACTIVE — History/Performance must
    show nothing fabricated for it, only real completed cycles."""
    db_path = tmp_path / "positional_options.db"
    _build_fixture_database(db_path)

    st = FakeStreamlit()
    st.selectbox_returns = {"po_strategy": "weekly_delta_neutral"}
    positional_page.render(st, config_root=None, database_path=db_path)

    assert any("No completed cycle history" in text for text in st.infos)
    assert any("No completed cycle to compute performance" in text for text in st.infos)


def test_positional_dashboard_data_filters_two_strategies_independently(tmp_path: Path) -> None:
    """Phase 5 (runtime generalization): dashboards/data/positional.py's own
    module docstring already claims "not specific to weekly_delta_neutral
    ... any future positional strategy reuses every function here
    unchanged" — proven here, not merely asserted, with two real cycles
    (from two independently-run engines, real entries, same database) under
    two different strategy_ids: each strategy's own rows must be visible
    only under its own id, never the other's."""
    import sqlite3

    from dashboards.data.positional import load_cycles, load_legs_for_cycle, load_open_cycle

    db_path = tmp_path / "positional_options.db"
    _build_fixture_database(db_path, strategy_id="weekly_delta_neutral", pid=1234)
    _build_fixture_database(db_path, strategy_id="dashboard_fixture_two", pid=5678)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cycles_a = load_cycles(conn, strategy_id="weekly_delta_neutral", execution_mode="paper")
        cycles_b = load_cycles(conn, strategy_id="dashboard_fixture_two", execution_mode="paper")
        assert len(cycles_a) == 1 and len(cycles_b) == 1
        assert cycles_a[0].cycle_id != cycles_b[0].cycle_id
        assert cycles_a[0].cycle_id.startswith("weekly_delta_neutral:")
        assert cycles_b[0].cycle_id.startswith("dashboard_fixture_two:")

        open_a = load_open_cycle(conn, strategy_id="weekly_delta_neutral", execution_mode="paper")
        open_b = load_open_cycle(
            conn, strategy_id="dashboard_fixture_two", execution_mode="paper"
        )
        assert open_a is not None and open_b is not None
        assert open_a.cycle_id == cycles_a[0].cycle_id
        assert open_b.cycle_id == cycles_b[0].cycle_id

        # A strategy_id with no rows at all gets nothing fabricated — never
        # falls back to "the one cycle that happens to exist".
        assert load_cycles(conn, strategy_id="not_a_real_strategy", execution_mode="paper") == ()
        assert (
            load_open_cycle(conn, strategy_id="not_a_real_strategy", execution_mode="paper")
            is None
        )

        legs_a = load_legs_for_cycle(conn, cycle_id=cycles_a[0].cycle_id)
        legs_b = load_legs_for_cycle(conn, cycle_id=cycles_b[0].cycle_id)
        assert len(legs_a) == 4 and len(legs_b) == 4
        assert {leg.leg_id for leg in legs_a}.isdisjoint({leg.leg_id for leg in legs_b})
    finally:
        conn.close()

    # And through the real page, not just the data layer: selecting one
    # strategy in the dropdown renders cleanly and never falls back to the
    # "not configured" message a cross-strategy leak or an empty result
    # would otherwise produce.
    st = FakeStreamlit()
    st.selectbox_returns = {"po_strategy": "dashboard_fixture_two"}
    positional_page.render(st, config_root=None, database_path=db_path)
    assert not any(positional_page.NOT_CONFIGURED in w for w in st.warnings)
    leg_tables = [rows for rows in st.dataframes if rows and "Role" in rows[0]]
    assert len(leg_tables) == 1
    assert any(label == "State" for label, _value in st.metrics)
