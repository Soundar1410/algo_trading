"""``ResolvedConfig`` -> :class:`WorkerConfig` for the ``positional_options``
runtime — the missing link, mirroring
``runtimes.intraday_options.config_adapter``'s own role for the intraday
runtime. Strict: a strategy file missing a required parameter fails to
build a worker config rather than silently defaulting one that would change
what actually trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common.config import ConfigError, EngineKind, ResolvedConfig
from common.engine.config import SessionConfig
from common.market_data.scrip_master import resolve_index_meta, segment_code

_REQUIRED_PARAMETERS = ("underlying", "index_security_id", "index_segment", "fno_segment")


@dataclass
class WorkerConfig:
    """Everything :func:`runtimes.positional_options.worker.build_engine`
    needs. Deliberately plain-old-data — no engine-owned type — the same
    discipline ``runtimes.intraday_options.worker.WorkerConfig`` follows,
    even though this runtime does not (yet) spawn a child process to pickle
    it across."""

    runtime_id: str
    strategy_id: str
    strategy_ref: str
    trading_date: str
    lots: int
    timezone: str
    underlying_security_id: str
    underlying_instrument: str
    underlying_segment: str
    #: The exchange segment (e.g. ``"NSE_FNO"``) every option leg subscribes
    #: on — distinct from ``underlying_segment`` (e.g. ``"IDX_I"``): one
    #: instrument cannot live in two segments, and subscribing an option leg
    #: on the underlying's segment silently delivers nothing (see
    #: :meth:`~common.market_data.adapter.MarketFeedAdapter.subscribe`).
    option_segment: str
    session: SessionConfig
    risk_free_rate: float
    dividend_yield: float
    quote_max_age_seconds: float
    evaluation_interval_seconds: float
    max_adjustments_per_day: int
    max_adjustments_per_cycle: int
    min_minutes_between_adjustments: int
    parameters: dict[str, Any] = field(default_factory=dict)
    paper_execution: dict[str, Any] | None = None
    cost_rates: dict[str, Any] | None = None
    scrip_master_cache_dir: str = ""
    #: Phase 5 (runtime generalization): every field below this line is
    #: ``None`` unless :class:`~runtimes.positional_options.supervisor.
    #: PositionalOptionsSupervisor` fills it in before spawning a child
    #: process for this worker — mirrors ``runtimes.intraday_options.
    #: config_adapter.WorkerConfig`` carrying its own
    #: ``database_path``/``lock_dir``/``pid_dir``/``log_dir`` for exactly
    #: the same reason: a spawned child owns no inherited object, only
    #: picklable plain values, and must open its own database connection,
    #: acquire its own lock and write its own log file. A direct
    #: :func:`~runtimes.positional_options.worker.run_worker`/
    #: :func:`~runtimes.positional_options.worker.build_engine` caller
    #: (every existing test) never sets these and is unaffected.
    database_path: Path | None = None
    lock_dir: Path | None = None
    pid_dir: Path | None = None
    log_dir: Path | None = None
    runtime_root: Path | None = None
    #: Where the scrip-master CSV and the parent's already-minted Dhan
    #: token cache both live — the same directory (``ProjectPaths.
    #: cache_root``) production always uses for both today; kept as one
    #: field, not two, since they are never configured separately.
    cache_dir: Path | None = None
    #: ``"module.submodule:function_name"`` overrides for a spawned
    #: child's own network-facing construction — resolved and called
    #: inside the child (mirrors ``strategy_ref``'s own dotted-path
    #: convention and ``load_positional_strategy``'s resolution), never a
    #: live object pickled across the process boundary. ``None`` (every
    #: production strategy) builds the real Dhan chain fetcher, scrip
    #: master and margin fetcher, exactly as before Phase 5. Set only by a
    #: test's own fixture strategy config, so a real strategy config can
    #: never accidentally construct anything but the real Dhan sources.
    chain_fetcher_factory: str | None = None
    scrip_master_factory: str | None = None
    margin_fetcher_factory: str | None = None


def build_worker_config(
    cfg: ResolvedConfig,
    *,
    trading_date: str,
    timezone: str = "Asia/Kolkata",
    database_path: Path | None = None,
    lock_dir: Path | None = None,
    pid_dir: Path | None = None,
    log_dir: Path | None = None,
    runtime_root: Path | None = None,
    cache_dir: Path | None = None,
    chain_fetcher_factory: str | None = None,
    scrip_master_factory: str | None = None,
    margin_fetcher_factory: str | None = None,
) -> WorkerConfig:
    if cfg.strategy.engine is not EngineKind.POSITIONAL_MULTI_LEG_ENGINE:
        raise ConfigError(
            f"strategy {cfg.strategy.strategy_id!r} has engine={cfg.strategy.engine.value!r}, "
            "but the positional_options runtime only drives "
            f"{EngineKind.POSITIONAL_MULTI_LEG_ENGINE.value!r} strategies."
        )
    parameters = cfg.strategy.parameters
    for key in _REQUIRED_PARAMETERS:
        if key not in parameters:
            raise ConfigError(
                f"strategies/{cfg.strategy.strategy_id}.yaml is missing required "
                f"parameters.{key}"
            )
    if "lot_size" in parameters:
        raise ConfigError(
            f"strategies/{cfg.strategy.strategy_id}.yaml sets parameters.lot_size — "
            "never permitted for a positional strategy (spec section 3.1/10): lot size "
            "is resolved exclusively from the selected contract's own metadata."
        )
    if cfg.strategy.mode.value == "live":
        raise ConfigError(
            f"strategy {cfg.strategy.strategy_id!r} is mode: live — the positional "
            "runtime does not support live execution (spec section 9.7); paper only."
        )

    underlying = str(parameters["underlying"])
    meta = resolve_index_meta(
        underlying,
        index_security_id=str(parameters.get("index_security_id") or "") or None,
        index_segment=str(parameters.get("index_segment") or "") or None,
        fno_segment=str(parameters.get("fno_segment") or "") or None,
    )
    strategy_ref = str(
        parameters.get(
            "strategy_ref",
            f"strategies.positional_options.{cfg.strategy.strategy_id}.strategy:"
            f"{_default_class_name(cfg.strategy.strategy_id)}",
        )
    )

    schedule = parameters.get("schedule", {}) or {}
    adjustment = parameters.get("adjustment", {}) or {}
    holidays = tuple(parameters.get("holidays", ()) or ())

    session = SessionConfig(
        timezone=timezone,
        start_time=str(schedule.get("entry_window_start", "09:25")),
        end_time=str(schedule.get("entry_window_end", "09:40")),
        square_off_time=str(schedule.get("hard_exit", "15:15")),
        holidays=holidays,
    )

    return WorkerConfig(
        runtime_id="positional_options",
        strategy_id=cfg.strategy.strategy_id,
        strategy_ref=strategy_ref,
        trading_date=trading_date,
        lots=int(parameters.get("lots", 1)),
        timezone=timezone,
        underlying_security_id=str(meta.security_id),
        underlying_instrument=underlying,
        underlying_segment=meta.segment,
        option_segment=meta.fno_segment,
        session=session,
        risk_free_rate=float(parameters.get("risk_free_rate", 0.065)),
        dividend_yield=float(parameters.get("dividend_yield", 0.0)),
        quote_max_age_seconds=float(
            (parameters.get("selection", {}) or {}).get("quote_max_age_seconds", 5.0)
        ),
        evaluation_interval_seconds=float(parameters.get("evaluation_interval_seconds", 5.0)),
        max_adjustments_per_day=int(adjustment.get("maximum_per_day", 1)),
        max_adjustments_per_cycle=int(adjustment.get("maximum_per_cycle", 3)),
        min_minutes_between_adjustments=int(adjustment.get("minimum_minutes_between", 90)),
        parameters=parameters,
        database_path=database_path,
        lock_dir=lock_dir,
        pid_dir=pid_dir,
        log_dir=log_dir,
        runtime_root=runtime_root,
        cache_dir=cache_dir,
        chain_fetcher_factory=chain_fetcher_factory,
        scrip_master_factory=scrip_master_factory,
        margin_fetcher_factory=margin_fetcher_factory,
        paper_execution=parameters.get("paper_execution"),
        cost_rates=parameters.get("cost_rates"),
        scrip_master_cache_dir=str(parameters.get("scrip_master_cache_dir", "")),
    )


def _default_class_name(strategy_id: str) -> str:
    return "".join(part.capitalize() for part in strategy_id.split("_")) + "Strategy"


__all__ = ["WorkerConfig", "build_worker_config", "segment_code"]
