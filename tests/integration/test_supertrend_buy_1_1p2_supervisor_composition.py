"""``supertrend_buy_1_1p2`` through the **real** intraday composition root.

Spec 18.7: "Disabled strategy is discovered but not spawned" and "Enabling it in a
test fixture adds a third isolated intraday worker with the correct tick/control
channels."

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


@pytest.fixture
def enabled_config_root(config_root: Path) -> Path:
    """The **committed** config tree, copied verbatim except that this strategy's
    ``enabled: false`` becomes ``enabled: true``.

    Copied rather than rewritten by hand on purpose: if the fixture invented its own
    YAML, it would prove that *some* config produces a worker, not that *this*
    committed config does. The single-line substitution is also what makes the
    negative test above meaningful — the only difference between "not spawned" and
    "spawned as a third worker" is that one flag.
    """
    (config_root / "global.yaml").write_text(
        (REPO_CONFIG / "global.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (config_root / "runtimes" / f"{RUNTIME_ID}.yaml").write_text(
        (REPO_CONFIG / "runtimes" / f"{RUNTIME_ID}.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    for name in ("ema_cross_9_21_buy", "straddle_920", STRATEGY_ID):
        text = (REPO_CONFIG / "strategies" / RUNTIME_ID / f"{name}.yaml").read_text(
            encoding="utf-8"
        )
        if name == STRATEGY_ID:
            assert "\nenabled: false\n" in text, "the committed config is no longer disabled"
            text = text.replace("\nenabled: false\n", "\nenabled: true\n", 1)
        (config_root / "strategies" / f"{name}.yaml").write_text(text, encoding="utf-8")
    return config_root


# ------------------------------------------- committed config: not spawned
def test_the_committed_disabled_strategy_registers_no_worker(adapter, tmp_path: Path):
    """Against the real committed ``config/`` tree. The strategy is discovered — the
    supervisor still runs its mode-transition exposure check over it, which is why
    disabling must not bypass that check — but no worker, no tick queue and no control
    queue are created for it."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=REPO_CONFIG,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    registrations = _registrations(supervisor)
    assert STRATEGY_ID not in registrations
    assert supervisor.control_queue(STRATEGY_ID) is None


def test_the_committed_config_still_registers_exactly_the_two_enabled_strategies(
    adapter, tmp_path: Path
):
    """Adding a disabled strategy must change nothing about what production starts."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=REPO_CONFIG,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    assert set(_registrations(supervisor)) == {"ema_cross_9_21_buy", "straddle_920"}


# ------------------------------------------------ enabled: a third worker
def test_enabling_it_adds_a_third_worker_with_tick_and_control_channels(
    enabled_config_root: Path, adapter, tmp_path: Path
):
    """Spec 18.7. The one thing this port must prove about the runtime seam: a
    trading-engine strategy is given both queues by the generic registration path,
    with no supervisor branch naming it."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=enabled_config_root,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    registrations = _registrations(supervisor)
    assert set(registrations) == {"ema_cross_9_21_buy", "straddle_920", STRATEGY_ID}

    config, channel = registrations[STRATEGY_ID]
    assert isinstance(config.engine, EngineWorkerConfig)
    assert config.multi_leg_engine is None
    assert config.requires_tick_channel is True
    assert channel.tick_queue is not None, "the engine worker got no tick queue"
    assert supervisor.control_queue(STRATEGY_ID) is not None


def test_the_third_worker_is_isolated_from_the_other_two(
    enabled_config_root: Path, adapter, tmp_path: Path
):
    """Independent identity, independent queues, independent correlation namespace —
    spec section 5 of the architecture document's strategy-isolation rules."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=enabled_config_root,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    registrations = _registrations(supervisor)
    channels = {sid: channel for sid, (_, channel) in registrations.items()}
    queues = [id(c.tick_queue) for c in channels.values() if c.tick_queue is not None]
    assert len(queues) == len(set(queues)) == 3

    control_queues = [id(supervisor.control_queue(sid)) for sid in registrations]
    assert len(control_queues) == len(set(control_queues)) == 3

    # ``add_worker`` refuses a strategy whose correlation token collides with an
    # already-admitted one; three admitted workers is itself proof that it did not.
    from common.execution.correlation import strategy_token

    tokens = {strategy_token(sid) for sid in registrations}
    assert len(tokens) == 3


def test_the_enabled_worker_carries_the_committed_trading_parameters(
    enabled_config_root: Path, adapter, tmp_path: Path
):
    """The registration is not merely present, it carries the real configuration:
    the same strategy_ref, ten lots, the dhan resolver and the 09:15/15:15/15:20
    session the committed file declares."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=enabled_config_root,
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
