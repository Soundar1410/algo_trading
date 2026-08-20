"""orchestration.auto_start.paper_safety: the PAPER-only gate.

The rule these tests protect is the one that makes this facility safe to leave
running unattended: a live-designated strategy **blocks the whole automatic
start**. It is never quietly rerouted into paper and traded anyway.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestration.auto_start import paper_safety as ps
from orchestration.auto_start.retry import TerminalStartupError

GLOBAL_YAML = """\
global:
  live_trading_enabled: {live}
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


def _write_config(
    root: Path,
    *,
    live_trading_enabled: bool = False,
    runtimes: dict[str, dict] | None = None,
    strategies: dict[str, dict] | None = None,
) -> Path:
    config = root / "config"
    (config / "runtimes").mkdir(parents=True, exist_ok=True)
    (config / "strategies").mkdir(parents=True, exist_ok=True)
    (config / "global.yaml").write_text(
        GLOBAL_YAML.format(live=str(live_trading_enabled).lower()), encoding="utf-8"
    )

    for runtime_id, values in (runtimes or {}).items():
        body = "\n".join(f"{k}: {_yaml(v)}" for k, v in values.items())
        (config / "runtimes" / f"{runtime_id}.yaml").write_text(
            f"runtime_id: {runtime_id}\n{body}\n", encoding="utf-8"
        )

    for strategy_id, values in (strategies or {}).items():
        body = "\n".join(f"{k}: {_yaml(v)}" for k, v in values.items())
        (config / "strategies" / f"{strategy_id}.yaml").write_text(
            f"strategy_id: {strategy_id}\n{body}\n", encoding="utf-8"
        )
    return config


def _yaml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


_INTRADAY = {"enabled": True, "live_execution_allowed": False}
_POSITIONAL = {"enabled": True, "live_execution_allowed": False}


def _strategy(runtime_id: str, **overrides) -> dict:
    base = {
        "runtime_id": runtime_id,
        "enabled": True,
        "mode": "paper",
        "live_approved": False,
        "engine": "trading_engine",
    }
    base.update(overrides)
    return base


def _verify(config_root: Path) -> ps.PaperSafetyReport:
    return ps.verify_paper_only(config_root, check_legacy=False, check_environment=False)


# ------------------------------------------------------------------ discovery
def test_enabled_runtimes_and_their_strategies_are_discovered(tmp_path: Path):
    config = _write_config(
        tmp_path,
        runtimes={"intraday_options": _INTRADAY, "positional_options": _POSITIONAL},
        strategies={
            "ema_cross_9_21_buy": _strategy("intraday_options"),
            "weekly_delta_neutral": _strategy(
                "positional_options", engine="positional_multi_leg_engine"
            ),
        },
    )
    report = _verify(config)

    assert report.safe
    assert set(report.runtime_ids) == {"intraday_options", "positional_options"}
    assert set(report.strategy_ids) == {"ema_cross_9_21_buy", "weekly_delta_neutral"}


def test_a_disabled_runtime_is_skipped_entirely(tmp_path: Path):
    config = _write_config(
        tmp_path,
        runtimes={
            "intraday_options": _INTRADAY,
            "positional_options": {"enabled": False, "live_execution_allowed": False},
        },
        strategies={
            "ema_cross_9_21_buy": _strategy("intraday_options"),
            "weekly_delta_neutral": _strategy(
                "positional_options", engine="positional_multi_leg_engine"
            ),
        },
    )
    report = _verify(config)

    assert report.runtime_ids == ("intraday_options",)
    assert "weekly_delta_neutral" not in report.strategy_ids


def test_a_disabled_strategy_stays_disabled(tmp_path: Path):
    config = _write_config(
        tmp_path,
        runtimes={"intraday_options": _INTRADAY},
        strategies={
            "ema_cross_9_21_buy": _strategy("intraday_options"),
            "skeleton_fixture": _strategy("intraday_options", enabled=False),
        },
    )
    report = _verify(config)

    assert report.strategy_ids == ("ema_cross_9_21_buy",)
    assert "skeleton_fixture" not in report.strategy_ids


def test_no_fixture_strategy_is_ever_enabled_automatically(tmp_path: Path):
    """Discovery reads config; it has no power to turn anything on."""
    config = _write_config(
        tmp_path,
        runtimes={"intraday_options": _INTRADAY},
        strategies={"skeleton_fixture": _strategy("intraday_options", enabled=False)},
    )
    report = _verify(config)
    assert report.strategy_ids == ()


def test_the_config_lists_no_strategy_ids_of_its_own(tmp_path: Path):
    """auto_start config must never become a second enablement authority."""
    from common.config import AutoStartConfig

    fields = set(AutoStartConfig.model_fields)
    assert not {f for f in fields if "strategy" in f or "runtime_id" in f}


# ----------------------------------------------------------------- violations
def test_a_global_live_switch_blocks_everything(tmp_path: Path):
    config = _write_config(
        tmp_path,
        live_trading_enabled=True,
        runtimes={"intraday_options": _INTRADAY},
        strategies={"ema_cross_9_21_buy": _strategy("intraday_options")},
    )
    report = _verify(config)

    assert not report.safe
    assert any("live_trading_enabled" in v for v in report.violations)


def test_a_runtime_permitting_live_execution_blocks_everything(tmp_path: Path):
    config = _write_config(
        tmp_path,
        runtimes={"intraday_options": {"enabled": True, "live_execution_allowed": True}},
        strategies={"ema_cross_9_21_buy": _strategy("intraday_options")},
    )
    report = _verify(config)

    assert not report.safe
    assert any("live_execution_allowed" in v for v in report.violations)


def test_a_live_strategy_blocks_the_start_and_is_never_rerouted_to_paper(tmp_path: Path):
    """The most important assertion in this file.

    A ``mode: live`` strategy stops the *whole* automatic start. It is never
    downgraded to paper and traded anyway. Note that the strict config models
    refuse this shape before the mode check even runs — a live strategy must
    declare a quantity, a static IP, an egress provider, rate limits and
    account-risk bounds — so the refusal here is layered, not single-point.
    """
    config = _write_config(
        tmp_path,
        runtimes={"intraday_options": _INTRADAY},
        strategies={
            "ema_cross_9_21_buy": _strategy(
                "intraday_options", mode="live", live_quantity_lots=1
            )
        },
    )
    report = _verify(config)

    assert not report.safe
    assert report.strategy_ids == (), "nothing live may appear in the start plan"
    with pytest.raises(TerminalStartupError):
        report.raise_if_unsafe()


def test_a_strategy_config_that_will_not_load_blocks_rather_than_crashes(tmp_path: Path):
    """The refusal must be a reported violation, not an unhandled exception."""
    config = _write_config(
        tmp_path,
        runtimes={"intraday_options": _INTRADAY},
        strategies={"ema_cross_9_21_buy": _strategy("intraday_options", mode="live")},
    )
    report = _verify(config)

    assert not report.safe
    assert any("unloadable strategy config" in v for v in report.violations)


def test_a_live_approved_strategy_blocks_the_start(tmp_path: Path):
    config = _write_config(
        tmp_path,
        runtimes={"intraday_options": _INTRADAY},
        strategies={"ema_cross_9_21_buy": _strategy("intraday_options", live_approved=True)},
    )
    report = _verify(config)

    assert not report.safe
    assert any("live_approved" in v for v in report.violations)


def test_a_violation_is_terminal_never_retried(tmp_path: Path):
    from orchestration.auto_start.retry import Retryability, classify

    config = _write_config(
        tmp_path,
        live_trading_enabled=True,
        runtimes={"intraday_options": _INTRADAY},
        strategies={"ema_cross_9_21_buy": _strategy("intraday_options")},
    )
    try:
        _verify(config).raise_if_unsafe()
    except TerminalStartupError as exc:
        assert classify(exc) is Retryability.TERMINAL
    else:  # pragma: no cover
        pytest.fail("expected a refusal")


def test_a_safe_report_raises_nothing(tmp_path: Path):
    config = _write_config(
        tmp_path,
        runtimes={"intraday_options": _INTRADAY},
        strategies={"ema_cross_9_21_buy": _strategy("intraday_options")},
    )
    _verify(config).raise_if_unsafe()  # must not raise


# --------------------------------------------------------------- legacy guard
def test_an_active_legacy_system_blocks_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class _Legacy:
        active = True
        undetermined = False

        def describe(self) -> str:
            return "launchd: loaded"

    monkeypatch.setattr(ps, "legacy_system_status", lambda: _Legacy())
    config = _write_config(
        tmp_path,
        runtimes={"intraday_options": _INTRADAY},
        strategies={"ema_cross_9_21_buy": _strategy("intraday_options")},
    )
    report = ps.verify_paper_only(config, check_legacy=True, check_environment=False)

    assert not report.safe
    violation = next(v for v in report.violations if "legacy" in v)
    assert "launchctl bootout" in violation, "the manual command must be reported"


def test_an_undetermined_legacy_state_also_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Fail-closed: 'could not verify' is not 'not present'."""

    class _Legacy:
        active = True
        undetermined = True

        def describe(self) -> str:
            return "unknown"

    monkeypatch.setattr(ps, "legacy_system_status", lambda: _Legacy())
    config = _write_config(
        tmp_path,
        runtimes={"intraday_options": _INTRADAY},
        strategies={"ema_cross_9_21_buy": _strategy("intraday_options")},
    )
    report = ps.verify_paper_only(config, check_legacy=True, check_environment=False)

    assert not report.safe
    assert any("could not be verified" in v for v in report.violations)


# ------------------------------------------------------- environment validation
def test_a_failing_environment_check_blocks_that_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        ps.validate_environment, "main", lambda argv: ps.validate_environment.EXIT_PROBLEMS
    )
    config = _write_config(
        tmp_path,
        runtimes={"intraday_options": _INTRADAY},
        strategies={"ema_cross_9_21_buy": _strategy("intraday_options")},
    )
    report = ps.verify_paper_only(config, check_legacy=False, check_environment=True)

    assert not report.safe
    assert any("validate_environment" in v for v in report.violations)


def test_the_known_runtimes_come_from_the_one_registry():
    """A third runtime must be one registry entry, not a branch here."""
    from scripts._runtimes import RUNTIMES

    assert set(ps._known_runtime_ids()) == set(RUNTIMES)
