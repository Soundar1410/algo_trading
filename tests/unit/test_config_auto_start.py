"""common.config: the typed auto_start block.

Strict and fail-closed like every other section — a typo is a load error, not
a silently ignored key, because a mistyped ``latest_start_time`` that fell back
to a default would change when an unattended machine starts trading.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from common.config import AutoStartConfig, ConfigError, load_auto_start_config

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


# ----------------------------------------------------------------- the defaults
def test_auto_start_is_disabled_by_default():
    assert AutoStartConfig().enabled is False


def test_the_committed_config_ships_disabled():
    """Installing the LaunchAgent and enabling trading are separate steps."""
    assert load_auto_start_config(REPO_CONFIG).enabled is False


def test_the_defaults_are_ist_and_0900():
    cfg = AutoStartConfig()
    assert cfg.timezone == "Asia/Kolkata"
    assert cfg.startup_time == "09:00"


def test_a_timezone_mismatch_is_refused_by_default():
    assert AutoStartConfig().require_system_timezone_match is True


def test_late_start_runs_to_the_session_deadline_by_default():
    """A positional runtime with open exposure must be recoverable late."""
    cfg = AutoStartConfig()
    assert cfg.latest_start_time == cfg.session_deadline_time == "15:15"


def test_the_dashboard_flag_exists_and_defaults_on():
    assert AutoStartConfig().dashboard_auto_start is True


# ------------------------------------------------------------------ strictness
def test_an_unknown_key_is_refused_rather_than_ignored():
    with pytest.raises(ValidationError):
        AutoStartConfig(startup_tyme="09:00")


def test_the_block_names_no_strategy_or_runtime():
    """Runtime and strategy YAML enabled flags stay the only authority."""
    fields = set(AutoStartConfig.model_fields)
    assert not [f for f in fields if "strategy" in f or "runtime_id" in f]


def test_an_out_of_order_window_is_refused():
    with pytest.raises(ValidationError, match="startup_time <= latest_start_time"):
        AutoStartConfig(startup_time="10:00", latest_start_time="09:00")


def test_a_latest_start_after_the_deadline_is_refused():
    with pytest.raises(ValidationError, match="startup_time <= latest_start_time"):
        AutoStartConfig(latest_start_time="16:00", session_deadline_time="15:15")


def test_an_unparseable_time_is_refused():
    with pytest.raises(ValidationError):
        AutoStartConfig(startup_time="nine o'clock")


def test_an_unknown_timezone_is_refused():
    with pytest.raises(ValidationError, match="Unknown IANA timezone"):
        AutoStartConfig(timezone="Mars/Olympus_Mons")


def test_a_non_positive_retry_interval_is_refused():
    """A zero interval would be a busy loop against Dhan."""
    with pytest.raises(ValidationError):
        AutoStartConfig(retry_interval_seconds=0)


def test_a_backoff_cap_below_the_base_interval_is_refused():
    with pytest.raises(ValidationError, match="retry_max_interval_seconds"):
        AutoStartConfig(retry_interval_seconds=300.0, retry_max_interval_seconds=30.0)


def test_the_model_is_frozen():
    cfg = AutoStartConfig()
    with pytest.raises(ValidationError):
        cfg.enabled = True


# --------------------------------------------------------------------- loading
def test_an_absent_block_falls_back_to_the_safe_defaults(tmp_path: Path):
    (tmp_path / "global.yaml").write_text("global:\n  timezone: Asia/Kolkata\n", encoding="utf-8")
    cfg = load_auto_start_config(tmp_path)
    assert cfg.enabled is False


def test_a_non_mapping_block_is_a_config_error(tmp_path: Path):
    (tmp_path / "global.yaml").write_text("auto_start: [1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_auto_start_config(tmp_path)


def test_a_typo_in_the_yaml_block_fails_at_load(tmp_path: Path):
    (tmp_path / "global.yaml").write_text(
        "auto_start:\n  enabled: true\n  startup_tyme: '09:00'\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_auto_start_config(tmp_path)


def test_the_committed_holiday_list_is_readable_and_typed():
    cfg = load_auto_start_config(REPO_CONFIG)
    assert isinstance(cfg.holidays, tuple)
