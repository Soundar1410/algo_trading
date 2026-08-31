"""Which workers ``build_supervisor`` gives a tick channel to.

The production defect this file exists for: ``build_supervisor`` spelled the
tick-channel predicate out as ``worker_config.engine is not None``, which
recognised :class:`~runtimes.intraday_options.worker.EngineWorkerConfig` and
ignored :class:`~runtimes.intraday_options.worker.
MultiLegEngineWorkerConfig`. In a real paper session ``c921_ema_cross_buy``
started, ``weekly_delta_neutral`` (a different runtime group) started, and
``straddle_920`` died at startup with "this worker is configured for the
multi-leg engine but was given no tick queue; the supervisor must register it
with tick_channel=True" — the multi-leg worker's own fail-closed refusal,
firing exactly as designed on a registration that never asked for the queue
it needs.

The fix moves the question to :attr:`~runtimes.intraday_options.worker.
WorkerConfig.requires_tick_channel`, so a future third engine kind is covered
by construction rather than by remembering to widen a boolean expression at
the composition root. These tests assert the *registration*, not the engine
run: the queues a worker is given are decided before any process is spawned,
and that decision is what broke.

Everything here goes through the real ``build_supervisor``, never a
hand-built ``WorkerConfig`` handed straight to ``add_worker`` — a
hand-built one would have passed the whole time the production wiring was
broken.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import runtimes.intraday_options.__main__ as runtime_main
from common.config import load_paths
from common.market_data import RecordedFeedAdapter, load_tick_tape
from common.process.legacy_guard import LaunchdLabelState, LegacySystemStatus
from runtimes.intraday_options.__main__ import build_supervisor
from runtimes.intraday_options.worker import (
    EngineWorkerConfig,
    MultiLegEngineWorkerConfig,
    WorkerConfig,
)

RUNTIME_ID = "intraday_options"
REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"

_EMA_REF = "strategies.intraday_options.c921_ema_cross_buy.strategy:EmaCross9x21BuyStrategy"
_STRADDLE_REF = "strategies.intraday_options.straddle_920.strategy:Straddle920Strategy"


@pytest.fixture(autouse=True)
def _legacy_system_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here is about the legacy-exclusion gate; stub it inactive so
    these tests do not depend on whether the real legacy LaunchAgent happens
    to be loaded on the machine running the suite (see
    ``test_intraday_options_main.py``'s identical fixture)."""
    monkeypatch.setattr(
        runtime_main,
        "legacy_system_status",
        lambda: LegacySystemStatus(
            launchd_state=LaunchdLabelState.INACTIVE,
            launchd_detail="stubbed inactive for this test file",
            process_running=False,
            process_detail="stubbed inactive for this test file",
        ),
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

#: A fixture (non-engine) strategy: no ``strategy_ref``, so
#: ``config_adapter`` builds neither engine field. Named after the
#: walking-skeleton pattern the spec labels "NOT a trading strategy".
FIXTURE_YAML = f"""
strategy_id: fixtureskel
runtime_id: {RUNTIME_ID}
enabled: true
parameters:
  instrument: NIFTY
  security_id: "13"
"""

#: The single-leg engine path (``engine: trading_engine`` + ``strategy_ref``).
SINGLE_LEG_YAML = f"""
strategy_id: singleleg
runtime_id: {RUNTIME_ID}
enabled: true
engine: trading_engine
parameters:
  instrument: NIFTY
  security_id: "13"
  strategy_ref: "{_EMA_REF}"
  strategy_kwargs:
    lots_per_trade: 1
  contract_resolver: dhan
"""

#: The multi-leg engine path — the shape ``straddle_920`` uses, which the
#: old predicate did not recognise.
MULTI_LEG_YAML = f"""
strategy_id: multileg1
runtime_id: {RUNTIME_ID}
enabled: true
engine: multi_leg_engine
parameters:
  instrument: NIFTY
  security_id: "13"
  strategy_ref: "{_STRADDLE_REF}"
  strategy_kwargs:
    lots_per_leg: 1
  contract_resolver: dhan
  vix_security_id: "21"
  vix_index_segment: IDX_I
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def three_kinds_config(config_root: Path) -> Path:
    """One config tree holding all three worker shapes, so a single
    ``build_supervisor`` call decides all three registrations the same way
    production does."""
    _write(config_root / "global.yaml", GLOBAL_YAML)
    _write(
        config_root / "runtimes" / f"{RUNTIME_ID}.yaml",
        f"runtime_id: {RUNTIME_ID}\nenabled: true\n",
    )
    _write(config_root / "strategies" / "fixtureskel.yaml", FIXTURE_YAML)
    _write(config_root / "strategies" / "singleleg.yaml", SINGLE_LEG_YAML)
    _write(config_root / "strategies" / "multileg1.yaml", MULTI_LEG_YAML)
    return config_root


@pytest.fixture
def adapter(tick_tape_path: Path) -> RecordedFeedAdapter:
    return RecordedFeedAdapter(load_tick_tape(tick_tape_path))


def _registrations(supervisor) -> dict[str, tuple[WorkerConfig, object]]:
    """``strategy_id`` -> ``(worker_config, channel)`` for what was admitted.

    ``_workers`` is the only externally visible record of a registration
    before ``.run()``; ``test_intraday_options_main.py`` reads the same list
    for the same reason.
    """
    return {config.strategy_id: (config, channel) for config, channel in supervisor._workers}


# --------------------------------------------------- the predicate itself


def _worker(tmp_path: Path, **overrides) -> WorkerConfig:
    return WorkerConfig(
        runtime_id=RUNTIME_ID,
        strategy_id="predicate",
        security_id="13",
        instrument="NIFTY",
        database_path=tmp_path / "db.sqlite",
        lock_dir=tmp_path / "locks",
        pid_dir=tmp_path / "pid",
        log_dir=tmp_path / "logs",
        trading_date="2026-08-21",
        **overrides,
    )


def test_requires_tick_channel_covers_both_engine_kinds(tmp_path: Path):
    single = _worker(tmp_path, engine=EngineWorkerConfig(strategy_ref=_EMA_REF))
    multi = _worker(tmp_path, multi_leg_engine=MultiLegEngineWorkerConfig(
        strategy_ref=_STRADDLE_REF
    ))
    fixture = _worker(tmp_path)

    assert single.requires_tick_channel is True
    assert multi.requires_tick_channel is True
    assert fixture.requires_tick_channel is False


# ------------------------------------------- registration, scratch config


def test_a_single_leg_engine_worker_is_registered_with_both_queues(
    three_kinds_config, adapter, tmp_path
):
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=three_kinds_config,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    config, channel = _registrations(supervisor)["singleleg"]
    assert config.engine is not None
    assert channel.tick_queue is not None, "the engine worker got no tick queue"
    assert supervisor.control_queue("singleleg") is not None


def test_a_multi_leg_engine_worker_is_registered_with_both_queues(
    three_kinds_config, adapter, tmp_path
):
    """The defect, stated directly.

    Before the fix this worker was registered with ``tick_channel=False``,
    so ``channel.tick_queue`` was ``None`` and the spawned child refused to
    start with "given no tick queue".
    """
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=three_kinds_config,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    config, channel = _registrations(supervisor)["multileg1"]
    assert config.multi_leg_engine is not None
    assert channel.tick_queue is not None, (
        "a multi_leg_engine worker was registered without a tick queue — this is "
        "the exact registration that killed straddle_920 at startup"
    )
    assert supervisor.control_queue("multileg1") is not None


def test_a_fixture_worker_still_gets_no_tick_channel(three_kinds_config, adapter, tmp_path):
    """The non-engine path is unchanged: the tick channel stays opt-in, and a
    worker with neither engine field must not be allocated one — the hub
    publishes ticks per registered channel, so an unread queue is real work
    and a real drop counter for nothing."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=three_kinds_config,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    config, channel = _registrations(supervisor)["fixtureskel"]
    assert config.engine is None and config.multi_leg_engine is None
    assert channel.tick_queue is None
    assert supervisor.control_queue("fixtureskel") is None


# ------------------------------------------ registration, committed config


@pytest.mark.parametrize(
    ("strategy_id", "engine_field"),
    [("straddle_920", "multi_leg_engine"), ("c921_ema_cross_buy", "engine")],
)
def test_the_committed_enabled_strategies_are_registered_with_a_tick_channel(
    adapter, tmp_path, strategy_id, engine_field
):
    """Against the **real committed** ``config/`` tree, not a scratch one.

    This is the test that would have caught the defect before the paper
    session did: it registers exactly what production registers.
    ``c921_ema_cross_buy`` is parametrised alongside ``straddle_920`` to pin
    that the fix widened the predicate without changing the single-leg
    strategy that was already working.
    """
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=REPO_CONFIG,
        paths=load_paths(tmp_path),
        adapter=adapter,
        strategy_ids=frozenset({strategy_id}),
    )

    registrations = _registrations(supervisor)
    assert strategy_id in registrations, (
        f"{strategy_id} is no longer an enabled intraday_options strategy in the "
        "committed config; this test needs updating deliberately, not deleting"
    )
    config, channel = registrations[strategy_id]
    assert getattr(config, engine_field) is not None
    assert config.requires_tick_channel is True
    assert channel.tick_queue is not None
    assert supervisor.control_queue(strategy_id) is not None
