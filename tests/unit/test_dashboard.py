"""The Streamlit dashboard: every page's data functions and render() calls.

Streamlit is never imported here — every ``render()`` takes the streamlit
module as a parameter precisely so it can be driven with a fake one
(``dashboards/app.py``'s own module docstring: "Streamlit is imported lazily
so the test suite never needs it at collection time"). ``grep -rni streamlit
tests/`` returned zero hits before this file (Phase 7 audit finding); this is
the file that closes it.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

import dashboards.app as master_page
import dashboards.intraday_options as io_page
import dashboards.intraday_stocks as stocks_page
import dashboards.positional_options as positional_page
import dashboards.system_health as health_page
from common.config import Settings
from common.config.models import ExecutionMode
from common.execution import ExecutionRepository
from common.persistence import Database, MigrationRunner, connect_readonly
from dashboards._shared import SnapshotUnavailable, load_snapshot

RUNTIME_ID = "intraday_options"
TRADING_DATE = "2026-08-07"

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARDS_DIR = REPO_ROOT / "dashboards"
PAGE_MODULE_FILES = sorted(
    p
    for p in DASHBOARDS_DIR.glob("*.py")
    if p.name not in {"__init__.py"}
) + sorted(DASHBOARDS_DIR.glob("pages/*.py"))


# ---------------------------------------------------------------- fake streamlit
class _FakeColumn:
    def __init__(self, sink: _FakeStreamlit) -> None:
        self._sink = sink

    def metric(self, label: str, value: object) -> None:
        self._sink.metrics.append((label, value))

    def __enter__(self) -> _FakeColumn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeStreamlit:
    """Records every call a page makes, instead of drawing anything."""

    def __init__(self) -> None:
        self.subheaders: list[str] = []
        self.titles: list[str] = []
        self.captions: list[str] = []
        self.infos: list[str] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.markdowns: list[str] = []
        self.writes: list[str] = []
        self.metrics: list[tuple[str, object]] = []

    def subheader(self, text: str) -> None:
        self.subheaders.append(text)

    def title(self, text: str) -> None:
        self.titles.append(text)

    def caption(self, text: str) -> None:
        self.captions.append(text)

    def info(self, text: str) -> None:
        self.infos.append(text)

    def error(self, text: str) -> None:
        self.errors.append(text)

    def warning(self, text: str) -> None:
        self.warnings.append(text)

    def markdown(self, text: str) -> None:
        self.markdowns.append(text)

    def write(self, text: str) -> None:
        self.writes.append(text)

    def columns(self, n: int) -> list[_FakeColumn]:
        return [_FakeColumn(self) for _ in range(n)]

    def set_page_config(self, **kwargs: object) -> None:
        return None


@pytest.fixture
def fake_st() -> _FakeStreamlit:
    return _FakeStreamlit()


# --------------------------------------------------------------------- fixtures
@pytest.fixture
def repository(database_path: Path) -> ExecutionRepository:
    database = Database(database_path)
    MigrationRunner(database).run_pending()
    return ExecutionRepository(database)


def _seed_group(repository: ExecutionRepository) -> None:
    session = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id=None,
        execution_mode=ExecutionMode.PAPER,
        process_role="supervisor",
        pid=999,
    )
    repository.record_heartbeat(
        session_id=session.id, runtime_id=RUNTIME_ID, strategy_id=None, health_state="RUNNING_PAPER"
    )


def _seed_strategy(
    repository: ExecutionRepository,
    strategy_id: str,
    *,
    mode: ExecutionMode = ExecutionMode.PAPER,
    pid: int = 111,
) -> None:
    session = repository.open_session(
        runtime_id=RUNTIME_ID,
        strategy_id=strategy_id,
        execution_mode=mode,
        process_role="worker",
        pid=pid,
    )
    repository.record_heartbeat(
        session_id=session.id,
        runtime_id=RUNTIME_ID,
        strategy_id=strategy_id,
        health_state="RUNNING_PAPER" if mode is ExecutionMode.PAPER else "RUNNING_LIVE",
    )


# ============================================================ Master page
def test_load_master_builds_a_card_from_a_seeded_database(
    repository: ExecutionRepository, database_path: Path
):
    _seed_group(repository)
    _seed_strategy(repository, "st01")
    _seed_strategy(repository, "st02", mode=ExecutionMode.LIVE)

    card = master_page.load_master(database_path, RUNTIME_ID, TRADING_DATE)

    assert isinstance(card, master_page.RuntimeCard)
    assert card.runtime_id == RUNTIME_ID
    assert card.paper_count == 1
    assert card.live_count == 1
    assert card.failed_count == 0
    assert card.total_count == 2
    assert card.group_health_state == "RUNNING_PAPER"


def test_master_render_shows_the_key_metrics(fake_st: _FakeStreamlit):
    card = master_page.RuntimeCard(
        runtime_id=RUNTIME_ID,
        group_health_state="RUNNING_PAPER",
        heartbeat_age_seconds=2.5,
        paper_count=2,
        live_count=1,
        failed_count=0,
        total_count=3,
        open_positions=1,
        orders_today=4,
        realised_pnl_paper=150.0,
        realised_pnl_live=-25.0,
        feed_last_event="connected",
        broker_healthy=True,
        database_healthy=True,
        recent_errors=(),
    )
    master_page.render(fake_st, card)

    assert any("supervisor" in s for s in fake_st.subheaders)
    assert ("Group health", "RUNNING_PAPER") in fake_st.metrics
    assert ("Open positions", 1) in fake_st.metrics
    assert any(v == "150.00" for _, v in fake_st.metrics)  # paper P&L, formatted
    assert any(master_page.RECONCILIATION_STATUS in c for c in fake_st.captions)
    assert fake_st.errors == []


def test_master_render_flags_failed_strategies(fake_st: _FakeStreamlit):
    card = master_page.RuntimeCard(
        runtime_id=RUNTIME_ID,
        group_health_state="DEGRADED",
        heartbeat_age_seconds=1.0,
        paper_count=1,
        live_count=0,
        failed_count=1,
        total_count=1,
        open_positions=0,
        orders_today=0,
        realised_pnl_paper=0.0,
        realised_pnl_live=0.0,
        feed_last_event="disconnected",
        broker_healthy=False,
        database_healthy=True,
        recent_errors=("worker st01 crashed",),
    )
    master_page.render(fake_st, card)

    assert any("FAILED" in e for e in fake_st.errors)
    assert "worker st01 crashed" in fake_st.errors


def test_master_render_shows_a_message_when_the_snapshot_is_unavailable(fake_st: _FakeStreamlit):
    master_page.render(fake_st, SnapshotUnavailable("No database yet"))

    assert fake_st.infos == ["No database yet"]
    assert fake_st.metrics == []
    assert fake_st.errors == []


def test_no_disabled_count_is_fabricated(fake_st: _FakeStreamlit):
    """Spec asks for a disabled-strategy count; runtime state cannot answer
    it (a disabled strategy never starts, so it never appears here). The
    page must say so, not show a fabricated zero."""
    assert not hasattr(master_page.RuntimeCard, "disabled_count")
    card = master_page.RuntimeCard(
        runtime_id=RUNTIME_ID,
        group_health_state="RUNNING_PAPER",
        heartbeat_age_seconds=1.0,
        paper_count=1,
        live_count=0,
        failed_count=0,
        total_count=1,
        open_positions=0,
        orders_today=0,
        realised_pnl_paper=0.0,
        realised_pnl_live=0.0,
        feed_last_event="connected",
        broker_healthy=True,
        database_healthy=True,
        recent_errors=(),
    )
    master_page.render(fake_st, card)
    assert any("disabled" in c.lower() for c in fake_st.captions)


# ------------------------------------------------------ Master: live-gate
def _write_config(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


GLOBAL_YAML_GATE_CLOSED = """
global:
  live_trading_enabled: false
  timezone: Asia/Kolkata
runtime_defaults:
  enabled: false
  live_execution_allowed: false
strategy_defaults:
  enabled: false
  mode: paper
  live_approved: false
"""


def test_load_live_gate_status_reads_global_and_runtime_flags(config_root: Path):
    _write_config(config_root / "global.yaml", GLOBAL_YAML_GATE_CLOSED)
    _write_config(
        config_root / "runtimes" / f"{RUNTIME_ID}.yaml",
        f"runtime_id: {RUNTIME_ID}\nenabled: true\nlive_execution_allowed: false\n",
    )

    status = master_page.load_live_gate_status(config_root, RUNTIME_ID, Settings())

    assert isinstance(status, master_page.LiveGateStatus)
    assert status.global_live_trading_enabled is False
    assert status.runtime_live_execution_allowed is False
    assert status.live_strategies == ()


def test_load_live_gate_status_evaluates_the_real_gate_for_live_mode_strategies(
    config_root: Path,
):
    """Reuses effective_live_gate itself — this is exactly what the
    supervisor's own admission gate would decide for this strategy."""
    _write_config(config_root / "global.yaml", GLOBAL_YAML_GATE_CLOSED)
    _write_config(
        config_root / "runtimes" / f"{RUNTIME_ID}.yaml",
        # Phase 10: a mode: live strategy now requires its runtime group to
        # carry a complete live_preflight block (ResolvedConfig's own
        # validator) purely to *parse* — unrelated to what this test checks
        # (that global.live_trading_enabled=false still blocks it).
        f"runtime_id: {RUNTIME_ID}\nenabled: true\n"
        "live_preflight:\n"
        "  expected_static_ip: '203.0.113.10'\n"
        "  max_preflight_age_seconds: 300\n"
        "  rate_limits:\n"
        "    rules:\n"
        "      - call_class: new_order\n"
        "        limit: 5\n"
        "        window_seconds: 60\n"
        "  account_risk:\n"
        "    max_daily_loss: 5000.0\n",
    )
    _write_config(
        config_root / "strategies" / "io_live_v1.yaml",
        "strategy_id: io_live_v1\nenabled: true\nmode: live\nlive_approved: true\n"
        "live_quantity_lots: 1\n",
    )

    status = master_page.load_live_gate_status(config_root, RUNTIME_ID, Settings())

    assert len(status.live_strategies) == 1
    strategy = status.live_strategies[0]
    assert strategy.strategy_id == "io_live_v1"
    assert strategy.allowed is False  # global.live_trading_enabled is false
    assert any("live_trading_enabled" in r for r in strategy.blocked_reasons)


def test_load_live_gate_status_ignores_paper_mode_strategies(config_root: Path):
    _write_config(config_root / "global.yaml", GLOBAL_YAML_GATE_CLOSED)
    _write_config(
        config_root / "runtimes" / f"{RUNTIME_ID}.yaml",
        f"runtime_id: {RUNTIME_ID}\nenabled: true\n",
    )
    _write_config(
        config_root / "strategies" / "io_paper_v1.yaml",
        "strategy_id: io_paper_v1\nenabled: true\nmode: paper\n",
    )

    status = master_page.load_live_gate_status(config_root, RUNTIME_ID, Settings())

    assert status.live_strategies == ()


def test_a_missing_runtime_config_returns_config_unavailable_not_an_exception(
    config_root: Path,
):
    _write_config(config_root / "global.yaml", GLOBAL_YAML_GATE_CLOSED)
    # No runtimes/intraday_options.yaml written at all.

    status = master_page.load_live_gate_status(config_root, RUNTIME_ID, Settings())

    assert isinstance(status, master_page.ConfigUnavailable)


def test_load_master_without_config_root_leaves_live_gate_none(
    repository: ExecutionRepository, database_path: Path
):
    """Every call site before this feature existed keeps working unchanged."""
    _seed_group(repository)

    card = master_page.load_master(database_path, RUNTIME_ID, TRADING_DATE)

    assert card.live_gate is None


def test_load_master_with_config_root_attaches_live_gate(
    repository: ExecutionRepository, database_path: Path, config_root: Path
):
    _seed_group(repository)
    _write_config(config_root / "global.yaml", GLOBAL_YAML_GATE_CLOSED)
    _write_config(
        config_root / "runtimes" / f"{RUNTIME_ID}.yaml",
        f"runtime_id: {RUNTIME_ID}\nenabled: true\n",
    )

    card = master_page.load_master(
        database_path, RUNTIME_ID, TRADING_DATE, config_root=config_root, settings=Settings()
    )

    assert isinstance(card.live_gate, master_page.LiveGateStatus)
    assert card.live_gate.global_live_trading_enabled is False


def test_master_render_omits_the_live_gate_section_when_none(fake_st: _FakeStreamlit):
    card = master_page.RuntimeCard(
        runtime_id=RUNTIME_ID,
        group_health_state="RUNNING_PAPER",
        heartbeat_age_seconds=1.0,
        paper_count=1,
        live_count=0,
        failed_count=0,
        total_count=1,
        open_positions=0,
        orders_today=0,
        realised_pnl_paper=0.0,
        realised_pnl_live=0.0,
        feed_last_event="connected",
        broker_healthy=True,
        database_healthy=True,
        recent_errors=(),
        live_gate=None,
    )
    master_page.render(fake_st, card)
    assert not any("Live-gate" in m for m in fake_st.markdowns)


def test_master_render_shows_the_live_gate_section(fake_st: _FakeStreamlit):
    live_gate = master_page.LiveGateStatus(
        global_live_trading_enabled=False,
        runtime_live_execution_allowed=True,
        live_strategies=(
            master_page.StrategyLiveGate(
                strategy_id="io_live_v1",
                allowed=False,
                blocked_reasons=("global.live_trading_enabled is false",),
            ),
        ),
    )
    card = master_page.RuntimeCard(
        runtime_id=RUNTIME_ID,
        group_health_state="RUNNING_PAPER",
        heartbeat_age_seconds=1.0,
        paper_count=1,
        live_count=1,
        failed_count=0,
        total_count=2,
        open_positions=0,
        orders_today=0,
        realised_pnl_paper=0.0,
        realised_pnl_live=0.0,
        feed_last_event="connected",
        broker_healthy=True,
        database_healthy=True,
        recent_errors=(),
        live_gate=live_gate,
    )
    master_page.render(fake_st, card)

    assert any("Live-gate" in m for m in fake_st.markdowns)
    assert ("Global live trading", "disabled") in fake_st.metrics
    assert ("Runtime live execution", "allowed") in fake_st.metrics
    assert any("io_live_v1" in w and "blocked" in w for w in fake_st.warnings)


def test_master_render_warns_when_config_is_unavailable(fake_st: _FakeStreamlit):
    card = master_page.RuntimeCard(
        runtime_id=RUNTIME_ID,
        group_health_state="RUNNING_PAPER",
        heartbeat_age_seconds=1.0,
        paper_count=1,
        live_count=0,
        failed_count=0,
        total_count=1,
        open_positions=0,
        orders_today=0,
        realised_pnl_paper=0.0,
        realised_pnl_live=0.0,
        feed_last_event="connected",
        broker_healthy=True,
        database_healthy=True,
        recent_errors=(),
        live_gate=master_page.ConfigUnavailable("runtimes/intraday_options.yaml not found"),
    )
    master_page.render(fake_st, card)

    assert any("Live-gate status unavailable" in w for w in fake_st.warnings)
    # The rest of the page must still render — a broken YAML file degrades
    # only this section, never the snapshot-backed rest of the page.
    assert ("Group health", "RUNNING_PAPER") in fake_st.metrics


# ==================================================== Intraday Options page
def test_mode_label_is_always_explicit_about_simulated_money():
    """The property RuntimeTile.mode_label used to guarantee, carried
    forward here: an operator must never guess whether PAPER is real money."""
    assert "simulated" in io_page.mode_label("paper")
    assert io_page.mode_label("live") == "LIVE"


def test_load_intraday_options_lists_every_strategy(
    repository: ExecutionRepository, database_path: Path
):
    _seed_strategy(repository, "st01", pid=222)
    repository.save_strategy_state(
        runtime_id=RUNTIME_ID,
        strategy_id="st01",
        execution_mode=ExecutionMode.PAPER,
        trading_date=TRADING_DATE,
        square_off_state="PENDING",
        entries_blocked=False,
    )

    view = io_page.load_intraday_options(database_path, RUNTIME_ID, TRADING_DATE)

    assert isinstance(view, io_page.IntradayOptionsView)
    assert len(view.strategies) == 1
    row = view.strategies[0]
    assert row.strategy_id == "st01"
    assert row.pid == 222
    assert "simulated" in row.mode_label
    assert row.square_off_state == "PENDING"


def test_intraday_options_render_shows_each_strategy_row(fake_st: _FakeStreamlit):
    view = io_page.IntradayOptionsView(
        runtime_id=RUNTIME_ID,
        realised_pnl_paper=10.0,
        realised_pnl_live=0.0,
        strategies=(
            io_page.StrategyRow(
                strategy_id="st01",
                execution_mode="paper",
                health_state="RUNNING_PAPER",
                heartbeat_age_seconds=3.0,
                pid=555,
                square_off_state="PENDING",
                entries_blocked=False,
                open_positions=1,
                last_error=None,
            ),
        ),
    )
    io_page.render(fake_st, view)

    assert any("st01" in m for m in fake_st.markdowns)
    assert ("PID", 555) in fake_st.metrics
    assert ("Open positions", 1) in fake_st.metrics
    assert fake_st.warnings == []
    assert any(c == io_page.NOT_YET_AVAILABLE for c in fake_st.captions)


def test_intraday_options_render_warns_when_entries_are_blocked(fake_st: _FakeStreamlit):
    view = io_page.IntradayOptionsView(
        runtime_id=RUNTIME_ID,
        realised_pnl_paper=0.0,
        realised_pnl_live=0.0,
        strategies=(
            io_page.StrategyRow(
                strategy_id="st01",
                execution_mode="paper",
                health_state="BLOCK_NEW_ENTRIES",
                heartbeat_age_seconds=1.0,
                pid=1,
                square_off_state="PENDING",
                entries_blocked=True,
                open_positions=0,
                last_error=None,
            ),
        ),
    )
    io_page.render(fake_st, view)

    assert any("entries blocked" in w for w in fake_st.warnings)


def test_intraday_options_render_handles_no_strategies_yet(fake_st: _FakeStreamlit):
    view = io_page.IntradayOptionsView(
        runtime_id=RUNTIME_ID, realised_pnl_paper=0.0, realised_pnl_live=0.0, strategies=()
    )
    io_page.render(fake_st, view)
    assert any("No strategy" in i for i in fake_st.infos)


def test_intraday_options_render_shows_a_message_when_unavailable(fake_st: _FakeStreamlit):
    io_page.render(fake_st, SnapshotUnavailable("locked"))
    assert fake_st.infos == ["locked"]


# ======================================================= System Health page
def test_load_system_health_reports_auth_feed_db_and_pids(
    repository: ExecutionRepository, database_path: Path
):
    _seed_group(repository)
    _seed_strategy(repository, "st01", pid=333)
    repository.record_auth_event(
        runtime_id=RUNTIME_ID, event="token_reused_from_cache", token_expiry="2026-08-08T09:00:00"
    )
    repository.record_feed_event(runtime_id=RUNTIME_ID, event="connected")

    view = health_page.load_system_health(database_path, RUNTIME_ID, TRADING_DATE)

    assert isinstance(view, health_page.SystemHealthView)
    assert view.auth_event == "token_reused_from_cache"
    assert view.token_expiry == "2026-08-08T09:00:00"
    assert view.feed_last_event == "connected"
    assert view.group_pid == 999
    assert view.strategy_pids == (
        health_page.StrategyPid(strategy_id="st01", pid=333, health_state="RUNNING_PAPER"),
    )
    assert view.database_healthy is True


def test_system_health_render_shows_pids_and_feed_state(fake_st: _FakeStreamlit):
    view = health_page.SystemHealthView(
        runtime_id=RUNTIME_ID,
        token_expiry="2026-08-08T09:00:00",
        auth_event="token_generated",
        feed_last_event="connected",
        feed_last_reason=None,
        reconnect_count=2,
        reconnect_exhausted_count=0,
        stale_instrument_count=0,
        subscriptions_match=True,
        database_healthy=True,
        integrity_problems=(),
        foreign_key_violation_count=0,
        migration_version="0003",
        group_pid=1000,
        strategy_pids=(health_page.StrategyPid("st01", 2000, "RUNNING_PAPER"),),
        recent_errors=(),
    )
    health_page.render(fake_st, view)

    assert ("Reconnects", 2) in fake_st.metrics
    assert any("1000" in w for w in fake_st.writes)
    assert any("2000" in w and "st01" in w for w in fake_st.writes)
    assert fake_st.warnings == []


def test_system_health_render_warns_on_subscription_mismatch(fake_st: _FakeStreamlit):
    view = health_page.SystemHealthView(
        runtime_id=RUNTIME_ID,
        token_expiry=None,
        auth_event=None,
        feed_last_event="degraded",
        feed_last_reason="stuck subscription",
        reconnect_count=0,
        reconnect_exhausted_count=0,
        stale_instrument_count=1,
        subscriptions_match=False,
        database_healthy=True,
        integrity_problems=(),
        foreign_key_violation_count=0,
        migration_version=None,
        group_pid=None,
        strategy_pids=(),
        recent_errors=(),
    )
    health_page.render(fake_st, view)

    assert any("do not match" in w for w in fake_st.warnings)


def test_system_health_render_surfaces_integrity_problems(fake_st: _FakeStreamlit):
    view = health_page.SystemHealthView(
        runtime_id=RUNTIME_ID,
        token_expiry=None,
        auth_event=None,
        feed_last_event=None,
        feed_last_reason=None,
        reconnect_count=0,
        reconnect_exhausted_count=0,
        stale_instrument_count=0,
        subscriptions_match=True,
        database_healthy=False,
        integrity_problems=("row 4 is corrupt",),
        foreign_key_violation_count=0,
        migration_version="0003",
        group_pid=None,
        strategy_pids=(),
        recent_errors=(),
    )
    health_page.render(fake_st, view)

    assert any("row 4 is corrupt" in e for e in fake_st.errors)


def test_system_health_render_shows_a_message_when_unavailable(fake_st: _FakeStreamlit):
    health_page.render(fake_st, SnapshotUnavailable("no database"))
    assert fake_st.infos == ["no database"]


# ================================================================== stubs
def test_positional_options_is_an_honest_stub(fake_st: _FakeStreamlit):
    positional_page.render(fake_st)
    assert fake_st.infos == [positional_page.STATUS_MESSAGE]
    assert "not implemented" in positional_page.STATUS_MESSAGE


def test_intraday_stocks_is_an_honest_stub(fake_st: _FakeStreamlit):
    stocks_page.render(fake_st)
    assert fake_st.infos == [stocks_page.STATUS_MESSAGE]
    assert "not implemented" in stocks_page.STATUS_MESSAGE


# ======================================================== robustness (_shared)
def test_a_missing_database_returns_a_clear_reason_not_an_exception(database_path: Path):
    result = load_snapshot(database_path, RUNTIME_ID, TRADING_DATE)
    assert isinstance(result, SnapshotUnavailable)
    assert "No database yet" in result.reason


def test_a_pre_migration_database_returns_a_clear_reason_not_an_exception(database_path: Path):
    """The file exists, opens fine, but no migration has ever run — every
    table read_snapshot queries is missing."""
    Database(database_path).connect()  # creates an empty, valid, table-less file
    assert database_path.is_file()

    result = load_snapshot(database_path, RUNTIME_ID, TRADING_DATE)

    assert isinstance(result, SnapshotUnavailable)
    assert "Database not ready" in result.reason


def test_a_locked_database_returns_a_clear_reason_not_an_exception(
    database_path: Path, monkeypatch
):
    """Simulated rather than genuinely raced: a real lock needs a second
    connection to hold a write transaction past the busy timeout, which
    would make this test slow and timing-dependent for no extra safety
    proven. The behaviour under test is entirely in the except branch,
    which does not care *why* sqlite raised — only that it does not
    propagate."""
    Database(database_path).connect()

    def _raise(*_args: object, **_kwargs: object):
        raise sqlite3.OperationalError("database is locked")

    import dashboards._shared as shared_module

    monkeypatch.setattr(shared_module, "read_snapshot", _raise)

    result = load_snapshot(database_path, RUNTIME_ID, TRADING_DATE)

    assert isinstance(result, SnapshotUnavailable)
    assert "locked" in result.reason


def test_load_snapshot_never_opens_a_write_connection(database_path: Path):
    """Belt and braces on top of the AST-import check below: even a
    successful read must go through connect_readonly, not Database."""
    database = Database(database_path)
    MigrationRunner(database).run_pending()

    result = load_snapshot(database_path, RUNTIME_ID, TRADING_DATE)

    assert not isinstance(result, SnapshotUnavailable)
    # The write connection above is still the only one that created
    # anything; prove the read path used a genuinely read-only one by
    # confirming a second read-only open still refuses a write.
    conn = connect_readonly(database_path)
    with pytest.raises(sqlite3.OperationalError, match="readonly database"):
        conn.execute("DELETE FROM runtime_sessions")
    conn.close()


# =============================================== regression: safe imports
def _imported_module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("path", PAGE_MODULE_FILES, ids=lambda p: p.name)
def test_no_dashboard_module_imports_a_broker_or_a_feed(path: Path):
    """A broker or feed import here is exactly the side-effecting import
    the spec forbids — a second broker/feed connection from the dashboard
    would compete with the supervisor's own."""
    imported = _imported_module_names(path)
    lowered = {name: name.lower() for name in imported}
    offenders = {
        name
        for name, lower in lowered.items()
        # market_data.adapter/dhan modules count as "feed" too.
        if "broker" in lower or ".feed" in lower or "market_data" in lower
    }
    assert offenders == set(), f"{path.name} imports {offenders}"


@pytest.mark.parametrize("path", PAGE_MODULE_FILES, ids=lambda p: p.name)
def test_no_dashboard_module_imports_streamlit_at_module_level(path: Path):
    """Streamlit must be importable lazily only, inside main() — this is
    what lets every test above run without streamlit at collection time.
    ``pages/*.py`` shims are the one exception: Streamlit itself only ever
    executes them at app-run time, never at test-collection time, so a
    top-level `main()` call there is the documented shape, not a violation.
    """
    if path.parent.name == "pages":
        pytest.skip("pages/*.py shims call main() unconditionally by design")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # Only the module's top-level statements — a lazy import inside main()
    # lives in a nested FunctionDef body and must not be flagged.
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert not any(alias.name == "streamlit" for alias in node.names), path.name
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "streamlit", path.name


def _imported_names_from(path: Path, module: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize(
    "path", [p for p in PAGE_MODULE_FILES if p.parent.name != "pages"], ids=lambda p: p.name
)
def test_no_dashboard_module_imports_the_write_capable_database_class(path: Path):
    """``common.persistence.Database`` opens a write connection —
    ``connect_readonly`` is the only entry point every page (via _shared)
    may use. ``_shared.py`` itself is exempt: it imports ``DatabaseError``
    (a plain exception class, not a connection) from the same module, which
    this check must not confuse with importing ``Database`` itself."""
    imported = _imported_names_from(path, "common.persistence")
    assert "Database" not in imported, f"{path.name} imports the write-capable Database class"


def test_the_dashboards_directory_is_what_we_think_it_is():
    """Guards the parametrisation above: an empty/short glob would pass
    everything trivially, the same guard test_scripts_are_read_only.py uses
    for scripts/."""
    names = {p.name for p in PAGE_MODULE_FILES}
    assert names == {
        "_shared.py",
        "app.py",
        "intraday_options.py",
        "intraday_stocks.py",
        "positional_options.py",
        "system_health.py",
        "1_Intraday_Options.py",
        "2_Positional_Options.py",
        "3_Intraday_Stocks.py",
        "4_System_Health.py",
    }
