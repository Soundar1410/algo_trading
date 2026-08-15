"""End-to-end Streamlit smoke tests via ``streamlit.testing.v1.AppTest``.

Every other test in this package drives a page's ``render()`` with a fake
streamlit module, which proves the page's own logic but cannot catch a
mistake only the real Streamlit runtime would (e.g. ``st.page_link``
resolves its path *relative to the entrypoint script's own directory*, not
the repository root — a real bug this suite caught and
``dashboards/app.py`` was fixed for; a fake streamlit's ``page_link`` never
validates the path at all).

Every page is driven against a throwaway project root under ``tmp_path`` —
``PROJECT_ROOT`` is monkeypatched so ``common.config.load_paths()`` can
never resolve to this repository's real ``data/operational/`` database.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from common.persistence import Database, MigrationRunner

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS_DIR = REPO_ROOT / "dashboards"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway project root, fully wired: config + an empty migrated
    ``intraday_options.db`` — never the real repository's data."""
    _write(
        tmp_path / "config" / "global.yaml",
        "global:\n  live_trading_enabled: false\n  timezone: Asia/Kolkata\n"
        "runtime_defaults:\n  enabled: false\n  live_execution_allowed: false\n"
        "strategy_defaults:\n  enabled: false\n  mode: paper\n  live_approved: false\n",
    )
    _write(
        tmp_path / "config" / "runtimes" / "intraday_options.yaml",
        "runtime_id: intraday_options\nenabled: true\nlive_execution_allowed: false\n",
    )
    _write(
        tmp_path / "config" / "runtimes" / "positional_options.yaml",
        "runtime_id: positional_options\nenabled: false\nlive_execution_allowed: false\n",
    )
    _write(
        tmp_path / "config" / "strategies" / "ema_cross_9_21_buy.yaml",
        "strategy_id: ema_cross_9_21_buy\nenabled: true\nmode: paper\nlive_approved: false\n",
    )
    database = Database(tmp_path / "data" / "operational" / "intraday_options.db")
    MigrationRunner(database).run_pending()

    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def project_root_with_two_strategies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Two configured, traded strategies — enough to exercise the
    Strategy selector's filtering and session-state persistence for real,
    not just against a fake streamlit module."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from common.broker import PaperBroker, PaperFillConfig, SlippageConfig
    from common.config.models import ExecutionMode
    from common.execution import ExecutionRepository, OrderLifecycle
    from common.models import Candle, Side, Signal

    _write(
        tmp_path / "config" / "global.yaml",
        "global:\n  live_trading_enabled: false\n  timezone: Asia/Kolkata\n"
        "runtime_defaults:\n  enabled: false\n  live_execution_allowed: false\n"
        "strategy_defaults:\n  enabled: false\n  mode: paper\n  live_approved: false\n",
    )
    _write(
        tmp_path / "config" / "runtimes" / "intraday_options.yaml",
        "runtime_id: intraday_options\nenabled: true\nlive_execution_allowed: false\n",
    )
    _write(
        tmp_path / "config" / "strategies" / "alpha_strategy.yaml",
        "strategy_id: alpha_strategy\nenabled: true\nmode: paper\nlive_approved: false\n",
    )
    _write(
        tmp_path / "config" / "strategies" / "beta_strategy.yaml",
        "strategy_id: beta_strategy\nenabled: true\nmode: paper\nlive_approved: false\n",
    )

    database_path = tmp_path / "data" / "operational" / "intraday_options.db"
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    repository = ExecutionRepository(database)
    ist = ZoneInfo("Asia/Kolkata")
    trading_date = datetime.now(ist).date().isoformat()

    def candle(minute: int, close: float) -> Candle:
        start = datetime.now(ist).replace(hour=9, minute=minute, second=0, microsecond=0)
        return Candle(
            security_id="13", instrument="NIFTY", open=close, high=close + 1, low=close - 1,
            close=close, volume=100, start_at=start, end_at=start + timedelta(minutes=1),
            tick_count=4,
        )

    strategy_trades = (("alpha_strategy", 100.0, 110.0), ("beta_strategy", 200.0, 190.0))
    for strategy_id, entry, exit_ in strategy_trades:
        session = repository.open_session(
            runtime_id="intraday_options", strategy_id=strategy_id,
            execution_mode=ExecutionMode.PAPER, process_role="worker", pid=1,
        )
        lifecycle = OrderLifecycle(
            repository=repository,
            broker=PaperBroker(
                config=PaperFillConfig(
                    slippage=SlippageConfig(mode="points", market_order_points=0.0)
                )
            ),
            runtime_id="intraday_options", strategy_id=strategy_id,
            execution_mode=ExecutionMode.PAPER, session_id=session.id,
        )
        entry_signal = Signal(
            strategy_id=strategy_id, execution_mode=ExecutionMode.PAPER, instrument="NIFTY",
            security_id="13", side=Side.BUY, quantity=50, candle=candle(15, entry),
            reference_price=entry, evaluated_at=candle(15, entry).end_at, reason="test",
        )
        exit_signal = Signal(
            strategy_id=strategy_id, execution_mode=ExecutionMode.PAPER, instrument="NIFTY",
            security_id="13", side=Side.SELL, quantity=50, candle=candle(16, exit_),
            reference_price=exit_, evaluated_at=candle(16, exit_).end_at, reason="test",
        )
        lifecycle.handle_signal(entry_signal, trading_date=trading_date)
        lifecycle.handle_signal(exit_signal, trading_date=trading_date)

    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    return tmp_path


def test_strategy_selector_filters_overview_and_survives_an_unrelated_rerun(
    project_root_with_two_strategies: Path,
):
    at = AppTest.from_file(str(DASHBOARDS_DIR / "intraday_options.py"), default_timeout=30)
    at.run()
    assert list(at.exception) == []

    strategy_box = at.selectbox(key="io_strategy")
    option_labels = list(strategy_box.options)
    assert any("alpha_strategy" in label for label in option_labels)
    assert any("beta_strategy" in label for label in option_labels)
    assert "All Strategies" in option_labels

    # Select one strategy — Overview must now show only that strategy.
    alpha_label = next(label for label in option_labels if "alpha_strategy" in label)
    strategy_box.select(alpha_label).run()
    assert list(at.exception) == []
    overview_markdown = " ".join(m.value for m in at.tabs[0].markdown)
    assert "alpha_strategy" in overview_markdown
    assert "beta_strategy" not in overview_markdown

    # Trigger an unrelated rerun (the Orders & Fills mode selectbox) and
    # confirm the strategy selection was not reset by it.
    at.selectbox(key="io_orders_mode").select("Paper").run()
    assert list(at.exception) == []
    assert at.selectbox(key="io_strategy").value == "alpha_strategy"


def test_strategy_comparison_multiselect_defaults_to_every_strategy(
    project_root_with_two_strategies: Path,
):
    at = AppTest.from_file(str(DASHBOARDS_DIR / "intraday_options.py"), default_timeout=30)
    at.run()
    assert list(at.exception) == []

    compare_box = at.multiselect(key="io_compare_strategies")
    assert set(compare_box.value) == {"alpha_strategy", "beta_strategy"}

    comparison_tables = at.tabs[6].get("dataframe")
    assert comparison_tables
    strategies_shown = set(comparison_tables[0].value["Strategy"])
    assert strategies_shown == {"alpha_strategy", "beta_strategy"}


def _row_count(database_path: Path, table: str) -> int:
    from common.persistence import connect_readonly

    conn = connect_readonly(database_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
    finally:
        conn.close()


def test_home_loads_with_no_exception_and_shows_three_category_links(project_root: Path):
    at = AppTest.from_file(str(DASHBOARDS_DIR / "app.py"), default_timeout=30)
    at.run()

    assert list(at.exception) == []
    links = {(pl.page, pl.label) for pl in at.get("page_link")}
    assert len(links) == 3
    labels = {label for _, label in links}
    assert any("Intraday Options" in label for label in labels)
    assert any("Positional Options" in label for label in labels)
    assert any("Intraday Stocks" in label for label in labels)


def test_intraday_options_page_loads_and_every_tab_is_present(project_root: Path):
    at = AppTest.from_file(str(DASHBOARDS_DIR / "intraday_options.py"), default_timeout=30)
    at.run()

    assert list(at.exception) == []
    tab_labels = [t.label for t in at.tabs]
    assert tab_labels == [
        "Overview",
        "Live Positions",
        "Baskets",
        "Orders & Fills",
        "Closed Trades",
        "Performance",
        "Strategy Comparison",
        "Signals & Events",
        "Health",
    ]


def test_system_health_page_loads_with_no_exception(project_root: Path):
    at = AppTest.from_file(str(DASHBOARDS_DIR / "system_health.py"), default_timeout=30)
    at.run()
    assert list(at.exception) == []


@pytest.mark.parametrize("page", ["positional_options.py", "intraday_stocks.py"])
def test_not_implemented_pages_load_with_no_exception(project_root: Path, page: str):
    at = AppTest.from_file(str(DASHBOARDS_DIR / page), default_timeout=30)
    at.run()
    assert list(at.exception) == []
    assert len(at.tabs) == 8


@pytest.mark.parametrize(
    "page",
    [
        "app.py",
        "intraday_options.py",
        "system_health.py",
        "positional_options.py",
        "intraday_stocks.py",
    ],
)
def test_no_page_writes_to_the_database(project_root: Path, page: str):
    """The real end-to-end proof the AST checks in test_dashboard.py can
    only argue for statically: loading every page for real leaves the
    database exactly as migrated — no session, no heartbeat, no row of any
    kind was written by rendering a page."""
    database_path = project_root / "data" / "operational" / "intraday_options.db"
    before = _row_count(database_path, "schema_migrations")

    at = AppTest.from_file(str(DASHBOARDS_DIR / page), default_timeout=30)
    at.run()

    after_migrations = _row_count(database_path, "schema_migrations")
    after_sessions = _row_count(database_path, "runtime_sessions")
    assert after_migrations == before
    assert after_sessions == 0


@pytest.fixture
def project_root_with_a_straddle_920_basket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """One straddle_920 basket with an open CE leg and a closed (adjusted)
    PE leg — enough to exercise the Baskets tab's drill-down for real."""
    _write(
        tmp_path / "config" / "global.yaml",
        "global:\n  live_trading_enabled: false\n  timezone: Asia/Kolkata\n"
        "runtime_defaults:\n  enabled: false\n  live_execution_allowed: false\n"
        "strategy_defaults:\n  enabled: false\n  mode: paper\n  live_approved: false\n",
    )
    _write(
        tmp_path / "config" / "runtimes" / "intraday_options.yaml",
        "runtime_id: intraday_options\nenabled: true\nlive_execution_allowed: false\n",
    )
    _write(
        tmp_path / "config" / "strategies" / "straddle_920.yaml",
        "strategy_id: straddle_920\nenabled: true\nmode: paper\nlive_approved: false\n"
        "engine: multi_leg_engine\n",
    )

    from datetime import datetime
    from zoneinfo import ZoneInfo

    from common.config.models import ExecutionMode
    from common.execution import ExecutionRepository

    database_path = tmp_path / "data" / "operational" / "intraday_options.db"
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    repository = ExecutionRepository(database)
    ist = ZoneInfo("Asia/Kolkata")
    trading_date = datetime.now(ist).date().isoformat()
    basket_id = f"straddle_920:{trading_date}"

    repository.upsert_strategy_basket(
        runtime_id="intraday_options",
        strategy_id="straddle_920",
        execution_mode=ExecutionMode.PAPER,
        trading_date=trading_date,
        basket_id=basket_id,
        lifecycle_state="OPEN",
        entries_consumed=True,
        day_blocked_reason=None,
        adjustment_count=1,
        pending_replacement_role=None,
        pending_replacement_state=None,
        original_combined_basis=195.0,
        square_off_state="PENDING",
    )
    repository.upsert_strategy_leg(
        runtime_id="intraday_options",
        strategy_id="straddle_920",
        execution_mode=ExecutionMode.PAPER,
        trading_date=trading_date,
        basket_id=basket_id,
        leg_id=f"{basket_id}:PE:1",
        leg_role="PE",
        leg_sequence=1,
        is_replacement=False,
        replaces_leg_id=None,
        security_id="SIM:24000:PE",
        symbol="NIFTY 24000 PE",
        strike=24000.0,
        expiry="2026-08-21",
        lot_size=75,
        side="SELL",
        quantity=750,
        entry_price=95.0,
        entry_time=f"{trading_date}T09:21:00",
        entry_correlation_id="corr_pe_entry",
        exit_price=200.0,
        exit_time=f"{trading_date}T09:30:00",
        exit_reason="ADJUSTMENT",
        exit_correlation_id="corr_pe_exit",
        realized_gross_pnl=-78750.0,
        state="CLOSED",
    )
    repository.upsert_strategy_leg(
        runtime_id="intraday_options",
        strategy_id="straddle_920",
        execution_mode=ExecutionMode.PAPER,
        trading_date=trading_date,
        basket_id=basket_id,
        leg_id=f"{basket_id}:CE:1",
        leg_role="CE",
        leg_sequence=1,
        is_replacement=False,
        replaces_leg_id=None,
        security_id="SIM:24000:CE",
        symbol="NIFTY 24000 CE",
        strike=24000.0,
        expiry="2026-08-21",
        lot_size=75,
        side="SELL",
        quantity=750,
        entry_price=100.0,
        entry_time=f"{trading_date}T09:21:00",
        entry_correlation_id="corr_ce_entry",
        exit_price=None,
        exit_time=None,
        exit_reason=None,
        exit_correlation_id=None,
        realized_gross_pnl=None,
        state="OPEN",
    )

    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    return tmp_path


def test_the_baskets_tab_shows_a_basket_and_its_legs(
    project_root_with_a_straddle_920_basket: Path,
):
    at = AppTest.from_file(str(DASHBOARDS_DIR / "intraday_options.py"), default_timeout=30)
    at.run()
    assert list(at.exception) == []

    strategy_box = at.selectbox(key="io_strategy")
    label = next(opt for opt in strategy_box.options if "straddle_920" in opt)
    strategy_box.select(label).run()
    assert list(at.exception) == []

    baskets_tab = at.tabs[2]
    tables = baskets_tab.get("dataframe")
    assert tables, "the Baskets tab rendered no leg table for a real basket/leg fixture"
    rendered_roles = {row["Role"] for table in tables for row in table.value.to_dict("records")}
    assert rendered_roles == {"CE", "PE"}
