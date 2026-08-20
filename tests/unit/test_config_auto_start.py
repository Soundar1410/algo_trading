"""common.config: the typed auto_start block.

Strict and fail-closed like every other section — a typo is a load error, not
a silently ignored key, because a mistyped ``latest_start_time`` that fell back
to a default would change when an unattended machine starts trading.
"""

from __future__ import annotations

from datetime import date
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


# ------------------------------------------------------- the committed calendar
def _committed_holidays() -> list[date]:
    return [date.fromisoformat(d) for d in load_auto_start_config(REPO_CONFIG).holidays]


def test_every_committed_holiday_is_a_parseable_iso_date():
    """A typo here silently changes when an unattended machine trades."""
    for raw in load_auto_start_config(REPO_CONFIG).holidays:
        assert date.fromisoformat(raw).isoformat() == raw, f"{raw} is not a plain ISO date"


def test_no_committed_holiday_falls_on_a_weekend():
    """MarketSession already excludes Saturday and Sunday, so a weekend entry
    is dead weight — and much more likely to be a transcription error than a
    real exchange closure worth recording."""
    for day in _committed_holidays():
        assert day.weekday() < 5, f"{day} is a {day.strftime('%A')}"


def test_the_committed_holidays_are_unique_and_sorted():
    days = _committed_holidays()
    assert len(days) == len(set(days)), "a duplicated date hides an editing mistake"
    assert days == sorted(days), "keep the list in date order so gaps are visible"


def test_the_committed_holidays_cover_the_expected_year():
    days = _committed_holidays()
    assert days, "an empty calendar means weekends only — populate it before enabling"
    assert {d.year for d in days} == {2026}, "one calendar year per committed list"


def test_a_committed_holiday_is_actually_treated_as_a_non_trading_day():
    """End to end through the real gate, not just the parsed config."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from orchestration.auto_start.gate import build_session, evaluate_start_window

    cfg = load_auto_start_config(REPO_CONFIG).model_copy(
        update={"enabled": True, "require_system_timezone_match": False}
    )
    session = build_session(cfg)
    ist = ZoneInfo(cfg.timezone)

    for day in _committed_holidays():
        moment = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ist)
        assert session.is_holiday(moment), f"{day} is not seen as a holiday"
        decision = evaluate_start_window(
            cfg, now=moment, session=session, check_system_timezone=False
        )
        assert not decision.eligible, f"{day} would still have started trading"
        assert not decision.terminal, f"{day} must be a quiet exit, not an alert"


def test_an_ordinary_weekday_is_still_a_trading_day():
    """The negative control: the calendar must not blanket-disable the year."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from orchestration.auto_start.gate import build_session, evaluate_start_window

    cfg = load_auto_start_config(REPO_CONFIG).model_copy(
        update={"enabled": True, "require_system_timezone_match": False}
    )
    session = build_session(cfg)
    # A Thursday with no holiday anywhere near it.
    moment = datetime(2026, 9, 17, 9, 30, tzinfo=ZoneInfo(cfg.timezone))
    assert moment.strftime("%A") == "Thursday"
    assert evaluate_start_window(
        cfg, now=moment, session=session, check_system_timezone=False
    ).eligible


def test_the_sunday_muhurat_session_is_not_a_trading_day():
    """Diwali Muhurat 2026-11-08 is a one-hour Sunday evening special. A 09:00
    unattended start must not attempt it, and the weekend rule already says so
    without the date being listed."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from orchestration.auto_start.gate import build_session

    cfg = load_auto_start_config(REPO_CONFIG)
    muhurat = datetime(2026, 11, 8, 9, 30, tzinfo=ZoneInfo(cfg.timezone))
    assert muhurat.strftime("%A") == "Sunday"
    assert not build_session(cfg).is_trading_day(muhurat)
