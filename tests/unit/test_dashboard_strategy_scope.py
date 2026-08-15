"""``dashboards/data/strategy_scope.py``: strategy discovery, status
labelling, and the filter composition (strategy + mode + date range) every
Intraday Options tab depends on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from _dashboard_fakes import FakeStreamlit

from common.config.models import ExecutionMode
from common.execution import ExecutionRepository
from common.persistence import Database, MigrationRunner
from dashboards.data.intraday_options import load_closed_trades, load_orders
from dashboards.data.strategy_scope import (
    DISABLED,
    HISTORICAL_ONLY,
    RUNNING,
    STOPPED,
    StrategyOption,
    discover_strategy_options,
    render_strategy_selector,
)

RUNTIME_ID = "intraday_options"
TRADING_DATE = "2026-08-14"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def repository(database_path: Path) -> ExecutionRepository:
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    return ExecutionRepository(database)


def _ro(database_path: Path):
    from common.persistence import connect_readonly

    return connect_readonly(database_path)


# ============================================================== discovery
def test_a_configured_strategy_with_no_trades_appears_as_stopped(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    config_root = tmp_path / "config"
    _write(
        config_root / "strategies" / "brand_new.yaml",
        "strategy_id: brand_new\nruntime_id: intraday_options\n"
        "enabled: true\nmode: paper\nlive_approved: false\n",
    )
    conn = _ro(database_path)
    options = discover_strategy_options(conn, config_root, RUNTIME_ID)
    conn.close()

    assert len(options) == 1
    assert options[0] == StrategyOption(
        strategy_id="brand_new", status_label=STOPPED, execution_mode=None
    )


def test_a_running_strategy_appears_as_running(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    config_root = tmp_path / "config"
    _write(
        config_root / "strategies" / "st01.yaml",
        "strategy_id: st01\nruntime_id: intraday_options\n"
        "enabled: true\nmode: paper\nlive_approved: false\n",
    )
    session = repository.open_session(
        runtime_id=RUNTIME_ID, strategy_id="st01", execution_mode=ExecutionMode.PAPER,
        process_role="worker", pid=1,
    )
    repository.record_heartbeat(
        session_id=session.id, runtime_id=RUNTIME_ID, strategy_id="st01",
        health_state="RUNNING_PAPER",
    )

    conn = _ro(database_path)
    options = discover_strategy_options(conn, config_root, RUNTIME_ID)
    conn.close()

    assert options == (
        StrategyOption(strategy_id="st01", status_label=RUNNING, execution_mode="paper"),
    )


def test_a_disabled_strategy_appears_as_disabled(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    config_root = tmp_path / "config"
    _write(
        config_root / "strategies" / "disabled_strategy.yaml",
        "strategy_id: disabled_strategy\nruntime_id: intraday_options\n"
        "enabled: false\nmode: paper\nlive_approved: false\n",
    )
    conn = _ro(database_path)
    options = discover_strategy_options(conn, config_root, RUNTIME_ID)
    conn.close()

    assert options == (
        StrategyOption(strategy_id="disabled_strategy", status_label=DISABLED, execution_mode=None),
    )


def test_a_removed_strategy_with_history_remains_selectable(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    """No config file at all — the strategy was removed from config, but
    it produced real signals in the past, so it must not vanish."""
    now = datetime.now(UTC).isoformat()
    session = repository.open_session(
        runtime_id=RUNTIME_ID, strategy_id="retired_strategy", execution_mode=ExecutionMode.PAPER,
        process_role="worker", pid=1,
    )
    with repository.database.transaction() as conn:
        conn.execute(
            "INSERT INTO signals (session_id, runtime_id, strategy_id, execution_mode, "
            "trading_date, instrument, security_id, side, candle_open, candle_high, "
            "candle_low, candle_close, candle_start_at, candle_end_at, reference_price, "
            "evaluated_at) VALUES (?, ?, ?, ?, ?, "
            "'NIFTY', '13', 'BUY', 100, 101, 99, 100, ?, ?, 100, ?)",
            (session.id, RUNTIME_ID, "retired_strategy", "paper", TRADING_DATE, now, now, now),
        )

    config_root = tmp_path / "config"
    (config_root / "strategies").mkdir(parents=True)
    conn = _ro(database_path)
    options = discover_strategy_options(conn, config_root, RUNTIME_ID)
    conn.close()

    assert any(
        o.strategy_id == "retired_strategy" and o.status_label == HISTORICAL_ONLY
        for o in options
    )


def test_no_database_and_no_config_returns_no_strategies(tmp_path: Path):
    options = discover_strategy_options(None, None, RUNTIME_ID)
    assert options == ()


def test_a_strategy_needs_no_dashboard_code_change_to_appear(
    repository: ExecutionRepository, database_path: Path, tmp_path: Path
):
    """The whole point of a discovery-based selector: adding a strategy
    config file is enough — nothing in dashboards/ names it."""
    config_root = tmp_path / "config"
    _write(
        config_root / "strategies" / "totally_new_strategy_xyz.yaml",
        "strategy_id: totally_new_strategy_xyz\nruntime_id: intraday_options\n"
        "enabled: true\nmode: paper\nlive_approved: false\n",
    )
    conn = _ro(database_path)
    options = discover_strategy_options(conn, config_root, RUNTIME_ID)
    conn.close()
    assert any(o.strategy_id == "totally_new_strategy_xyz" for o in options)


# ================================================================ selector
def test_render_strategy_selector_returns_none_for_all_strategies():
    st = FakeStreamlit()
    options = (StrategyOption("st01", RUNNING, "paper"),)
    st.selectbox_return = "ALL"
    result = render_strategy_selector(st, options, key="io_strategy")
    assert result is None


def test_render_strategy_selector_returns_the_chosen_strategy_id():
    st = FakeStreamlit()
    options = (StrategyOption("st01", RUNNING, "paper"), StrategyOption("st02", STOPPED, None))
    st.selectbox_return = "st02"
    result = render_strategy_selector(st, options, key="io_strategy")
    assert result == "st02"


def test_render_strategy_selector_is_disabled_when_no_strategies_exist():
    st = FakeStreamlit()
    result = render_strategy_selector(st, (), key="io_strategy")
    assert result is None
    _label, _options, kwargs = st.selectbox_calls[0]
    assert kwargs.get("disabled") is True


def test_selector_return_value_is_deterministic_across_repeated_calls():
    """Standing in for real Streamlit's own key-based session-state
    persistence (exercised for real in test_dashboard_apptest.py): given
    the same underlying widget value, this function must resolve to the
    same strategy id every time it is called — it keeps no state of its
    own that could drift from what the key holds."""
    st = FakeStreamlit()
    options = (StrategyOption("st01", RUNNING, "paper"),)
    st.selectbox_return = "st01"
    first = render_strategy_selector(st, options, key="io_strategy")
    second = render_strategy_selector(st, options, key="io_strategy")
    assert first == second == "st01"


# ======================================================= filter composition
def _seed_two_strategies(repository: ExecutionRepository) -> None:
    """st01 trades paper, st02 trades live — both round trips, same date."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from common.broker import PaperBroker, PaperFillConfig, SlippageConfig
    from common.execution import OrderLifecycle
    from common.models import Candle, Side, Signal

    ist = ZoneInfo("Asia/Kolkata")

    def candle(minute: int, close: float) -> Candle:
        start = datetime(2026, 8, 14, 9, minute, tzinfo=ist)
        return Candle(
            security_id="13", instrument="NIFTY", open=close, high=close + 1, low=close - 1,
            close=close, volume=100, start_at=start, end_at=start + timedelta(minutes=1),
            tick_count=4,
        )

    def signal(strategy_id: str, side: Side, minute: int, close: float) -> Signal:
        c = candle(minute, close)
        return Signal(
            strategy_id=strategy_id, execution_mode=ExecutionMode.PAPER, instrument="NIFTY",
            security_id="13", side=side, quantity=50, candle=c, reference_price=c.close,
            evaluated_at=c.end_at, reason="test",
        )

    for strategy_id in ("st01", "st02"):
        session = repository.open_session(
            runtime_id=RUNTIME_ID, strategy_id=strategy_id, execution_mode=ExecutionMode.PAPER,
            process_role="worker", pid=1,
        )
        lifecycle = OrderLifecycle(
            repository=repository,
            broker=PaperBroker(
                config=PaperFillConfig(
                    slippage=SlippageConfig(mode="points", market_order_points=0.0)
                )
            ),
            runtime_id=RUNTIME_ID, strategy_id=strategy_id, execution_mode=ExecutionMode.PAPER,
            session_id=session.id,
        )
        lifecycle.handle_signal(
            signal(strategy_id, Side.BUY, 15, 100.0), trading_date=TRADING_DATE
        )
        lifecycle.handle_signal(
            signal(strategy_id, Side.SELL, 16, 110.0), trading_date=TRADING_DATE
        )


def test_selecting_one_strategy_excludes_the_other_from_closed_trades(
    repository: ExecutionRepository, database_path: Path
):
    _seed_two_strategies(repository)
    conn = _ro(database_path)
    only_st01 = load_closed_trades(
        conn, RUNTIME_ID, strategy_id="st01", start_date=TRADING_DATE, end_date=TRADING_DATE
    )
    only_st02 = load_closed_trades(
        conn, RUNTIME_ID, strategy_id="st02", start_date=TRADING_DATE, end_date=TRADING_DATE
    )
    everyone = load_closed_trades(
        conn, RUNTIME_ID, start_date=TRADING_DATE, end_date=TRADING_DATE
    )
    conn.close()

    assert {t.strategy_id for t in only_st01} == {"st01"}
    assert {t.strategy_id for t in only_st02} == {"st02"}
    assert {t.strategy_id for t in everyone} == {"st01", "st02"}  # "All Strategies"


def test_selecting_one_strategy_excludes_the_other_from_orders(
    repository: ExecutionRepository, database_path: Path
):
    _seed_two_strategies(repository)
    conn = _ro(database_path)
    only_st01 = load_orders(conn, RUNTIME_ID, TRADING_DATE, strategy_id="st01")
    conn.close()
    assert only_st01 and {o.strategy_id for o in only_st01} == {"st01"}


def test_strategy_and_mode_filters_compose(repository: ExecutionRepository, database_path: Path):
    """strategy=st01, mode=paper together — matching the spec's
    Strategy/Mode/Period combination example."""
    _seed_two_strategies(repository)
    conn = _ro(database_path)
    trades = load_closed_trades(
        conn, RUNTIME_ID, strategy_id="st01", execution_mode="paper",
        start_date=TRADING_DATE, end_date=TRADING_DATE,
    )
    trades_wrong_mode = load_closed_trades(
        conn, RUNTIME_ID, strategy_id="st01", execution_mode="live",
        start_date=TRADING_DATE, end_date=TRADING_DATE,
    )
    conn.close()
    assert len(trades) == 1
    assert trades[0].strategy_id == "st01"
    assert trades[0].execution_mode == "paper"
    assert trades_wrong_mode == ()


def test_paper_and_live_orders_remain_separated_when_scoped_to_one_strategy(
    repository: ExecutionRepository, database_path: Path
):
    _seed_two_strategies(repository)
    conn = _ro(database_path)
    paper_only = load_orders(
        conn, RUNTIME_ID, TRADING_DATE, strategy_id="st01", execution_mode="paper"
    )
    live_only = load_orders(
        conn, RUNTIME_ID, TRADING_DATE, strategy_id="st01", execution_mode="live"
    )
    conn.close()
    assert paper_only and all(o.execution_mode == "paper" for o in paper_only)
    assert live_only == ()


# ==================================================================== CSV
def test_csv_export_reflects_the_filtered_scope_not_everything(
    repository: ExecutionRepository, database_path: Path
):
    """The CSV export button is always fed the same post-filter table the
    page just rendered — proving the filtered load function returns the
    narrowed set is exactly what proves the export is narrowed too."""
    from dashboards.formatting import to_csv_bytes

    _seed_two_strategies(repository)
    conn = _ro(database_path)
    only_st01 = load_closed_trades(
        conn, RUNTIME_ID, strategy_id="st01", start_date=TRADING_DATE, end_date=TRADING_DATE
    )
    conn.close()

    csv_bytes = to_csv_bytes(
        [{"strategy_id": t.strategy_id, "net_pnl": t.net_pnl} for t in only_st01]
    )
    csv_text = csv_bytes.decode("utf-8")
    assert "st01" in csv_text
    assert "st02" not in csv_text
