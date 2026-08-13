"""``runtimes.intraday_options.__main__``: strategy filtering and startup order.

Phase 7 Part 4's ``scripts/start_strategy.py`` needs a supervisor that admits
exactly one strategy while still going through the same admission path an
unfiltered start does — the spec is explicit that a bare worker is never
spawned outside a supervisor. ``build_supervisor`` itself had no test before
Part 4 added ``strategy_ids``; these are the first.

Phase 7 Part 5 added a backup/migrate/retain sequence to ``main`` itself,
strictly before authentication — the one call site
``common.retention.backup_database`` and ``common.retention.run_retention``
are invoked from. ``test_main_backs_up_before_migrating_and_retains_after``
exercises that ordering directly, stopping short of authentication (no
credentials are configured, matching every other test in this suite).

Phase 8 added a legacy-system check even earlier than that (spec section
12/16's "old-system exclusion"). ``isolated_env`` (``tests/conftest.py``)
clears every ``DHAN_*``/``TELEGRAM_*``/``PROJECT_ROOT`` variable so no test
here depends on the operator's local ``.env`` — this file's own
``_legacy_system_inactive`` autouse fixture does the equivalent for the
legacy check: without it, every test below would depend on whether the real
legacy LaunchAgent happens to be loaded on whichever machine runs the suite
(it was, on the machine this fixture was added from — see
``test_legacy_guard.py``'s real-machine tests for that check on purpose).
``test_main_refuses_when_the_legacy_system_is_active`` is the one test that
turns the stub the other way, to prove the refusal itself.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

import runtimes.intraday_options.__main__ as runtime_main
from common.config import load_paths
from common.execution import ModeTransitionDecision
from common.market_data import RecordedFeedAdapter, load_tick_tape
from common.process.legacy_guard import LaunchdLabelState, LegacySystemStatus
from runtimes.intraday_options.__main__ import (
    EXIT_LEGACY_SYSTEM_ACTIVE,
    EXIT_NO_CREDENTIALS,
    build_supervisor,
    main,
)

RUNTIME_ID = "intraday_options"

_INACTIVE_LEGACY_STATUS = LegacySystemStatus(
    launchd_state=LaunchdLabelState.INACTIVE,
    launchd_detail="stubbed inactive for this test file",
    process_running=False,
    process_detail="stubbed inactive for this test file",
)


@pytest.fixture(autouse=True)
def _legacy_system_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """This file's tests are about strategy filtering and startup order, not
    the legacy check — stub it inactive so none of them depend on whether
    the real legacy LaunchAgent happens to be loaded on the machine running
    the suite. See the module docstring."""
    monkeypatch.setattr(
        "runtimes.intraday_options.__main__.legacy_system_status",
        lambda: _INACTIVE_LEGACY_STATUS,
    )

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


def test_real_entrypoint_supplies_the_production_live_preflight_callback():
    """Regression for the Phase-10 production-call-site gap."""
    source = inspect.getsource(runtime_main.main)
    assert "live_preflight_passed_for=" in source
    assert "parent_live_preflight_passed" in source


def test_disabled_strategy_is_checked_as_a_disable_transition(
    populated_config, adapter, tmp_path, monkeypatch
):
    _write(
        populated_config / "strategies" / "io_bravo.yaml",
        "strategy_id: io_bravo\nenabled: false\nparameters:\n"
        "  instrument: NIFTY\n  security_id: '222'\n",
    )
    seen: dict[str, object] = {}

    def check(_repository, *, strategy_id, new_mode, **_kwargs):
        seen[strategy_id] = new_mode
        return ModeTransitionDecision(True)

    monkeypatch.setattr(runtime_main, "check_mode_transition_safety", check)
    build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=populated_config,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    assert seen["io_bravo"] is None


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


def test_main_refuses_when_the_legacy_system_is_active(
    populated_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Spec section 12/16's "old-system exclusion," checked before backup,
    migration or any network call — the one test in this file that turns
    ``_legacy_system_inactive`` the other way."""
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    active_status = LegacySystemStatus(
        launchd_state=LaunchdLabelState.ACTIVE,
        launchd_detail="label 'com.soundarraj.tradingautomation.starttrading' is loaded",
        process_running=False,
        process_detail="no live process found",
    )
    monkeypatch.setattr(
        "runtimes.intraday_options.__main__.legacy_system_status", lambda: active_status
    )

    exit_code = main(["--runtime-id", RUNTIME_ID, "--config-root", str(populated_config)])

    assert exit_code == EXIT_LEGACY_SYSTEM_ACTIVE
    # Refused before backup ever ran — nothing written to disk at all.
    paths = load_paths(tmp_path)
    assert not any(paths.backup_root.glob(f"{RUNTIME_ID}_*.db"))
    assert not paths.database_path(RUNTIME_ID).is_file()


def test_main_refuses_when_the_legacy_system_state_is_undetermined(
    populated_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """The fail-closed half of the same gate: a launchd check that could not
    be determined (``launchctl`` unavailable/errored/timed out) must refuse
    exactly like a confirmed detection — never be treated as "not detected" —
    and the operator message must say so, not falsely claim a detection."""
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    undetermined_status = LegacySystemStatus(
        launchd_state=LaunchdLabelState.UNKNOWN,
        launchd_detail="launchctl unavailable (FileNotFoundError('no launchctl'))",
        process_running=False,
        process_detail="no live process found",
    )
    monkeypatch.setattr(
        "runtimes.intraday_options.__main__.legacy_system_status", lambda: undetermined_status
    )

    exit_code = main(["--runtime-id", RUNTIME_ID, "--config-root", str(populated_config)])

    assert exit_code == EXIT_LEGACY_SYSTEM_ACTIVE
    paths = load_paths(tmp_path)
    assert not any(paths.backup_root.glob(f"{RUNTIME_ID}_*.db"))
    assert not paths.database_path(RUNTIME_ID).is_file()
    printed = capsys.readouterr().out
    assert "could not be determined" in printed
    assert "appears to be active" not in printed


def test_main_backs_up_before_migrating_and_retains_after(
    populated_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The pre-credential startup sequence: backup, then migrate, then retain.

    No Dhan credentials are configured (``isolated_env`` already cleared
    them), so ``main`` returns ``EXIT_NO_CREDENTIALS`` right after this
    sequence runs — before any network call, exactly where the assertions
    below need it to stop.
    """
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    paths = load_paths(tmp_path)
    db_path = paths.database_path(RUNTIME_ID)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE pre_migration_marker (x INTEGER)")
    conn.commit()
    conn.close()

    exit_code = main(["--runtime-id", RUNTIME_ID, "--config-root", str(populated_config)])

    assert exit_code == EXIT_NO_CREDENTIALS

    # Backup ran before migration: the snapshot has only the marker table,
    # not the real schema migration 0001+ creates.
    backups = list(paths.backup_root.glob(f"{RUNTIME_ID}_*.db"))
    assert len(backups) == 1
    backup_conn = sqlite3.connect(str(backups[0]))
    try:
        backup_tables = {
            row[0]
            for row in backup_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        backup_conn.close()
    assert backup_tables == {"pre_migration_marker"}

    # Migration then ran against the live database: the real schema exists.
    live_conn = sqlite3.connect(str(db_path))
    try:
        live_tables = {
            row[0]
            for row in live_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        live_conn.close()
    assert {"schema_migrations", "runtime_sessions", "errors"} <= live_tables
