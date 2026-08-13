"""scripts/assert_no_live_config_committed.py — the commit/CI-time guard
against a live-enabling value slipping into config/. Every check is a
narrow, deliberate mirror of CLAUDE.md's own rule text."""

from __future__ import annotations

from pathlib import Path

from scripts.assert_no_live_config_committed import main

GLOBAL_SAFE = """
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


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_the_real_committed_config_tree_passes():
    """The actual config/ this repo ships — the guard this script exists
    to enforce, run against the real thing."""
    repo_root = Path(__file__).resolve().parents[2]
    assert main([str(repo_root / "config")]) == 0


def test_a_clean_synthetic_tree_passes(tmp_path: Path):
    _write(tmp_path / "global.yaml", GLOBAL_SAFE)
    _write(
        tmp_path / "strategies" / "s1.yaml",
        "strategy_id: s1\nenabled: true\nmode: paper\nlive_approved: false\n",
    )
    assert main([str(tmp_path)]) == 0


def test_global_live_trading_enabled_true_is_caught(tmp_path: Path, capsys):
    _write(
        tmp_path / "global.yaml",
        "global:\n  live_trading_enabled: true\n  timezone: Asia/Kolkata\n",
    )
    assert main([str(tmp_path)]) == 1
    assert "global.live_trading_enabled is true" in capsys.readouterr().out


def test_runtime_defaults_live_execution_allowed_true_is_caught(tmp_path: Path, capsys):
    _write(
        tmp_path / "global.yaml",
        "global:\n  live_trading_enabled: false\nruntime_defaults:\n"
        "  live_execution_allowed: true\n",
    )
    assert main([str(tmp_path)]) == 1
    assert "runtime_defaults.live_execution_allowed is true" in capsys.readouterr().out


def test_strategy_defaults_mode_live_is_caught(tmp_path: Path, capsys):
    _write(
        tmp_path / "global.yaml",
        "global:\n  live_trading_enabled: false\nstrategy_defaults:\n  mode: live\n",
    )
    assert main([str(tmp_path)]) == 1
    assert "strategy_defaults.mode is 'live'" in capsys.readouterr().out


def test_a_runtime_file_with_live_execution_allowed_true_is_caught(tmp_path: Path, capsys):
    _write(tmp_path / "global.yaml", GLOBAL_SAFE)
    _write(
        tmp_path / "runtimes" / "intraday_options.yaml",
        "runtime_id: intraday_options\nenabled: true\nlive_execution_allowed: true\n",
    )
    assert main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "runtimes/intraday_options.yaml" in out
    assert "live_execution_allowed is true" in out


def test_a_disabled_strategy_with_mode_live_is_still_caught(tmp_path: Path, capsys):
    """The rule (CLAUDE.md, verbatim) forbids `mode: live` in committed YAML
    outright — `enabled: false` does not exempt it."""
    _write(tmp_path / "global.yaml", GLOBAL_SAFE)
    _write(
        tmp_path / "strategies" / "future.yaml",
        "strategy_id: future\nenabled: false\nmode: live\nlive_approved: false\n",
    )
    assert main([str(tmp_path)]) == 1
    assert "mode is 'live'" in capsys.readouterr().out


def test_a_strategy_with_live_approved_true_is_caught(tmp_path: Path, capsys):
    _write(tmp_path / "global.yaml", GLOBAL_SAFE)
    _write(
        tmp_path / "strategies" / "s1.yaml",
        "strategy_id: s1\nenabled: true\nmode: paper\nlive_approved: true\n",
    )
    assert main([str(tmp_path)]) == 1
    assert "live_approved is true" in capsys.readouterr().out


def test_multiple_problems_are_all_reported_not_just_the_first(tmp_path: Path, capsys):
    _write(
        tmp_path / "global.yaml",
        "global:\n  live_trading_enabled: true\n",
    )
    _write(
        tmp_path / "strategies" / "s1.yaml",
        "strategy_id: s1\nenabled: true\nmode: live\nlive_approved: true\n",
    )
    assert main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "3 live-config problem(s) found" in out


def test_a_missing_global_yaml_is_a_problem_not_a_silent_pass(tmp_path: Path, capsys):
    assert main([str(tmp_path)]) == 1
    assert "not found" in capsys.readouterr().out


def test_unreadable_yaml_is_a_problem_not_a_silent_skip(tmp_path: Path, capsys):
    _write(tmp_path / "global.yaml", GLOBAL_SAFE)
    _write(tmp_path / "strategies" / "broken.yaml", "not: [valid: yaml: at: all")
    assert main([str(tmp_path)]) == 1
    assert "could not be read" in capsys.readouterr().out


def test_no_strategies_or_runtimes_directory_is_not_itself_a_problem(tmp_path: Path):
    """An otherwise-empty config tree (no runtimes/, no strategies/) has
    nothing live to flag — only global.yaml is required."""
    _write(tmp_path / "global.yaml", GLOBAL_SAFE)
    assert main([str(tmp_path)]) == 0
