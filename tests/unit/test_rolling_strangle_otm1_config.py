"""The committed ``config/strategies/rolling_strangle_otm1.yaml``, read
through the real loader and the real intraday config adapter.

Nothing here builds a config by hand: every assertion goes through
``load_resolved_config`` / ``discover_strategies`` / ``build_worker_config``
against the file that is actually committed, because a hand-built config
would pass whatever the committed one said. Mirrors ``tests/unit/
test_supertrend_buy_1_1p2_config.py``'s structure exactly.

Spec sections 13 (configuration requirements), 14 (validation rules), and
16 (safety boundary).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from common.config.loader import (
    discover_enabled_strategies,
    discover_strategies,
    load_resolved_config,
)
from common.config.models import EngineKind, ExecutionMode, effective_live_gate
from common.engine.config import SessionConfig
from common.engine.session import MarketSession
from runtimes.intraday_options.config_adapter import build_worker_config
from runtimes.intraday_options.multi_leg_engine_worker import load_multi_leg_strategy
from runtimes.intraday_options.worker import MultiLegEngineWorkerConfig
from strategies.intraday_options.rolling_strangle_otm1.strategy import RollingStrangleOtm1Strategy

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPO_ROOT / "config"
STRATEGY_ID = "rolling_strangle_otm1"
RUNTIME_ID = "intraday_options"
CONFIG_FILE = CONFIG_ROOT / "strategies" / RUNTIME_ID / f"{STRATEGY_ID}.yaml"
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def resolved():
    return load_resolved_config(CONFIG_ROOT, RUNTIME_ID, STRATEGY_ID)


@pytest.fixture
def worker(resolved, tmp_path: Path):
    return build_worker_config(
        resolved,
        database_path=tmp_path / "db.sqlite",
        lock_dir=tmp_path / "locks",
        pid_dir=tmp_path / "pid",
        log_dir=tmp_path / "logs",
        trading_date="2026-08-24",
    )


@pytest.fixture
def multi_leg(worker) -> MultiLegEngineWorkerConfig:
    assert worker.multi_leg_engine is not None
    return worker.multi_leg_engine


# ------------------------------------------------------------- 16. fail-closed
def test_the_committed_config_is_enabled_paper_and_not_live_approved(resolved):
    """Shipped disabled at delivery; the operator enabled it for real, paper-only
    trading on 31 August 2026 (see docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md).
    The two flags that still keep live execution unreachable, regardless."""
    assert resolved.strategy.strategy_id == STRATEGY_ID
    assert resolved.strategy.runtime_id == RUNTIME_ID
    assert resolved.strategy.enabled is True
    assert resolved.strategy.mode is ExecutionMode.PAPER
    assert resolved.strategy.live_approved is False


def test_the_enabled_strategy_is_discovered_and_enabled():
    discovered = [c.strategy.strategy_id for c in discover_strategies(CONFIG_ROOT, RUNTIME_ID)]
    enabled = [c.strategy.strategy_id for c in discover_enabled_strategies(CONFIG_ROOT, RUNTIME_ID)]
    assert STRATEGY_ID in discovered
    assert STRATEGY_ID in enabled


def test_enabling_this_strategy_did_not_change_other_strategies_flags():
    """No other strategy's committed flags moved as a side effect of this one's
    enable decision. Deliberately membership checks, not an exhaustive equality
    against the full enabled set — that set is this file's business only for
    STRATEGY_ID, and hardcoding every other strategy's name here is exactly the
    coupling that made this test brittle the last time the committed set grew."""
    enabled = {c.strategy.strategy_id for c in discover_enabled_strategies(CONFIG_ROOT, RUNTIME_ID)}
    assert STRATEGY_ID in enabled
    assert {"c921_ema_cross_buy", "straddle_920"} <= enabled
    for other in ("c921_ema_cross_buy", "straddle_920"):
        cfg = load_resolved_config(CONFIG_ROOT, RUNTIME_ID, other)
        assert cfg.strategy.enabled is True
        assert cfg.strategy.mode is ExecutionMode.PAPER
        assert cfg.strategy.live_approved is False


def test_paper_mode_is_refused_by_the_live_gate(resolved):
    decision = effective_live_gate(resolved, preflight_passed=True)
    assert decision.allowed is False
    assert decision.blocked_reasons


def test_the_committed_file_names_no_live_enabling_value():
    text = CONFIG_FILE.read_text(encoding="utf-8")
    assert "mode: paper" in text
    assert "live_approved: false" in text
    assert "mode: live" not in text


def test_mode_live_is_refused_outright_for_the_multi_leg_engine(resolved, tmp_path: Path):
    """Spec section 16 / config_adapter.py's own multi-leg-engine live refusal
    — not bypassable by mutating just the mode field."""
    mutated = resolved.model_copy(
        update={"strategy": resolved.strategy.model_copy(update={"mode": ExecutionMode.LIVE})}
    )
    with pytest.raises(Exception, match="mode: live is not supported"):
        build_worker_config(
            mutated,
            database_path=tmp_path / "db.sqlite",
            lock_dir=tmp_path / "locks",
            pid_dir=tmp_path / "pid",
            log_dir=tmp_path / "logs",
            trading_date="2026-08-24",
        )


# ------------------------------------------------------------- engine routing
def test_it_routes_to_the_multi_leg_engine_generically(resolved, worker):
    assert resolved.strategy.engine is EngineKind.MULTI_LEG_ENGINE
    assert worker.multi_leg_engine is not None
    assert isinstance(worker.multi_leg_engine, MultiLegEngineWorkerConfig)
    assert worker.engine is None


def test_the_worker_requires_a_tick_channel(worker):
    """What the supervisor's registration keys on — proven end to end in
    tests/integration/test_rolling_strangle_otm1_supervisor_composition.py."""
    assert worker.requires_tick_channel is True


def test_the_strategy_ref_loads_the_real_class_with_the_configured_kwargs(multi_leg):
    strategy = load_multi_leg_strategy(multi_leg.strategy_ref, multi_leg.strategy_kwargs)
    assert isinstance(strategy, RollingStrangleOtm1Strategy)
    assert strategy.name == STRATEGY_ID
    assert strategy.quantity_lots == 10
    assert strategy._entry_time.strftime("%H:%M") == "09:45"
    assert strategy._new_entry_cutoff.strftime("%H:%M") == "15:10"
    assert strategy._otm_steps == 1
    assert strategy._single_leg_roll is True
    assert strategy._sl_total == 20_000.0


def test_the_configured_underlying_is_the_nifty_index(worker):
    assert worker.instrument == "NIFTY"
    assert worker.security_id == "13"


def test_this_strategy_names_no_auxiliary_vix_instrument(multi_leg):
    """Spec section 6.1: India VIX is not an input to this strategy."""
    assert multi_leg.vix_security_id == ""


def test_the_underlying_subscribes_on_the_authoritative_index_segment(worker):
    """Spec section 6.2: NIFTY index ticks must use the index segment
    (IDX_I), never the option-tuned adapter default (NSE_FNO) — the initial
    hub subscription for the underlying is what this field drives."""
    from common.market_data.scrip_master import segment_code

    assert worker.security_segment == segment_code("IDX_I")


# ------------------------------------------------------ sizing and contract
def test_order_sizing_is_ten_lots_and_names_no_lot_size(multi_leg):
    assert multi_leg.lots == 10
    assert multi_leg.strategy_kwargs["lots_per_leg"] == 10
    parsed = yaml.safe_load(CONFIG_FILE.read_text())
    assert "lot_size" not in parsed["parameters"]
    assert "lot_size" not in parsed["parameters"]["strategy_kwargs"]
    code_lines = [
        line for line in CONFIG_FILE.read_text().splitlines() if not line.strip().startswith("#")
    ]
    assert not [line for line in code_lines if "lot_size" in line]


def test_contracts_are_resolved_from_the_real_scrip_master(multi_leg):
    assert multi_leg.contract_resolver == "dhan"
    assert multi_leg.strike_step == 50
    assert multi_leg.expiry is None
    assert multi_leg.lot_size == 0, "dhan resolver ignores this; never a hardcoded lot size"


def test_strategy_kwargs_strike_step_agrees_with_the_top_level_one(multi_leg):
    """These two configured copies (spec section 6.4 step 3's otm_steps
    formula vs. the engine's OptionSelector construction) must never drift —
    see the YAML file's own comment for why a mismatch would not fail to
    load, only silently select the wrong OTM strike."""
    assert multi_leg.strategy_kwargs["strike_step"] == multi_leg.strike_step == 50


# ----------------------------------------------------------- trading values
def test_committed_trading_values_match_the_specification(multi_leg):
    kwargs = multi_leg.strategy_kwargs
    assert kwargs["otm_distance_points"] == 50
    assert kwargs["roll_trigger_points"] == 60
    assert kwargs["max_rolls_ce"] == 2
    assert kwargs["max_rolls_pe"] == 2
    assert kwargs["single_leg_roll"] is True
    assert kwargs["combined_stop_per_lot"] == 2000.0
    assert kwargs["blackout_dates"] == []


# ------------------------------------------------------------- session times
def test_the_session_window_is_0915_to_1510_with_a_1515_square_off(worker, multi_leg):
    assert multi_leg.session_start_time == "09:15"
    assert worker.square_off_policy.entry_cutoff.strftime("%H:%M") == "15:10"
    assert worker.square_off_policy.square_off_at.strftime("%H:%M") == "15:15"


def test_square_off_is_exactly_1515_in_the_committed_file():
    parsed = yaml.safe_load(CONFIG_FILE.read_text())
    assert parsed["risk"]["square_off_at"] == "15:15"
    assert parsed["risk"]["entry_cutoff"] == "15:10"
    assert parsed["risk"]["entry_start"] == "09:15"


def test_no_new_roll_or_replacement_is_allowed_at_exactly_1510(worker, multi_leg):
    """Spec section 5: entry_cutoff strictly-before semantics."""
    session = MarketSession(
        SessionConfig.from_square_off_policy(
            worker.square_off_policy,
            start_time=multi_leg.session_start_time,
            holidays=tuple(multi_leg.holidays),
        )
    )
    day = datetime(2026, 8, 24, tzinfo=IST)  # Monday, a trading day
    assert session.can_enter(day.replace(hour=15, minute=9, second=59)) is True
    assert session.can_enter(day.replace(hour=15, minute=10)) is False
    # ...but the session stays open through hard square-off, so an exit and
    # the 15:15 force-close are still reachable.
    assert session.is_open(day.replace(hour=15, minute=10)) is True
    assert session.is_open(day.replace(hour=15, minute=15)) is True


# --------------------------------------------------------------- daily risk
def test_the_generic_engine_daily_guard_stays_disabled(multi_leg):
    """The strategy owns its own gross combined-stop decider (spec section
    10.1) — the generic per-tick DailyRiskGuard must never run alongside it
    as a second, differently-shaped one."""
    assert multi_leg.max_daily_loss_percent is None


# --------------------------------------------------------- trading calendar
def test_the_engine_session_gets_a_verified_2026_nse_holiday_calendar(multi_leg):
    assert len(multi_leg.holidays) == 20
    for entry in multi_leg.holidays:
        assert entry.startswith("2026-")


def test_a_holiday_and_a_weekend_are_both_closed_to_this_strategy(worker, multi_leg):
    session = MarketSession(
        SessionConfig.from_square_off_policy(
            worker.square_off_policy,
            start_time=multi_leg.session_start_time,
            holidays=tuple(multi_leg.holidays),
        )
    )
    midday = {"hour": 11, "minute": 0, "tzinfo": IST}
    assert session.can_enter(datetime(2026, 10, 2, **midday)) is False  # Gandhi Jayanti
    assert session.can_enter(datetime(2026, 8, 22, **midday)) is False  # Saturday
    assert session.can_enter(datetime(2026, 8, 24, **midday)) is True  # ordinary Monday


# -------------------------------------------------------- paper execution
def test_paper_execution_uses_the_current_model_not_the_legacy_zero_slippage(worker):
    """Spec section 13's intentional architecture-level deviation: the legacy
    simulator ran zero slippage and no latency. This uses the same canonical
    intraday paper block c921_ema_cross_buy/supertrend_buy_1_1p2 use."""
    paper = worker.paper_execution
    assert paper["slippage"] == {"options": {"mode": "ticks", "market_order_ticks": 1}}
    assert paper["submission_latency_ms"] == 250
    assert paper["tick_size"] == 0.05
    assert paper["allow_ltp_fallback"] is True
    assert paper["ltp_fallback_extra_ticks"] == 1
    assert paper["max_quote_age_ms"] == 2000

    ema = build_worker_config(
        load_resolved_config(CONFIG_ROOT, RUNTIME_ID, "c921_ema_cross_buy"),
        database_path=Path("/tmp/unused.db"),
        lock_dir=Path("/tmp"),
        pid_dir=Path("/tmp"),
        log_dir=Path("/tmp"),
        trading_date="2026-08-24",
    )
    assert paper == ema.paper_execution


def test_charges_are_the_repository_defaults_not_a_strategy_override(worker):
    assert worker.cost_rates == {}


# -------------------------------------------------- 14. validation boundaries
def _mutated_strategy_kwargs(resolved, **overrides):
    parameters = dict(resolved.strategy.parameters)
    strategy_kwargs = dict(parameters.get("strategy_kwargs") or {})
    strategy_kwargs.update(overrides)
    parameters["strategy_kwargs"] = strategy_kwargs
    return resolved.model_copy(
        update={"strategy": resolved.strategy.model_copy(update={"parameters": parameters})}
    )


def _worker_for(mutated_resolved, tmp_path: Path):
    return build_worker_config(
        mutated_resolved,
        database_path=tmp_path / "db.sqlite",
        lock_dir=tmp_path / "locks",
        pid_dir=tmp_path / "pid",
        log_dir=tmp_path / "logs",
        trading_date="2026-08-24",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"lots_per_leg": 0},
        {"strike_step": 0},
        {"otm_distance_points": 0},
        {"roll_trigger_points": 0},
        {"max_rolls_ce": -1},
        {"max_rolls_pe": -1},
        {"combined_stop_per_lot": 0},
        {"entry_time": "15:10", "stop_new_entries_after": "15:10"},  # not strictly before
        {"stop_new_entries_after": "15:16"},  # after square_off_time
        {"blackout_dates": ["not-a-date"]},
        {"mxa_rolls_ce": 5},  # a plausible typo for max_rolls_ce
    ],
)
def test_invalid_strategy_kwargs_fail_construction_clearly(resolved, tmp_path, overrides):
    mutated = _mutated_strategy_kwargs(resolved, **overrides)
    worker = _worker_for(mutated, tmp_path)
    assert worker.multi_leg_engine is not None
    with pytest.raises(ValueError):
        load_multi_leg_strategy(
            worker.multi_leg_engine.strategy_ref, worker.multi_leg_engine.strategy_kwargs
        )


def test_an_unrecognised_strategy_kwarg_is_refused_not_silently_ignored(resolved, tmp_path):
    """The exact failure mode a misspelled risk-critical field must not
    reach: a typo that would otherwise fall through to the default and ship
    unnoticed."""
    mutated = _mutated_strategy_kwargs(resolved, max_rols_pe=1)  # typo for max_rolls_pe
    worker = _worker_for(mutated, tmp_path)
    assert worker.multi_leg_engine is not None
    with pytest.raises(ValueError, match="unrecognised parameter"):
        load_multi_leg_strategy(
            worker.multi_leg_engine.strategy_ref, worker.multi_leg_engine.strategy_kwargs
        )


def test_valid_blackout_dates_parse_and_construct_cleanly(resolved, tmp_path):
    mutated = _mutated_strategy_kwargs(resolved, blackout_dates=["2026-10-20", "2027-01-01"])
    worker = _worker_for(mutated, tmp_path)
    assert worker.multi_leg_engine is not None
    strategy = load_multi_leg_strategy(
        worker.multi_leg_engine.strategy_ref, worker.multi_leg_engine.strategy_kwargs
    )
    assert isinstance(strategy, RollingStrangleOtm1Strategy)
