"""``rolling_strangle_otm1`` through the **real** intraday composition root.

Spec section 18.7-equivalent (Phase 4): "Disabled strategy is discovered but
not spawned" and "Enabling it adds an isolated intraday worker with the
correct tick/control channels." Shipped disabled at delivery; the operator
enabled it for real, paper-only trading on 31 August 2026 (see
docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md) — so this file now asserts the
*real* committed ``config/`` tree directly registers this strategy
correctly, rather than proving it via a synthetic enabled-copy fixture. The
"not spawned while disabled" property is still proven, just against a
synthetic *disabled* copy now (the fixture's role is inverted from this
file's original form, not dropped). Mirrors ``tests/integration/
test_supertrend_buy_1_1p2_supervisor_composition.py``'s structure exactly —
see that file's own docstring for why this goes through the real
``build_supervisor`` rather than a hand-built ``WorkerConfig``: the defect
that discipline exists for (a ``multi_leg_engine`` worker registered with no
tick queue) killed ``straddle_920`` at startup and would have passed a
hand-built test the whole time it was broken.

Nothing is started: ``build_supervisor`` only *registers* workers. No
runtime, feed thread or child process runs; no Dhan/Telegram endpoint is
reachable from here.
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
from runtimes.intraday_options.worker import MultiLegEngineWorkerConfig

RUNTIME_ID = "intraday_options"
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_CONFIG = REPO_ROOT / "config"
STRATEGY_ID = "rolling_strangle_otm1"


@pytest.fixture(autouse=True)
def _legacy_system_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here is about the legacy-exclusion gate; stub it inactive so
    these tests do not depend on whether the real legacy LaunchAgent happens
    to be loaded on the machine running the suite (the identical fixture
    ``test_supervisor_tick_channel_registration.py``/``test_supertrend_buy_
    1_1p2_supervisor_composition.py`` use, for the same reason)."""
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
def disabled_config_root(config_root: Path) -> Path:
    """The **committed** config tree, copied verbatim except that this
    strategy's own ``enabled: true`` becomes ``enabled: false`` — the
    baseline "as if this strategy had never been enabled" the tests below
    compare the real committed tree against, to prove enabling it changed
    nothing about its siblings' wiring.
    """
    (config_root / "global.yaml").write_text(
        (REPO_CONFIG / "global.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (config_root / "runtimes" / f"{RUNTIME_ID}.yaml").write_text(
        (REPO_CONFIG / "runtimes" / f"{RUNTIME_ID}.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    strategies_dir = REPO_CONFIG / "strategies" / RUNTIME_ID
    for source in strategies_dir.glob("*.yaml"):
        text = source.read_text(encoding="utf-8")
        if source.stem == STRATEGY_ID:
            assert "\nenabled: true\n" in text, "the committed config is no longer enabled"
            text = text.replace("\nenabled: true\n", "\nenabled: false\n", 1)
        (config_root / "strategies" / source.name).write_text(text, encoding="utf-8")
    return config_root


# --------------------------------------------- committed config: registered
def test_the_committed_enabled_strategy_registers_an_isolated_worker_with_tick_and_control_channels(
    adapter, tmp_path: Path
):
    """Against the real committed ``config/`` tree, directly: a
    multi-leg-engine strategy is given both queues by the generic
    registration path, with no supervisor branch naming it."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=REPO_CONFIG,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    registrations = _registrations(supervisor)
    assert STRATEGY_ID in registrations

    config, channel = registrations[STRATEGY_ID]
    assert isinstance(config.multi_leg_engine, MultiLegEngineWorkerConfig)
    assert config.engine is None
    assert config.requires_tick_channel is True
    assert channel.tick_queue is not None, "the multi-leg worker got no tick queue"
    assert supervisor.control_queue(STRATEGY_ID) is not None


def test_the_worker_is_isolated_from_every_sibling(adapter, tmp_path: Path):
    """Independent identity, independent queues, independent correlation
    namespace — architecture spec's strategy-isolation rules. Deliberately
    checks uniqueness across however many strategies are actually enabled,
    rather than hardcoding a count — that count grows as more strategies are
    enabled, which is exactly what made this test brittle the last time it
    happened."""
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

    # add_worker refuses a strategy whose correlation token collides with an
    # already-admitted one; every registration coexisting is itself proof it
    # did not, for the real committed strategy_id set.
    tokens = {strategy_token(sid) for sid in registrations}
    assert len(tokens) == len(registrations)


def test_the_correlation_token_is_deterministic_and_generic() -> None:
    """No manual token mapping exists or is needed: strategy_token is a pure
    function of strategy_id, generic to every caller, unrelated to this
    strategy's own name in any special way."""
    assert strategy_token(STRATEGY_ID) == "roll"
    assert strategy_token(STRATEGY_ID) == strategy_token(STRATEGY_ID)
    assert len(strategy_token(STRATEGY_ID)) <= 4  # STRATEGY_TOKEN_LENGTH
    for other in ("c921_ema_cross_buy", "straddle_920", "supertrend_buy_1_1p2", "skeleton_fixture"):
        assert strategy_token(other) != strategy_token(STRATEGY_ID)


def test_the_enabled_worker_carries_the_committed_trading_parameters_and_stays_paper(
    adapter, tmp_path: Path
):
    """The registration is not merely present, it carries the real
    configuration: strategy_ref, ten lots, the dhan resolver, the
    09:15/15:10/15:15 session, and every live gate still refusing live
    execution (spec section 16)."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=REPO_CONFIG,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    config, _ = _registrations(supervisor)[STRATEGY_ID]
    multi_leg = config.multi_leg_engine
    assert multi_leg.strategy_ref.endswith(":RollingStrangleOtm1Strategy")
    assert multi_leg.lots == 10
    assert multi_leg.strike_step == 50
    assert multi_leg.contract_resolver == "dhan"
    assert multi_leg.lot_size == 0, "dhan resolver ignores this; never a hardcoded lot size"
    assert multi_leg.max_daily_loss_percent is None, "the strategy owns its own combined stop"
    assert multi_leg.session_start_time == "09:15"
    assert multi_leg.strategy_kwargs["entry_time"] == "09:45"
    assert multi_leg.strategy_kwargs["stop_new_entries_after"] == "15:10"
    assert multi_leg.strategy_kwargs["max_rolls_ce"] == 2
    assert multi_leg.strategy_kwargs["max_rolls_pe"] == 2
    assert multi_leg.strategy_kwargs["single_leg_roll"] is True
    assert multi_leg.strategy_kwargs["combined_stop_per_lot"] == 2000.0
    assert config.square_off_policy.entry_cutoff.strftime("%H:%M") == "15:10"
    assert config.square_off_policy.square_off_at.strftime("%H:%M") == "15:15"
    assert config.execution_mode.value == "paper"
    assert config.strategy_live_approved is False
    assert config.global_live_trading_enabled is False
    assert config.runtime_live_execution_allowed is False


# ------------------------------------------------ disabled: not registered
def test_a_disabled_copy_registers_no_worker(disabled_config_root: Path, adapter, tmp_path: Path):
    """The other half of Spec 18.7-equivalent, proven against a synthetic
    disabled copy now that the real committed file is enabled: discovered —
    the supervisor still runs its mode-transition exposure check over it,
    disabling must not bypass that — but no worker, tick queue or control
    queue."""
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
    otherwise change the workers that are enabled independent of it —
    proven by comparing the real committed tree against the synthetic copy
    with only this one strategy turned off."""
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
