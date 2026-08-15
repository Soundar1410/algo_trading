"""Turn a resolved YAML configuration into a picklable :class:`WorkerConfig`.

This is the missing link Phase 5 adds: :func:`~common.config.loader.
load_resolved_config` has existed since Phase 0, but nothing outside a test
ever called it, and nothing turned its result into the ``WorkerConfig`` the
supervisor spawns children from. This module is that adapter.

**Three paths, discriminated by ``StrategyConfig.engine`` (``EngineKind``).**
Until Phase 9 this module was fixture-path only: ``WorkerConfig.engine`` was
left ``None`` on every strategy it built, because populating
:class:`~runtimes.intraday_options.worker.EngineWorkerConfig` needs
per-strategy engine parameters (``strategy_ref``, ``timeframe``,
``strike_step``, ...) that no real strategy existed yet to supply — CLAUDE.md
was explicit that real strategies are Phase 9's job.

Phase 9 landed the first one (``ema_cross_9_21_buy``) discriminated by
``"strategy_ref" in parameters`` rather than by ``EngineKind`` — at that point
``StrategyConfig.engine`` defaulted to ``TRADING_ENGINE`` on *every* strategy,
fixture included, so it could not yet distinguish "wants the ported engine"
from "happens to carry the single-leg engine's default label", and every
committed strategy file set ``engine: trading_engine`` explicitly regardless.

The straddle_920 port makes ``EngineKind`` the real discriminator, **additively**:

* ``EngineKind.TRADING_ENGINE`` (the default) + ``"strategy_ref" in
  parameters`` -> the exact Phase 9 path, byte-for-byte unchanged — this
  branch is untouched by the change below, so ``ema_cross_9_21_buy.yaml`` and
  every other ``trading_engine`` config keep behaving exactly as before
  (pinned by ``tests/unit/test_config_adapter_engine_branch.py``'s existing
  assertions plus a new one asserting the produced ``WorkerConfig`` is
  unaffected by this module's edit).
* ``EngineKind.TRADING_ENGINE`` with no ``strategy_ref`` -> the Phase 1
  fixture path, unchanged.
* ``EngineKind.MULTI_LEG_ENGINE`` -> a real
  :class:`~runtimes.intraday_options.worker.MultiLegEngineWorkerConfig`,
  driving :class:`~common.engine.multi_leg_engine.MultiLegEngine`.
* Any other ``EngineKind`` (``FIXED_STRIKE_ENGINE``, ``STOCK_PORTFOLIO_ENGINE``)
  -> :class:`~common.config.ConfigError` at load time. Unsupported engine
  kinds must fail validation/construction; this adapter never falls back to
  the single-leg engine for one it does not recognise.

Kept out of :mod:`runtimes.intraday_options.worker` on purpose: that module's
own docstring documents a measured import-time budget for the spawned child
(``test_worker_import_boundary.py`` enforces it), and this adapter — pydantic
models, YAML-derived dicts — is an entrypoint-side concern the child never
needs. ``EngineWorkerConfig`` itself is safe to import here at module level:
it is defined in ``worker.py``, deliberately carries no ``common.engine``-owned
type (``test_the_engine_config_carries_no_engine_owned_type`` pins that), and
this adapter only ever *constructs* one — it never imports ``common.engine`` or
``runtimes.intraday_options.engine_worker`` itself.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Any

from common.config import (
    ConfigError,
    EngineKind,
    ExecutionMode,
    ResolvedConfig,
    StrategyConfig,
    fingerprint,
)
from common.market_data.scrip_master import resolve_index_meta, segment_code
from common.risk import SquareOffPolicy

from .worker import EngineWorkerConfig, MultiLegEngineWorkerConfig, WorkerConfig

#: Required keys in ``strategy.parameters`` — neither path can subscribe to a
#: feed or size an order without them, and a missing key here is a
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


def _resolve_security_segment(
    parameters: dict[str, Any], *, strategy_id: str
) -> int:
    """The numeric MarketFeed exchange segment ``parameters['security_id']`` lives in.

    Needed so the *initial* hub subscription for the underlying goes out under
    its real segment (e.g. ``IDX_I``/0 for an index) rather than the feed
    adapter's own default, which is tuned for its *option* subscriptions
    (``NSE_FNO``/2) — see ``common.feed.hub.WorkerChannel.segment``. Reuses the
    same override parameters (``index_security_id``/``index_segment``/
    ``fno_segment``) the engine path already exposes for a live/dynamic
    contract resolution, so one underlying is described in exactly one place.

    Raises:
        ConfigError: the instrument is not in
            ``common.market_data.scrip_master.INDEX_REGISTRY`` and no override
            was supplied — a real feed subscription needs a real segment, so
            this fails at config load rather than silently subscribing nothing.
    """
    instrument = str(parameters["instrument"])
    try:
        meta = resolve_index_meta(
            instrument,
            index_security_id=str(parameters.get("index_security_id") or "") or None,
            index_segment=str(parameters.get("index_segment") or "") or None,
            fno_segment=str(parameters.get("fno_segment") or "") or None,
        )
        return segment_code(meta.segment)
    except (ValueError, KeyError) as exc:
        raise ConfigError(
            f"strategies/{strategy_id}.yaml: cannot resolve the market-feed "
            f"exchange segment for instrument {instrument!r}: {exc}. The initial "
            "underlying subscription needs a real segment to avoid silently "
            "subscribing on the wrong one — set parameters.index_segment/"
            "fno_segment/index_security_id explicitly for an underlying not in "
            "common.market_data.scrip_master.INDEX_REGISTRY."
        ) from exc


def _square_off_policy(strategy: StrategyConfig) -> SquareOffPolicy:
    risk = strategy.risk
    strategy_id = strategy.strategy_id
    kwargs: dict[str, Any] = {}
    if "entry_cutoff" in risk:
        kwargs["entry_cutoff"] = _time_from_hhmm(
            risk["entry_cutoff"], field="entry_cutoff", strategy_id=strategy_id
        )
    if "square_off_at" in risk:
        kwargs["square_off_at"] = _time_from_hhmm(
            risk["square_off_at"], field="square_off_at", strategy_id=strategy_id
        )
    # Phase 6 Part 4: typed StrategyConfig fields, not risk keys — see
    # StrategyConfig's own docstring for why. The config loader has already
    # refused simulate_exchange_settlement, so SquareOffPolicy's own
    # __post_init__ refusal is unreachable from here; it stays as the
    # defence-in-depth check for direct construction.
    kwargs["expiry_policy"] = strategy.expiry_policy
    kwargs["square_off_before_expiry_days"] = strategy.square_off_before_expiry_days
    return SquareOffPolicy(**kwargs)


def build_worker_config(
    cfg: ResolvedConfig,
    *,
    database_path: Path,
    lock_dir: Path,
    pid_dir: Path,
    log_dir: Path,
    trading_date: str,
    live_preflight_passed: bool = False,
    account_shared_database_path: Path | None = None,
    token_cache_dir: Path | None = None,
) -> WorkerConfig:
    """Build the ``WorkerConfig`` for one enabled strategy.

    Args:
        live_preflight_passed: whether live preflight has already run and
            passed for this strategy, this session — computed by the
            caller (``__main__.py::build_supervisor``, via
            ``common.broker.live_preflight_gate.LivePreflightGate``)
            *before* calling this function. Defaults ``False``, so a caller
            that forgets to run preflight — or a paper strategy, for which
            this is simply never checked — gets the fail-closed value.

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

    live_preflight = cfg.runtime.live_preflight
    new_order_rule = next(
        (r for r in live_preflight.rate_limits.rules if r.call_class.value == "new_order"), None
    )

    engine_field: EngineWorkerConfig | None = None
    multi_leg_field: MultiLegEngineWorkerConfig | None = None
    if cfg.strategy.engine is EngineKind.MULTI_LEG_ENGINE:
        multi_leg_field = _build_multi_leg_engine_worker_config(cfg.strategy, parameters)
    elif cfg.strategy.engine is EngineKind.TRADING_ENGINE:
        if "strategy_ref" in parameters:
            engine_field = _build_engine_worker_config(cfg.strategy, parameters)
    else:
        raise ConfigError(
            f"strategies/{strategy_id}.yaml: engine kind {cfg.strategy.engine.value!r} has no "
            "construction path in this runtime yet. Supported kinds are "
            f"{EngineKind.TRADING_ENGINE.value!r} and {EngineKind.MULTI_LEG_ENGINE.value!r}. "
            "Unsupported engine kinds must fail validation rather than silently fall back "
            "to the single-leg engine."
        )

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
        security_segment=_resolve_security_segment(parameters, strategy_id=strategy_id),
        quantity=int(parameters.get("quantity", 50)),
        entry_on_candle=int(parameters.get("entry_on_candle", 1)),
        exit_on_candle=int(parameters.get("exit_on_candle", 3)),
        paper_execution=dict(parameters.get("paper_execution", {})),
        cost_rates=dict(parameters.get("cost_rates", {})),
        square_off_policy=_square_off_policy(cfg.strategy),
        config_fingerprint=fingerprint(cfg),
        engine=engine_field,
        multi_leg_engine=multi_leg_field,
        heartbeat_interval_seconds=cfg.runtime.health.heartbeat_interval_seconds,
        # Phase 10: the real live-gate inputs, threaded through as
        # picklable primitives — see WorkerConfig's own docstring for why
        # this replaced the old always-false stub.
        global_live_trading_enabled=cfg.global_config.live_trading_enabled,
        runtime_enabled=cfg.runtime.enabled,
        runtime_live_execution_allowed=cfg.runtime.live_execution_allowed,
        strategy_enabled=cfg.strategy.enabled,
        strategy_live_approved=cfg.strategy.live_approved,
        live_preflight_passed=live_preflight_passed,
        live_quantity_lots=cfg.strategy.live_quantity_lots,
        live_expected_static_ip=live_preflight.expected_static_ip,
        live_egress_ip_provider=live_preflight.egress_ip_provider,
        live_max_preflight_age_seconds=live_preflight.max_preflight_age_seconds,
        live_rate_limit_rules=tuple(
            (rule.call_class.value, rule.limit, rule.window_seconds)
            for rule in live_preflight.rate_limits.rules
        ),
        live_rate_limit_new_order_limit=(new_order_rule.limit if new_order_rule else None),
        live_rate_limit_new_order_window_seconds=(
            new_order_rule.window_seconds if new_order_rule else None
        ),
        live_max_daily_loss=live_preflight.account_risk.max_daily_loss,
        live_max_open_positions=live_preflight.account_risk.max_open_positions,
        live_max_open_legs=live_preflight.account_risk.max_open_legs,
        live_max_deployed_capital=live_preflight.account_risk.max_deployed_capital,
        live_max_mtm_age_seconds=live_preflight.account_risk.max_mtm_age_seconds,
        account_shared_database_path=account_shared_database_path,
        token_cache_dir=token_cache_dir,
    )


def _build_engine_worker_config(
    strategy: StrategyConfig, parameters: dict[str, Any]
) -> EngineWorkerConfig:
    """Build the ported engine's configuration from ``strategy.parameters``.

    Every field here is read defensively (``.get`` with the same default
    :class:`~runtimes.intraday_options.worker.EngineWorkerConfig` itself would
    use), except ``strategy_ref`` — already guaranteed present by this
    function's only caller — and ``strategy_kwargs``, which travels through
    verbatim: it is the strategy's own constructor's keyword arguments (e.g.
    ``ema_fast``/``lots_per_trade``/... for ``ema_cross_9_21_buy``), and this
    adapter has no business re-deriving or renaming them.
    """
    strategy_id = strategy.strategy_id
    strategy_ref = str(parameters["strategy_ref"])
    if not strategy_ref.strip() or ":" not in strategy_ref:
        raise ConfigError(
            f"strategies/{strategy_id}.yaml parameters.strategy_ref must be "
            f"'package.module:ClassName', got {strategy_ref!r}"
        )
    daily_max_loss_pct = parameters.get("daily_max_loss_pct")
    strategy_kwargs = dict(parameters.get("strategy_kwargs") or {})
    if strategy.mode is ExecutionMode.LIVE:
        if strategy.live_quantity_lots is None:  # defence beyond ResolvedConfig
            raise ConfigError("a live engine strategy has no live_quantity_lots")
        lots = strategy.live_quantity_lots
    else:
        lots = int(
            strategy_kwargs.get(
                "lots_per_trade",
                parameters.get("lots_per_trade", parameters.get("lots", 1)),
            )
        )
    return EngineWorkerConfig(
        strategy_ref=strategy_ref,
        strategy_kwargs=strategy_kwargs,
        timeframe=str(parameters.get("timeframe", "5m")),
        underlying_instrument=str(parameters.get("underlying_instrument", "")),
        # lots_per_trade has exactly ONE configured home: strategy_kwargs,
        # because that is also what the strategy's own constructor reads (see
        # EmaCross9x21BuyStrategy._pick / .quantity_lots) — a second,
        # independently-configured copy at parameters top level is exactly
        # the kind of drift CLAUDE.md's dhanhq-pin rule and this adapter's own
        # square-off-time reasoning both refuse elsewhere. Top-level
        # parameters.lots_per_trade/.lots is accepted only as a fallback, for
        # a future strategy whose constructor does not nest sizing under
        # strategy_kwargs.
        lots=lots,
        strike_step=int(parameters.get("strike_step", 50)),
        lot_size=int(parameters.get("lot_size", 50)),
        expiry=parameters.get("expiry"),
        contract_resolver=str(parameters.get("contract_resolver", "simulated")),
        scrip_master_cache_dir=str(parameters.get("scrip_master_cache_dir", "")),
        index_security_id=str(parameters.get("index_security_id", "")),
        index_segment=str(parameters.get("index_segment", "")),
        fno_segment=str(parameters.get("fno_segment", "")),
        # Spec section 6.4 / 12.1: capital_base * daily_max_loss_pct% is the
        # ABSOLUTE rupee daily_max_loss, evaluated on live MTM (realised + open
        # unrealised) every tick. That conversion and the per-tick evaluation
        # both already live in TradingEngine._build_daily_guard /
        # _on_option_tick (common.engine.daily_guard.DailyRiskGuard) — this
        # adapter only ever passes the two config numbers through unmultiplied.
        starting_capital=float(parameters.get("capital_base", 100_000.0)),
        max_daily_loss_percent=(
            None if daily_max_loss_pct is None else float(daily_max_loss_pct)
        ),
        regime_enabled=bool(parameters.get("regime_enabled", False)),
        warmup_from_history=bool(parameters.get("warmup_from_history", True)),
        warmup_source=str(parameters.get("warmup_source", "none")),
        warmup_max_lookback_sessions=int(parameters.get("warmup_max_lookback_sessions", 3)),
        parameters=dict(parameters),
        session_start_time=str(strategy.risk.get("entry_start", "09:15")),
        holidays=tuple(parameters.get("holidays", ())),
        feed_poll_seconds=float(parameters.get("feed_poll_seconds", 0.5)),
    )


def _build_multi_leg_engine_worker_config(
    strategy: StrategyConfig, parameters: dict[str, Any]
) -> MultiLegEngineWorkerConfig:
    """Build the generic multi-leg engine's configuration from
    ``strategy.parameters``. Mirrors :func:`_build_engine_worker_config`'s
    reasoning; see that function for the fields the two share.

    India VIX (or any other auxiliary market-data instrument a future
    multi-leg strategy names): resolved as a
    :class:`~common.market_data.instruments.MarketDataInstrument` shape
    rather than reusing :class:`~common.market_data.scrip_master.IndexMeta` —
    see that module's docstring for why a fake ``fno_segment`` would put
    false metadata into validated configuration. ``vix_security_id`` empty
    means "this strategy has no auxiliary index"; a strategy that names one
    must also name a real, known segment, or config load fails rather than
    silently subscribing on the wrong one.
    """
    strategy_id = strategy.strategy_id
    strategy_ref = str(parameters.get("strategy_ref", ""))
    if not strategy_ref.strip() or ":" not in strategy_ref:
        raise ConfigError(
            f"strategies/{strategy_id}.yaml parameters.strategy_ref must be "
            f"'package.module:ClassName', got {strategy_ref!r}"
        )
    if strategy.mode is ExecutionMode.LIVE:
        # Correction/spec section 18: exact legacy partial-execution behaviour
        # can leave one open short option leg; any live proposal needs a
        # separate written risk review and approval that has not happened.
        # No committed multi-leg strategy config may reach this — see
        # runtimes/intraday_options/multi_leg_engine_worker.py's docstring.
        raise ConfigError(
            f"strategies/{strategy_id}.yaml: mode: live is not supported for the "
            "multi-leg engine — see runtimes/intraday_options/multi_leg_engine_worker.py"
        )

    daily_max_loss_pct = parameters.get("daily_max_loss_pct")
    strategy_kwargs = dict(parameters.get("strategy_kwargs") or {})
    lots = int(
        strategy_kwargs.get(
            "lots_per_leg", parameters.get("lots_per_leg", parameters.get("lots", 1))
        )
    )

    vix_security_id = str(parameters.get("vix_security_id", "") or "")
    vix_segment = str(parameters.get("vix_index_segment", parameters.get("vix_segment", "IDX_I")))
    vix_mode = parameters.get("vix_mode")
    if vix_security_id:
        try:
            segment_code(vix_segment)
        except KeyError as exc:
            raise ConfigError(
                f"strategies/{strategy_id}.yaml parameters.vix_index_segment "
                f"{vix_segment!r} is not a known exchange segment: {exc}"
            ) from exc

    # P1-4 correction: lot size must never come from a silent, hardcoded
    # production fallback. The "dhan" resolver always takes it from the
    # resolved contract's own metadata (spec section 7) — this config value
    # is never read for it, matching _build_option_selector's own behaviour.
    # The "simulated" resolver has no contract metadata of its own, so it
    # requires an explicit fixture/test lot_size; a strategy config that
    # leaves it out fails to load rather than silently trading a made-up
    # quantity.
    contract_resolver = str(parameters.get("contract_resolver", "simulated"))
    lot_size_raw = parameters.get("lot_size")
    if contract_resolver == "simulated":
        if lot_size_raw is None:
            raise ConfigError(
                f"strategies/{strategy_id}.yaml: parameters.contract_resolver is "
                "'simulated', which requires an explicit parameters.lot_size "
                "(a test/simulated fixture value) — there is no silent production "
                "fallback for it"
            )
        lot_size = int(lot_size_raw)
    else:
        lot_size = 0  # unused: the resolved contract supplies the real lot size.

    return MultiLegEngineWorkerConfig(
        strategy_ref=strategy_ref,
        strategy_kwargs=strategy_kwargs,
        timeframe=str(parameters.get("timeframe", "5m")),
        underlying_instrument=str(parameters.get("underlying_instrument", "")),
        lots=lots,
        strike_step=int(parameters.get("strike_step", 50)),
        lot_size=lot_size,
        expiry=parameters.get("expiry"),
        contract_resolver=contract_resolver,
        scrip_master_cache_dir=str(parameters.get("scrip_master_cache_dir", "")),
        index_security_id=str(parameters.get("index_security_id", "")),
        index_segment=str(parameters.get("index_segment", "")),
        fno_segment=str(parameters.get("fno_segment", "")),
        vix_security_id=vix_security_id,
        vix_segment=vix_segment,
        vix_mode=int(vix_mode) if vix_mode is not None else None,
        starting_capital=float(parameters.get("capital_base", 100_000.0)),
        max_daily_loss_percent=(
            None if daily_max_loss_pct is None else float(daily_max_loss_pct)
        ),
        parameters=dict(parameters),
        session_start_time=str(strategy.risk.get("entry_start", "09:15")),
        holidays=tuple(parameters.get("holidays", ())),
        feed_poll_seconds=float(parameters.get("feed_poll_seconds", 0.5)),
    )
