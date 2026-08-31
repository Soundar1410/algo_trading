"""``dashboards/app.py`` (Home): market status, multi-runtime aggregation,
category cards, and paper/live P&L separation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from _dashboard_fakes import FakeStreamlit

import dashboards.app as home_page
from common.config.models import ExecutionMode
from common.execution import ExecutionRepository
from common.persistence import Database, MigrationRunner

IST = ZoneInfo("Asia/Kolkata")
TRADING_DATE = "2026-08-14"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_config(config_root: Path, *, positional_enabled: bool = False) -> None:
    _write(
        config_root / "global.yaml",
        "global:\n  live_trading_enabled: false\n  timezone: Asia/Kolkata\n"
        "runtime_defaults:\n  enabled: false\n  live_execution_allowed: false\n"
        "strategy_defaults:\n  enabled: false\n  mode: paper\n  live_approved: false\n",
    )
    _write(
        config_root / "runtimes" / "intraday_options.yaml",
        "runtime_id: intraday_options\nenabled: true\nlive_execution_allowed: false\n",
    )
    _write(
        config_root / "runtimes" / "positional_options.yaml",
        f"runtime_id: positional_options\nenabled: {str(positional_enabled).lower()}\n"
        "live_execution_allowed: false\n",
    )
    _write(
        config_root / "strategies" / "c921_ema_cross_buy.yaml",
        "strategy_id: c921_ema_cross_buy\nruntime_id: intraday_options\n"
        "enabled: true\nmode: paper\nlive_approved: false\n",
    )
    _write(
        config_root / "strategies" / "skeleton_fixture.yaml",
        "strategy_id: skeleton_fixture\nruntime_id: intraday_options\n"
        "enabled: false\nmode: paper\nlive_approved: false\n",
    )


def _seed_operational_db(operational_root: Path) -> ExecutionRepository:
    database = Database(operational_root / "intraday_options.db")
    MigrationRunner(database).run_pending()
    return ExecutionRepository(database)


# =============================================================== market status
def test_market_status_open_during_session_hours_on_a_weekday():
    # Friday 2026-08-14, 10:00 IST.
    now = datetime(2026, 8, 14, 10, 0, tzinfo=IST)
    assert home_page.market_status(now) == "OPEN"


def test_market_status_closed_before_open():
    now = datetime(2026, 8, 14, 8, 0, tzinfo=IST)
    assert home_page.market_status(now) == "CLOSED"


def test_market_status_closed_on_saturday():
    now = datetime(2026, 8, 15, 10, 0, tzinfo=IST)  # Saturday
    assert home_page.market_status(now) == "CLOSED"


# ============================================================== load_home
def test_load_home_aggregates_configured_and_not_configured_categories(tmp_path: Path):
    config_root = tmp_path / "config"
    operational_root = tmp_path / "operational"
    _seed_config(config_root, positional_enabled=False)
    repository = _seed_operational_db(operational_root)
    session = repository.open_session(
        runtime_id="intraday_options", strategy_id="c921_ema_cross_buy",
        execution_mode=ExecutionMode.PAPER, process_role="worker", pid=1,
    )
    repository.record_heartbeat(
        session_id=session.id, runtime_id="intraday_options", strategy_id="c921_ema_cross_buy",
        health_state="RUNNING_PAPER",
    )

    view = home_page.load_home(
        config_root=config_root, operational_root=operational_root, trading_date=TRADING_DATE
    )

    assert view.total_strategies == 2  # c921_ema_cross_buy + skeleton_fixture
    assert view.disabled_count == 1  # skeleton_fixture
    assert view.running_count == 1

    by_category = {card.category: card for card in view.category_cards}
    assert by_category["Intraday Options"].configured is True
    assert by_category["Positional Options"].configured is False
    assert by_category["Positional Options"].status_label == "NOT CONFIGURED"
    assert by_category["Intraday Stocks"].configured is False


def test_a_missing_runtime_database_degrades_only_that_card(tmp_path: Path):
    """A runtime whose config says enabled but whose database was never
    created (e.g. supervisor has never started) must not raise and must
    not affect the other two cards."""
    config_root = tmp_path / "config"
    operational_root = tmp_path / "operational"
    operational_root.mkdir(parents=True)
    _seed_config(config_root, positional_enabled=True)
    # positional_options.yaml says enabled: true, but no database file
    # exists for it — exactly the "configured but never started" state.

    view = home_page.load_home(
        config_root=config_root, operational_root=operational_root, trading_date=TRADING_DATE
    )

    by_category = {card.category: card for card in view.category_cards}
    assert by_category["Positional Options"].configured is True
    assert by_category["Positional Options"].status_label == "STOPPED"
    assert by_category["Positional Options"].detail  # a real reason, not blank
    # Intraday Options card also has no database yet in this test, and must
    # independently report its own STOPPED state rather than raising.
    assert by_category["Intraday Options"].status_label == "STOPPED"
    assert by_category["Intraday Stocks"].status_label == "NOT CONFIGURED"


def test_paper_and_live_pnl_are_never_combined(tmp_path: Path):
    config_root = tmp_path / "config"
    operational_root = tmp_path / "operational"
    _seed_config(config_root)
    repository = _seed_operational_db(operational_root)
    now = datetime.now(UTC).isoformat()
    with repository.database.transaction() as conn:
        conn.execute(
            "INSERT INTO positions (runtime_id, strategy_id, execution_mode, trading_date, "
            "instrument, security_id, quantity, average_price, realised_pnl, charges, status, "
            "opened_at, updated_at) VALUES ('intraday_options', 'c921_ema_cross_buy', 'paper', "
            f"'{TRADING_DATE}', 'NIFTY', '13', 0, 100.0, 500.0, 0.0, 'CLOSED', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO positions (runtime_id, strategy_id, execution_mode, trading_date, "
            "instrument, security_id, quantity, average_price, realised_pnl, charges, status, "
            "opened_at, updated_at) VALUES ('intraday_options', 'c921_ema_cross_buy', 'live', "
            f"'{TRADING_DATE}', 'NIFTY', '13', 0, 100.0, -200.0, 0.0, 'CLOSED', ?, ?)",
            (now, now),
        )

    view = home_page.load_home(
        config_root=config_root, operational_root=operational_root, trading_date=TRADING_DATE
    )
    assert view.realised_pnl_paper == 500.0
    assert view.realised_pnl_live == -200.0

    st = FakeStreamlit()
    home_page.render(st, view)
    labels = {label for label, _ in st.metrics}
    assert "Realised P&L — paper" in labels
    assert "Realised P&L — live" in labels
    # Never one combined "P&L" metric hiding the mode split.
    assert not any(label == "Realised P&L" for label in labels)


def test_render_home_produces_page_links_for_every_category(tmp_path: Path):
    config_root = tmp_path / "config"
    operational_root = tmp_path / "operational"
    _seed_config(config_root)
    _seed_operational_db(operational_root)

    view = home_page.load_home(
        config_root=config_root, operational_root=operational_root, trading_date=TRADING_DATE
    )
    st = FakeStreamlit()
    home_page.render(st, view)
    assert len(st.page_links) == 3
    labelled_categories = {label for _, label in st.page_links}
    assert any("Intraday Options" in label for label in labelled_categories)
    assert any("Positional Options" in label for label in labelled_categories)
    assert any("Intraday Stocks" in label for label in labelled_categories)
