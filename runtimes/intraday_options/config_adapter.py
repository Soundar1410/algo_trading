"""Turn a resolved YAML configuration into a picklable :class:`WorkerConfig`.

This is the missing link Phase 5 adds: :func:`~common.config.loader.
load_resolved_config` has existed since Phase 0, but nothing outside a test
ever called it, and nothing turned its result into the ``WorkerConfig`` the
supervisor spawns children from. This module is that adapter.

**Deliberately fixture-path only.** ``WorkerConfig.engine`` is left ``None`` on
every strategy this module builds, regardless of ``StrategyConfig.engine``.
Populating :class:`~runtimes.intraday_options.worker.EngineWorkerConfig` needs
per-strategy engine parameters (``strategy_ref``, ``timeframe``, ``strike_step``,
...) that no real strategy exists yet to supply — CLAUDE.md is explicit that real
strategies are Phase 9's job, and synthesising engine parameters now would
produce exactly the "untested code that merely looks finished" the runbook's
D34 already declined to do for ``EquityScripMaster``. When Phase 9 lands a real
strategy, this module (or Phase 9's own adapter) grows the branch that builds
``EngineWorkerConfig`` from ``StrategyConfig.parameters``.

Kept out of :mod:`runtimes.intraday_options.worker` on purpose: that module's
own docstring documents a measured import-time budget for the spawned child
(``test_worker_import_boundary.py`` enforces it), and this adapter — pydantic
models, YAML-derived dicts — is an entrypoint-side concern the child never
needs.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Any

from common.config import ConfigError, ResolvedConfig, fingerprint
from common.risk import SquareOffPolicy

from .worker import WorkerConfig

#: Required keys in ``strategy.parameters`` — the fixture path cannot subscribe
#: to a feed or size an order without them, and a missing key here is a
#: misconfigured strategy file, not a value worth defaulting.
_REQUIRED_PARAMETERS = ("instrument", "security_id")


def _parameter(parameters: dict[str, Any], key: str, strategy_id: str) -> Any:
    if key not in parameters:
        raise ConfigError(
            f"strategies/{strategy_id}.yaml is missing required parameters.{key}"
        )
    return parameters[key]


def _time_from_hhmm(value: str, *, field: str, strategy_id: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour, minute)
    except (ValueError, AttributeError) as exc:
        raise ConfigError(
            f"strategies/{strategy_id}.yaml risk.{field} must be 'HH:MM', got {value!r}"
        ) from exc


def _square_off_policy(risk: dict[str, Any], strategy_id: str) -> SquareOffPolicy:
    kwargs: dict[str, Any] = {}
    if "entry_cutoff" in risk:
        kwargs["entry_cutoff"] = _time_from_hhmm(
            risk["entry_cutoff"], field="entry_cutoff", strategy_id=strategy_id
        )
    if "square_off_at" in risk:
        kwargs["square_off_at"] = _time_from_hhmm(
            risk["square_off_at"], field="square_off_at", strategy_id=strategy_id
        )
    return SquareOffPolicy(**kwargs)


def build_worker_config(
    cfg: ResolvedConfig,
    *,
    database_path: Path,
    lock_dir: Path,
    pid_dir: Path,
    log_dir: Path,
    trading_date: str,
) -> WorkerConfig:
    """Build the ``WorkerConfig`` for one enabled strategy.

    Raises:
        ConfigError: a required ``parameters`` key is missing, or a risk time
            is not ``HH:MM``. Raised rather than defaulted — a strategy file
            that cannot express what instrument it trades is a configuration
            bug to fix, not a runtime condition to paper over.
    """
    strategy_id = cfg.strategy.strategy_id
    parameters = cfg.strategy.parameters
    for key in _REQUIRED_PARAMETERS:
        _parameter(parameters, key, strategy_id)

    return WorkerConfig(
        runtime_id=cfg.runtime.runtime_id,
        strategy_id=strategy_id,
        security_id=str(parameters["security_id"]),
        instrument=str(parameters["instrument"]),
        database_path=database_path,
        lock_dir=lock_dir,
        pid_dir=pid_dir,
        log_dir=log_dir,
        trading_date=trading_date,
        execution_mode=cfg.strategy.mode,
        quantity=int(parameters.get("quantity", 50)),
        entry_on_candle=int(parameters.get("entry_on_candle", 1)),
        exit_on_candle=int(parameters.get("exit_on_candle", 3)),
        paper_execution=dict(parameters.get("paper_execution", {})),
        cost_rates=dict(parameters.get("cost_rates", {})),
        square_off_policy=_square_off_policy(cfg.strategy.risk, strategy_id),
        config_fingerprint=fingerprint(cfg),
        engine=None,  # Phase 9 boundary — see module docstring.
    )
