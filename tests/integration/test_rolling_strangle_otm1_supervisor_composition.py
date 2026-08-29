"""``rolling_strangle_otm1`` through the **real** intraday composition root.

Spec section 18.7-equivalent (Phase 4): "Disabled strategy is discovered but
not spawned" and "Enabling it in a test fixture adds an isolated intraday
worker with the correct tick/control channels." Mirrors ``tests/integration/
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
#: Every strategy expected enabled in the committed intraday_options tree,
#: independent of this one — the baseline this file must never disturb.
_OTHER_ENABLED = ("ema_cross_9_21_buy", "straddle_920")


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
def enabled_config_root(config_root: Path) -> Path:
    """The **committed** config tree, copied verbatim except that this
    strategy's ``enabled: false`` becomes ``enabled: true``.

    Copied rather than rewritten by hand, and only this one line changed —
    per the user's own Phase 4 instruction — so a passing test proves *this*
    committed configuration produces a correctly isolated worker, not merely
    that some invented YAML does; and the single-line substitution is what
    makes the "not spawned" test above meaningful, since that is the only
    difference between the two states.
    """
    (config_root / "global.yaml").write_text(
        (REPO_CONFIG / "global.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (config_root / "runtimes" / f"{RUNTIME_ID}.yaml").write_text(
        (REPO_CONFIG / "runtimes" / f"{RUNTIME_ID}.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    for name in (*_OTHER_ENABLED, STRATEGY_ID):
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
    """Against the real committed ``config/`` tree: discovered (the
    supervisor's mode-transition exposure check still runs over it — disabling
    must not bypass that), but no worker, tick queue or control queue."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=REPO_CONFIG,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    registrations = _registrations(supervisor)
    assert STRATEGY_ID not in registrations
    assert supervisor.control_queue(STRATEGY_ID) is None


def test_the_committed_config_still_registers_only_the_existing_enabled_strategies(
    adapter, tmp_path: Path
):
    """Adding a disabled strategy file must change nothing about what
    production starts today."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=REPO_CONFIG,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    assert set(_registrations(supervisor)) == set(_OTHER_ENABLED)


# ------------------------------------------------ enabled: an isolated worker
def test_enabling_it_adds_an_isolated_worker_with_tick_and_control_channels(
    enabled_config_root: Path, adapter, tmp_path: Path
):
    """The one thing this port must prove about the runtime seam: a
    multi-leg-engine strategy is given both queues by the generic
    registration path, with no supervisor branch naming it."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=enabled_config_root,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    registrations = _registrations(supervisor)
    assert set(registrations) == {*_OTHER_ENABLED, STRATEGY_ID}

    config, channel = registrations[STRATEGY_ID]
    assert isinstance(config.multi_leg_engine, MultiLegEngineWorkerConfig)
    assert config.engine is None
    assert config.requires_tick_channel is True
    assert channel.tick_queue is not None, "the multi-leg worker got no tick queue"
    assert supervisor.control_queue(STRATEGY_ID) is not None


def test_the_new_worker_is_isolated_from_the_others(
    enabled_config_root: Path, adapter, tmp_path: Path
):
    """Independent identity, independent queues, independent correlation
    namespace — architecture spec's strategy-isolation rules."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=enabled_config_root,
        paths=load_paths(tmp_path),
        adapter=adapter,
    )

    registrations = _registrations(supervisor)
    assert len(registrations) == 3

    channels = {sid: channel for sid, (_, channel) in registrations.items()}
    queues = [id(c.tick_queue) for c in channels.values() if c.tick_queue is not None]
    assert len(queues) == len(set(queues)) == 3

    control_queues = [id(supervisor.control_queue(sid)) for sid in registrations]
    assert len(control_queues) == len(set(control_queues)) == 3

    # add_worker refuses a strategy whose correlation token collides with an
    # already-admitted one; three admitted workers is itself proof it did
    # not, for this exact committed strategy_id set.
    tokens = {strategy_token(sid) for sid in registrations}
    assert len(tokens) == 3


def test_the_correlation_token_is_deterministic_and_generic(enabled_config_root: Path) -> None:
    """No manual token mapping exists or is needed: strategy_token is a pure
    function of strategy_id, generic to every caller, unrelated to this
    strategy's own name in any special way."""
    assert strategy_token(STRATEGY_ID) == "roll"
    assert strategy_token(STRATEGY_ID) == strategy_token(STRATEGY_ID)
    assert len(strategy_token(STRATEGY_ID)) <= 4  # STRATEGY_TOKEN_LENGTH
    for other in (*_OTHER_ENABLED, "supertrend_buy_1_1p2", "skeleton_fixture"):
        assert strategy_token(other) != strategy_token(STRATEGY_ID)


def test_the_enabled_worker_carries_the_committed_trading_parameters_and_stays_paper(
    enabled_config_root: Path, adapter, tmp_path: Path
):
    """The registration is not merely present, it carries the real
    configuration: strategy_ref, ten lots, the dhan resolver, the
    09:15/15:10/15:15 session, and every live gate still refusing live
    execution (spec section 16)."""
    supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=enabled_config_root,
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


def test_the_other_two_enabled_workers_are_unaffected_by_enabling_this_one(
    enabled_config_root: Path, adapter, tmp_path: Path
):
    """Flipping only this strategy's own flag must not reroute, resize or
    otherwise change the workers that were already enabled."""
    baseline_root = tmp_path / "baseline"
    with_new_strategy_root = tmp_path / "with_new_strategy"
    baseline_root.mkdir()
    with_new_strategy_root.mkdir()

    baseline_supervisor = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=REPO_CONFIG,
        paths=load_paths(baseline_root),
        adapter=adapter,
    )
    baseline = _registrations(baseline_supervisor)

    with_new_strategy = build_supervisor(
        runtime_id=RUNTIME_ID,
        config_root=enabled_config_root,
        paths=load_paths(with_new_strategy_root),
        adapter=adapter,
    )
    after = _registrations(with_new_strategy)

    for strategy_id in _OTHER_ENABLED:
        before_config, before_channel = baseline[strategy_id]
        after_config, after_channel = after[strategy_id]
        assert type(before_config.engine) is type(after_config.engine)
        assert type(before_config.multi_leg_engine) is type(after_config.multi_leg_engine)
        assert before_config.execution_mode == after_config.execution_mode
        assert (before_channel.tick_queue is not None) == (after_channel.tick_queue is not None)
