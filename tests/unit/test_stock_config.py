"""Phase 5 Part B1: the committed config pair, bound strictly, changes nothing
for the existing runtimes.

* ``RunConfig.from_config`` loads exactly today's ``RulesParameters`` defaults,
  the v1.3 schedule and both ``enabled: true`` flags (go-live, D150); any unknown, missing,
  mistyped or non-configurable value refuses the run.
* ``discover_strategies`` for ``intraday_options`` and ``positional_options``,
  ``RUNTIMES``, the paper-safety check, every dashboard reader of
  ``config/`` and ``validate_environment`` see exactly what they saw before
  the two files existed — compared on a copy of ``config/`` with and without
  them, not asserted from a snapshot.
* The live-config guard covers both new files.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from common.config import Settings, discover_enabled_strategies, discover_strategies
from runtimes.positional_stocks.run_config import RunConfig, RunRefused, Schedule
from scripts import assert_no_live_config_committed
from strategies.positional_stocks.wsr1_weekly_stochrsi.models import RulesParameters

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "config"
RUNTIME_FILE = Path("runtimes") / "positional_stocks.yaml"
STRATEGY_FILE = Path("strategies") / "positional_stocks" / "wsr1_weekly_stochrsi.yaml"
EXISTING = ("intraday_options", "positional_options")


@pytest.fixture
def with_and_without(tmp_path: Path) -> tuple[Path, Path]:
    """Two copies of the committed ``config/``: as it is, and without the pair."""
    with_pair = tmp_path / "with" / "config"
    without = tmp_path / "without" / "config"
    shutil.copytree(CONFIG, with_pair)
    shutil.copytree(CONFIG, without)
    (without / RUNTIME_FILE).unlink()
    (without / STRATEGY_FILE).unlink()
    (without / STRATEGY_FILE).parent.rmdir()
    assert (with_pair / STRATEGY_FILE).is_file()
    return with_pair, without


# ================================================================== binding
def test_the_committed_values_are_todays_defaults_and_the_v1_3_schedule() -> None:
    config = RunConfig.from_config(CONFIG)
    assert config.params == RulesParameters()
    assert config.schedule == Schedule()
    assert [s.text() for s in config.schedule.fetch_attempts] == [
        "SATURDAY 08:00",
        "SATURDAY 14:00",
        "SUNDAY 10:00",
    ]
    assert config.schedule.decide.text() == "MONDAY 08:30"
    assert (config.runtime_enabled, config.strategy_enabled) == (True, True)
    assert config.mode == "paper" and config.index_symbol == "NIFTY"
    assert (config.decide_deadline_minutes, config.fetch_deadline_minutes) == (5.0, 20.0)
    assert config.disabled_reason() is None


def _edit(root: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    path = root / STRATEGY_FILE
    data = yaml.safe_load(path.read_text())
    edit(data["parameters"])
    path.write_text(yaml.safe_dump(data))


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda p: p.__setitem__("surprise", 1), "unknown parameters key(s): ['surprise']"),
        (lambda p: p["stoch"].__setitem__("k", 5), "stoch.k is 5; this spec version implements"),
        (lambda p: p["filters"].__setitem__("rs_weeks", 13), "filters.rs_weeks is 13"),
        (lambda p: p["regime"].__setitem__("index", "NIFTY 100"), "regime.index is 'NIFTY 100'"),
        (lambda p: p.pop("capital"), "parameters.capital is missing"),
        (lambda p: p.__setitem__("max_positions", 2.5), "max_positions must be a whole number"),
        (lambda p: p["sizing"].__setitem__("tranches_pct", [50, 30, 30]), "must sum to 100"),
        (lambda p: p["schedule"].__setitem__("decide", "MONDAY"), "schedule slot must be like"),
        (
            lambda p: p["deadlines"].__setitem__("extra", 1),
            "unknown key(s) in parameters.deadlines",
        ),
        (
            lambda p: p["deadlines"].__setitem__("fetch_requests_per_second", 5),
            "fetch_requests_per_second is 5",
        ),
    ],
)
def test_the_binding_is_strict(
    with_and_without: tuple[Path, Path], edit: Callable[[dict[str, Any]], None], message: str
) -> None:
    root, _ = with_and_without
    _edit(root, edit)
    with pytest.raises(RunRefused, match="configuration") as caught:
        RunConfig.from_config(root)
    assert message in str(caught.value)


def test_a_configurable_value_is_taken_from_the_yaml(with_and_without: tuple[Path, Path]) -> None:
    root, _ = with_and_without
    _edit(root, lambda p: p["costs"].__setitem__("cost_bps_buy", 15))
    assert str(RunConfig.from_config(root).params.cost_bps_buy) == "15"


def test_an_unbindable_config_refuses_the_run_with_a_report(tmp_path: Path) -> None:
    from _stock_run_fixtures import Root

    from runtimes.positional_stocks.weekly_run import EXIT_REFUSED

    root = Root.create(tmp_path)
    root.standard_cache()
    assert root.run("--as-of", "auto", config_error="configuration: bad") == EXIT_REFUSED
    assert root.output[0] == "REFUSED: configuration: bad"
    assert len(list(root.reports.glob("*-refused.md"))) == 1


# ====================================================== existing runtimes
@pytest.mark.parametrize("runtime_id", EXISTING)
def test_strategy_discovery_for_the_existing_runtimes_is_unchanged(
    with_and_without: tuple[Path, Path], runtime_id: str
) -> None:
    with_pair, without = with_and_without
    settings = Settings()
    for discover in (discover_strategies, discover_enabled_strategies):
        before = discover(without, runtime_id, settings=settings)
        after = discover(with_pair, runtime_id, settings=settings)
        assert after == before and after, f"{discover.__name__}({runtime_id}) changed"
        assert all(c.strategy.runtime_id == runtime_id for c in after)


def test_the_new_runtime_is_invisible_to_auto_start_and_paper_safety() -> None:
    from orchestration.auto_start.paper_safety import verify_paper_only
    from scripts._runtimes import RUNTIMES

    assert "positional_stocks" not in RUNTIMES
    report = verify_paper_only(CONFIG, check_legacy=False, check_environment=False)
    assert not report.violations
    assert "positional_stocks" not in {plan.runtime_id for plan in report.plans}
    assert "wsr1_weekly_stochrsi" not in report.strategy_ids


def test_the_live_config_guard_covers_both_new_files(with_and_without: tuple[Path, Path]) -> None:
    root, _ = with_and_without
    ok = assert_no_live_config_committed.EXIT_OK
    assert assert_no_live_config_committed.main([str(root)]) == ok
    for relative, key, value in (
        (STRATEGY_FILE, "mode", "live"),
        (STRATEGY_FILE, "live_approved", True),
        (RUNTIME_FILE, "live_execution_allowed", True),
    ):
        path = root / relative
        original = path.read_text()
        data = yaml.safe_load(original)
        data[key] = value
        path.write_text(yaml.safe_dump(data))
        assert assert_no_live_config_committed.main([str(root)]) != ok, f"{relative}: {key}"
        path.write_text(original)


def test_validate_environment_reads_no_runtime_or_strategy_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``validate_environment`` checks paths, disk, time, the legacy agent,
    credentials and the runtime's database — never a runtime or strategy
    YAML — so the new pair cannot change its result for any runtime id."""
    import common.config.loader as loader
    from common.process import LaunchdLabelState, LegacySystemStatus
    from scripts import validate_environment

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("validate_environment read a config file")

    for name in ("_read_yaml", "load_runtime_config", "load_strategy_config"):
        monkeypatch.setattr(loader, name, forbidden)
    monkeypatch.setattr(
        validate_environment,
        "legacy_system_status",
        lambda: LegacySystemStatus(LaunchdLabelState.INACTIVE, "", False, ""),
    )
    for runtime_id in (*EXISTING, "positional_stocks"):
        validate_environment.main(["--runtime-id", runtime_id])
        assert f"Database ({runtime_id})" in capsys.readouterr().out


# ================================================================ dashboards
def _dashboard_views(root: Path, runtime_id: str) -> dict[str, object]:
    from dashboards.data.account import (
        _raw_strategy_files,
        load_live_gate_matrix,
        load_live_gate_status,
    )
    from dashboards.data.intraday_options import load_capital_base, load_strategy_config_raw
    from dashboards.data.strategy_scope import discover_strategy_options

    settings = Settings()
    raw = _raw_strategy_files(root, runtime_id=runtime_id)
    return {
        "raw_strategy_files": raw,
        "live_gate_status": load_live_gate_status(root, runtime_id, settings),
        "live_gate_matrix": load_live_gate_matrix(root, runtime_id, settings),
        "strategy_options": discover_strategy_options(None, root, runtime_id, settings),
        "raw_configs": {
            str(d["strategy_id"]): load_strategy_config_raw(root, str(d["strategy_id"]))
            for d in raw
        },
        "capital_bases": {
            str(d["strategy_id"]): load_capital_base(root, str(d["strategy_id"])) for d in raw
        },
    }


@pytest.mark.parametrize("runtime_id", EXISTING)
def test_every_dashboard_reader_of_config_is_unchanged(
    with_and_without: tuple[Path, Path], runtime_id: str
) -> None:
    with_pair, without = with_and_without
    before = _dashboard_views(without, runtime_id)
    after = _dashboard_views(with_pair, runtime_id)
    assert after == before
    assert before["raw_strategy_files"], "the comparison must not be vacuous"


def test_no_dashboard_reader_raises_on_the_new_files(
    with_and_without: tuple[Path, Path],
) -> None:
    from dashboards.data.account import _raw_strategy_files
    from dashboards.data.intraday_options import load_strategy_config_raw

    root, _ = with_and_without
    views = _dashboard_views(root, "positional_stocks")
    assert [d["strategy_id"] for d in views["raw_strategy_files"]] == ["wsr1_weekly_stochrsi"]  # type: ignore[index]
    everything = _raw_strategy_files(root)
    assert "wsr1_weekly_stochrsi" in {d["strategy_id"] for d in everything}
    assert load_strategy_config_raw(root, "wsr1_weekly_stochrsi") is not None
    # Home's category cards read only these runtime ids, unchanged.
    from dashboards.Home import _CATEGORIES

    assert "positional_stocks" not in {runtime_id for _, runtime_id, _, _ in _CATEGORIES}
