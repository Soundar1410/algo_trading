"""orchestration.auto_start.gate: the trading-day and start-window decision.

Every case here is decided against an explicit, timezone-aware ``now`` rather
than the real clock, so the suite says the same thing at any hour of any day.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from common.config import AutoStartConfig
from orchestration.auto_start import gate

IST = ZoneInfo("Asia/Kolkata")


def _cfg(**overrides) -> AutoStartConfig:
    base = {"enabled": True, "require_system_timezone_match": False}
    base.update(overrides)
    return AutoStartConfig(**base)


def _at(hour: int, minute: int, *, day: int = 20) -> datetime:
    """A Thursday in August 2026 unless a different day is asked for."""
    return datetime(2026, 8, day, hour, minute, tzinfo=IST)


def _decide(cfg: AutoStartConfig, now: datetime) -> gate.StartDecision:
    return gate.evaluate_start_window(
        cfg, now=now, session=gate.build_session(cfg), check_system_timezone=False
    )


# --------------------------------------------------------------- the boundary
def test_0859_does_not_start():
    decision = _decide(_cfg(), _at(8, 59))
    assert not decision.eligible
    assert not decision.terminal  # an early login is not an error
    assert "before the configured start" in decision.reason


def test_0900_exactly_is_eligible():
    assert _decide(_cfg(), _at(9, 0)).eligible


def test_0910_late_login_is_eligible():
    """RunAtLoad after the calendar trigger has already fired."""
    assert _decide(_cfg(), _at(9, 10)).eligible


def test_1100_starts_so_a_positional_runtime_can_recover_existing_exposure():
    """The correction that matters most for weekly_delta_neutral.

    A Mac booted at 11:00 with an open weekly cycle must still start the
    positional runtime so it can manage that exposure. Whether a *new* entry is
    allowed is the strategy's own entry-cutoff decision, made later and
    elsewhere — the infrastructure must not pre-empt it by refusing to start.
    """
    assert _decide(_cfg(), _at(11, 0)).eligible


def test_the_shipped_default_latest_start_is_the_session_deadline():
    """A narrow late-start window would silently strand open positions."""
    cfg = AutoStartConfig()
    assert cfg.latest_start_time == cfg.session_deadline_time


def test_after_the_session_deadline_no_new_startup():
    decision = _decide(_cfg(), _at(15, 16))
    assert not decision.eligible
    assert "past the latest start" in decision.reason


def test_an_operator_may_narrow_the_late_start_window():
    cfg = _cfg(latest_start_time="10:30")
    assert _decide(cfg, _at(10, 29)).eligible
    assert not _decide(cfg, _at(10, 31)).eligible


# ---------------------------------------------------------------- the calendar
def test_a_weekend_is_not_a_trading_day():
    saturday = _at(9, 30, day=22)
    assert saturday.weekday() == 5
    decision = _decide(_cfg(), saturday)
    assert not decision.eligible
    assert not decision.terminal
    assert "not a trading day" in decision.reason


def test_a_configured_holiday_is_refused_by_name():
    cfg = _cfg(holidays=("2026-08-20",))
    decision = _decide(cfg, _at(9, 30))
    assert not decision.eligible
    assert "holiday" in decision.reason


def test_holidays_come_from_market_session_not_a_second_copy():
    """The gate must not reimplement the calendar it shares with the engine."""
    cfg = _cfg(holidays=("2026-08-20",))
    session = gate.build_session(cfg)
    assert session.is_holiday(_at(9, 30))
    assert not session.is_trading_day(_at(9, 30))


def test_disabled_config_is_never_eligible():
    assert not _decide(_cfg(enabled=False), _at(9, 30)).eligible


# ---------------------------------------------------------------- the timezone
def test_a_timezone_mismatch_fails_closed(monkeypatch: pytest.MonkeyPatch):
    """launchd fires in the Mac's local zone; a mismatch means the 09:00
    trigger is not the configured 09:00. Refuse rather than trade on it."""
    monkeypatch.setattr(gate, "system_timezone_matches", lambda *a, **k: False)
    monkeypatch.setattr(gate, "system_timezone_name", lambda: "America/New_York")
    cfg = _cfg(require_system_timezone_match=True)
    decision = gate.evaluate_start_window(cfg, now=_at(9, 30), session=gate.build_session(cfg))
    assert not decision.eligible
    assert decision.terminal
    assert "does not match" in decision.reason


def test_a_timezone_mismatch_can_be_deliberately_accepted(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gate, "system_timezone_matches", lambda *a, **k: False)
    cfg = _cfg(require_system_timezone_match=False)
    assert gate.evaluate_start_window(
        cfg, now=_at(9, 30), session=gate.build_session(cfg)
    ).eligible


def test_a_matching_offset_counts_even_when_the_zone_name_differs(monkeypatch: pytest.MonkeyPatch):
    """Asia/Calcutta is Asia/Kolkata. A name comparison alone would be wrong."""
    monkeypatch.setattr(gate, "system_timezone_name", lambda: "Asia/Calcutta")
    assert gate.system_timezone_matches("Asia/Kolkata", at=_at(9, 0))


def test_the_env_TZ_wins_when_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    assert gate.system_timezone_name() == "Asia/Kolkata"


# ----------------------------------------------------------------- the deadline
def test_the_deadline_lands_on_todays_date_in_the_configured_zone():
    cfg = _cfg(session_deadline_time="15:15")
    deadline = gate.session_deadline(cfg, now=_at(9, 30))
    assert deadline.hour == 15
    assert deadline.minute == 15
    assert deadline.date() == _at(9, 30).date()


def test_a_naive_now_is_refused_rather_than_guessed_at():
    from common.utils.timeutils import NaiveDatetimeError

    with pytest.raises(NaiveDatetimeError):
        _decide(_cfg(), datetime(2026, 8, 20, 9, 30))
