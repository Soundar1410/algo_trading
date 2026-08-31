"""``supertrend_buy_1_1p2`` through the **real** intraday composition root.

Spec 18.7: "Disabled strategy is discovered but not spawned" and "Enabling it
adds an isolated intraday worker with the correct tick/control channels."
Shipped disabled at delivery; the operator enabled it for real, paper-only
trading on 31 August 2026 (see docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md) —
so this file now asserts the *real* committed ``config/`` tree directly
registers this strategy correctly, rather than proving it via a synthetic
enabled-copy fixture. The "not spawned while disabled" property is still
proven, just against a synthetic *disabled* copy now (the fixture's role is
inverted from this file's original form, not dropped).

Everything here goes through ``runtimes.intraday_options.__main__.build_supervisor``,
never a hand-built ``WorkerConfig`` handed straight to ``add_worker`` — the defect
this discipline exists for (a ``multi_leg_engine`` worker registered with no tick
queue, which killed ``straddle_920`` at startup) would have passed a hand-built test
the whole time it was broken. See
``tests/unit/test_supervisor_tick_channel_registration.py``, whose approach this
file follows.

Nothing is started: ``build_supervisor`` only *registers* workers. The queues a
worker is given are decided before any process is spawned, and that decision is what
this file asserts. No runtime, feed thread or child process runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import runtimes.intraday_options.__main__ as runtime_main
from common.config import load_paths
from common.execution.correlation import strategy_token
from common.market_data import RecordedFeedAdapter, load_tick_tape
from common.process.legacy_guard import LaunchdLabelState, LegacySystemStatus
from runtimes.intraday_options.__main__ import build_supervisor
from runtimes.intraday_options.worker import EngineWorkerConfig

RUNTIME_ID = "intraday_options"
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_CONFIG = REPO_ROOT / "config"
STRATEGY_ID = "supertrend_buy_1_1p2"


@pytest.fixture(autouse=True)
def _legacy_system_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here is about the legacy-exclusion gate; stub it inactive so these
    tests do not depend on whether the real legacy LaunchAgent happens to be loaded
    on the machine running the suite (the identical fixture
    ``test_supervisor_tick_channel_registration.py`` uses, for the same reason)."""
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


@pytest.fixture
def adapter(tick_tape_path: Path) -> RecordedFeedAdapter:
    return RecordedFeedAdapter(load_tick_tape(tick_tape_path))


def _registrations(supervisor) -> dict[str, tuple[object, object]]:
    return {config.strategy_id: (config, channel) for config, channel in supervisor._workers}


def _copy_config_tree(config_root: Path) -> None:
    """Every committed intraday_options file, verbatim, into a scratch root —
    the starting point both fixtures below build on."""
    (config_root / "global.yaml").write_text(
        (REPO_CONFIG / "global.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (config_root / "runtimes" / f"{RUNTIME_ID}.yaml").write_text(
        (REPO_CONFIG / "runtimes" / f"{RUNTIME_ID}.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    strategies_dir = REPO_CONFIG / "strategies" / RUNTIME_ID
    for source in strategies_dir.glob("*.yaml"):
        (config_root / "strategies" / source.name).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )


@pytest.fixture
def disabled_config_root(config_root: Path) -> Path:
    """The **committed** config tree, copied verbatim except that this
    strategy's own ``enabled: true`` becomes ``enabled: false`` — the
    baseline "as if this strategy had never been enabled" the tests below
    compare the real committed tree against, to prove enabling it changed
    nothing about its siblings' wiring.
    """
    _copy_config_tree(config_root)
    text = (REPO_CONFIG / "strategies" / RUNTIME_ID / f"{STRATEGY_ID}.yaml").read_text(
        encoding="utf-8"
    )
    assert "\nenabled: true\n" in text, "the committed config is no longer enabled"
    text = text.replace("\nenabled: true\n", "\nenabled: false\n", 1)
    (config_root / "strategies" / f"{STRATEGY_ID}.yaml").write_text(text, encoding="utf-8")
    return config_root


# --------------------------------------------- committed config: registered
def test_the_committed_enabled_strategy_registers_a_worker_with_tick_and_control_channels(
    adapter, tmp_path: Path
):
    """Against the real committed ``config/`` tree, directly: a
    trading-engine strategy is given both queues by the generic registration
    path, with no supervisor branch naming it."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=REPO_CONFIG,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    registrations = _registrations(supervisor)
    assert STRATEGY_ID in registrations

    config, channel = registrations[STRATEGY_ID]
    assert isinstance(config.engine, EngineWorkerConfig)
    assert config.multi_leg_engine is None
    assert config.requires_tick_channel is True
    assert channel.tick_queue is not None, "the engine worker got no tick queue"
    assert supervisor.control_queue(STRATEGY_ID) is not None


def test_the_worker_is_isolated_from_every_sibling(adapter, tmp_path: Path):
    """Independent identity, independent queues, independent correlation
    namespace — spec section 5 of the architecture document's
    strategy-isolation rules. Deliberately checks uniqueness across however
    many strategies are actually enabled, rather than hardcoding a count —
    that count grows as more strategies are enabled, which is exactly what
    made this test brittle the last time it happened."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=REPO_CONFIG,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    registrations = _registrations(supervisor)
    assert STRATEGY_ID in registrations
    assert len(registrations) > 1, "nothing to prove isolation from"

    channels = {sid: channel for sid, (_, channel) in registrations.items()}
    queues = [id(c.tick_queue) for c in channels.values() if c.tick_queue is not None]
    assert len(queues) == len(set(queues))

    control_queues = [id(supervisor.control_queue(sid)) for sid in registrations]
    assert len(control_queues) == len(set(control_queues))

    # ``add_worker`` refuses a strategy whose correlation token collides with
    # an already-admitted one; every registration coexisting is itself proof
    # that none did, for the real committed strategy_id set.
    tokens = {strategy_token(sid) for sid in registrations}
    assert len(tokens) == len(registrations)


def test_the_enabled_worker_carries_the_committed_trading_parameters(adapter, tmp_path: Path):
    """The registration is not merely present, it carries the real configuration:
    the same strategy_ref, ten lots, the dhan resolver and the 09:15/15:15/15:20
    session the committed file declares."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=REPO_CONFIG,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    config, _ = _registrations(supervisor)[STRATEGY_ID]
    engine = config.engine
    assert engine.strategy_ref.endswith(":SupertrendBuy1x1p2Strategy")
    assert engine.lots == 10
    assert engine.contract_resolver == "dhan"
    assert engine.warmup_source == "dhan"
    assert engine.strategy_kwargs["warmup_min_bars"] == 75
    assert engine.session_start_time == "09:15"
    assert config.square_off_policy.entry_cutoff.strftime("%H:%M") == "15:15"
    assert config.square_off_policy.square_off_at.strftime("%H:%M") == "15:20"
    assert config.execution_mode.value == "paper"
    assert config.strategy_live_approved is False
    assert config.global_live_trading_enabled is False


# ------------------------------------------------ disabled: not registered
def test_a_disabled_copy_registers_no_worker(disabled_config_root: Path, adapter, tmp_path: Path):
    """The other half of Spec 18.7, proven against a synthetic disabled copy
    now that the real committed file is enabled: the strategy is still
    discovered — the supervisor still runs its mode-transition exposure
    check over it, which is why disabling must not bypass that check — but
    no worker, tick queue or control queue are created for it."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=disabled_config_root,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    registrations = _registrations(supervisor)
    assert STRATEGY_ID not in registrations
    assert supervisor.control_queue(STRATEGY_ID) is None


def test_disabling_it_does_not_change_the_other_enabled_workers(
    disabled_config_root: Path, adapter, tmp_path: Path
):
    """Flipping only this strategy's own flag must not reroute, resize or
    otherwise change the workers that are enabled independent of it — proven
    by comparing the real committed tree against the synthetic copy with
    only this one strategy turned off."""
    enabled_root = tmp_path / "enabled"
    disabled_root = tmp_path / "disabled"
    enabled_root.mkdir()
    disabled_root.mkdir()

    enabled_supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=REPO_CONFIG,
        paths=load_paths(enabled_root),
        adapter=adapter,
    )
    without = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=disabled_config_root,
        paths=load_paths(disabled_root),
        adapter=adapter,
    )

    with_this = _registrations(enabled_supervisor)
    without_this = _registrations(without)

    assert set(without_this) == set(with_this) - {STRATEGY_ID}
    for strategy_id in without_this:
        before_config, before_channel = without_this[strategy_id]
        after_config, after_channel = with_this[strategy_id]
        assert type(before_config.engine) is type(after_config.engine)
        assert type(before_config.multi_leg_engine) is type(after_config.multi_leg_engine)
        assert before_config.execution_mode == after_config.execution_mode
        assert (before_channel.tick_queue is not None) == (after_channel.tick_queue is not None)
