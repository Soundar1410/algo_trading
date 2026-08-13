"""FIXTURE-BASED / CODE-LEVEL ACCEPTANCE — the mixed-mode architecture gate
(spec 2968-2977), proven through the real ``build_supervisor``/
``discover_enabled_strategies`` config-loading path, not hand-built
``WorkerConfig`` objects.

**Not operational live evidence.** These fixture strategies are pure test
doubles (the ``skeleton_fixture`` walking-skeleton pattern, spec-labelled
"NOT a trading strategy") — never ``ema_cross_9_21_buy``, never a
placeholder production strategy. Config is written to a scratch
``config_root`` per test (the same convention ``test_config_loader.py``/
``test_dashboard.py`` use), not a static fixture directory, so nothing here
is ever discoverable by the real, committed ``config/`` tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from common.config.paths import ProjectPaths
from common.market_data import RecordedFeedAdapter, load_tick_tape
from common.persistence import Database
from runtimes.intraday_options.__main__ import build_supervisor

RUNTIME_ID = "intraday_options"

GLOBAL_YAML_LIVE_DISABLED = """
global:
  live_trading_enabled: false
  timezone: Asia/Kolkata
runtime_defaults:
  enabled: false
  live_execution_allowed: false
  shared_market_feed: true
strategy_defaults:
  enabled: false
  mode: paper
  live_approved: false
  expiry_policy: force_square_off_before_expiry
"""

RUNTIME_YAML = """
runtime_id: intraday_options
enabled: true
live_execution_allowed: false
shared_market_feed: true
live_preflight:
  expected_static_ip: '203.0.113.10'
  max_preflight_age_seconds: 300
  rate_limits:
    rules:
      # 5/1s = the recommended cited value, not an arbitrary placeholder —
      # see RateLimitRule's own docstring: 50% below Dhan's documented
      # 10 requests/second (dhanhq.co/docs/v2/releases/, Version 2.3),
      # sized against this limiter's fixed-window worst case (2 x limit
      # at a boundary = 10, Dhan's own ceiling, not over it).
      - call_class: new_order
        limit: 5
        window_seconds: 1
  account_risk:
    max_daily_loss: 5000.0
"""

_PAPER_EXECUTION_BLOCK = """
  paper_execution:
    slippage:
      options:
        mode: ticks
        market_order_ticks: 1
    submission_latency_ms: 250
    tick_size: 0.05
    allow_ltp_fallback: true
    ltp_fallback_extra_ticks: 1
    max_quote_age_ms: null
"""

PAPER_FIXTURE_YAML = f"""
strategy_id: paper_fixture_mixed_mode
enabled: true
mode: paper
live_approved: false
engine: trading_engine
risk:
  entry_cutoff: "15:00"
  square_off_at: "15:15"
parameters:
  instrument: NIFTY
  security_id: "99926000"
  quantity: 50
  entry_on_candle: 1
  exit_on_candle: 3
{_PAPER_EXECUTION_BLOCK}
"""

LIVE_FIXTURE_YAML = """
strategy_id: live_fixture_mixed_mode
enabled: true
mode: live
live_approved: true
live_quantity_lots: 1
engine: trading_engine
risk:
  entry_cutoff: "15:00"
  square_off_at: "15:15"
parameters:
  instrument: NIFTY
  security_id: "99926000"
  quantity: 50
  entry_on_candle: 1
  exit_on_candle: 3
"""


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def mixed_mode_config_root(config_root: Path) -> Path:
    _write(config_root / "global.yaml", GLOBAL_YAML_LIVE_DISABLED)
    _write(config_root / "runtimes" / f"{RUNTIME_ID}.yaml", RUNTIME_YAML)
    _write(config_root / "strategies" / "paper_fixture_mixed_mode.yaml", PAPER_FIXTURE_YAML)
    _write(config_root / "strategies" / "live_fixture_mixed_mode.yaml", LIVE_FIXTURE_YAML)
    return config_root


def _paths(tmp_path: Path) -> ProjectPaths:
    paths = ProjectPaths(project_root=tmp_path / "project")
    paths.ensure_writable_dirs()
    return paths


def test_the_live_designated_strategy_is_blocked_and_the_paper_one_admitted(
    mixed_mode_config_root: Path, tmp_path: Path, tick_tape_path: Path
):
    """Requirement #25: with global live disabled, the live-designated
    fixture strategy is blocked rather than rerouted to paper, and the
    paper fixture strategy continues normally — through the real
    build_supervisor/discover_enabled_strategies config path."""
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    paths = _paths(tmp_path)

    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=mixed_mode_config_root,
        paths=paths,
        adapter=adapter,
    )

    result = supervisor.run()

    assert result.workers_started == 1
    assert "paper_fixture_mixed_mode" in result.worker_exit_codes
    assert "live_fixture_mixed_mode" not in result.worker_exit_codes


def test_global_live_disabled_blocks_without_rerouting_to_paper(
    mixed_mode_config_root: Path, tmp_path: Path, tick_tape_path: Path
):
    """No trace of the live-designated strategy in any trading table, and
    no PaperBroker fill ever carries its identity — the negative assertion
    that matters most (spec: never rerouted, never silently demoted)."""
    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    paths = _paths(tmp_path)

    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=mixed_mode_config_root,
        paths=paths,
        adapter=adapter,
    )
    supervisor.run()

    database_path = paths.database_path(RUNTIME_ID)
    conn = Database(database_path).connect()
    for table in ("order_intents", "orders", "fills", "positions", "strategy_state"):
        rows = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE strategy_id = ?",
            ("live_fixture_mixed_mode",),
        ).fetchone()
        assert rows["n"] == 0, f"{table} has a row for the blocked live strategy"


def test_correlation_ids_and_mode_stay_separated_for_the_admitted_paper_strategy(
    mixed_mode_config_root: Path, tmp_path: Path, tick_tape_path: Path
):
    """Every persisted correlation ID for the admitted strategy carries the
    paper (`p_`) namespace — requirement #15/mixed-mode gate's "P&L,
    correlation IDs and positions remain mode-separated"."""
    from common.execution.correlation import is_paper

    adapter = RecordedFeedAdapter(load_tick_tape(tick_tape_path))
    paths = _paths(tmp_path)

    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=mixed_mode_config_root,
        paths=paths,
        adapter=adapter,
    )
    supervisor.run()

    database_path = paths.database_path(RUNTIME_ID)
    conn = Database(database_path).connect()
    rows = conn.execute(
        "SELECT correlation_id, execution_mode FROM order_intents "
        "WHERE strategy_id = ?",
        ("paper_fixture_mixed_mode",),
    ).fetchall()
    assert rows, "the paper fixture should have traded at least once"
    for row in rows:
        assert row["execution_mode"] == "paper"
        assert is_paper(row["correlation_id"])
