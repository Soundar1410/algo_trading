"""``runtimes.intraday_options.__main__.build_supervisor``'s strategy filter.

Phase 7 Part 4's ``scripts/start_strategy.py`` needs a supervisor that admits
exactly one strategy while still going through the same admission path an
unfiltered start does — the spec is explicit that a bare worker is never
spawned outside a supervisor. ``build_supervisor`` itself had no test before
Part 4 added ``strategy_ids``; these are the first.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from common.config import load_paths
from common.market_data import RecordedFeedAdapter, load_tick_tape
from runtimes.intraday_options.__main__ import build_supervisor

RUNTIME_ID = "intraday_options"

GLOBAL_YAML = """
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
  risk:
    max_lots: 1
    max_daily_loss: 5000
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _strategy_yaml(strategy_id: str, security_id: str) -> str:
    return (
        f"strategy_id: {strategy_id}\n"
        "enabled: true\n"
        "parameters:\n"
        "  instrument: NIFTY\n"
        f"  security_id: \"{security_id}\"\n"
    )


@pytest.fixture
def populated_config(config_root: Path) -> Path:
    _write(config_root / "global.yaml", GLOBAL_YAML)
    _write(
        config_root / "runtimes" / f"{RUNTIME_ID}.yaml",
        f"runtime_id: {RUNTIME_ID}\nenabled: true\n",
    )
    _write(config_root / "strategies" / "io_alpha.yaml", _strategy_yaml("io_alpha", "111"))
    _write(config_root / "strategies" / "io_bravo.yaml", _strategy_yaml("io_bravo", "222"))
    return config_root


@pytest.fixture
def adapter(tick_tape_path: Path) -> RecordedFeedAdapter:
    return RecordedFeedAdapter(load_tick_tape(tick_tape_path))


def _admitted_ids(supervisor) -> set[str]:
    # No public accessor exists for "what got admitted" — build_supervisor's
    # only externally visible effect before .run() is this private list, and
    # adding a public one for a single test would outrun what anything else
    # needs.
    return {config.strategy_id for config, _ in supervisor._workers}


def test_with_no_filter_every_enabled_strategy_is_admitted(populated_config, adapter, tmp_path):
    paths = load_paths(tmp_path)
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID, config_root=populated_config, paths=paths, adapter=adapter
    )
    assert _admitted_ids(supervisor) == {"io_alpha", "io_bravo"}


def test_a_strategy_id_filter_admits_only_that_one(populated_config, adapter, tmp_path):
    paths = load_paths(tmp_path)
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=populated_config,
        paths=paths,
        adapter=adapter,
        strategy_ids=frozenset({"io_bravo"}),
    )
    assert _admitted_ids(supervisor) == {"io_bravo"}


def test_a_filter_matching_nothing_admits_nothing_not_an_error(populated_config, adapter, tmp_path):
    paths = load_paths(tmp_path)
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=populated_config,
        paths=paths,
        adapter=adapter,
        strategy_ids=frozenset({"does_not_exist"}),
    )
    assert _admitted_ids(supervisor) == set()
