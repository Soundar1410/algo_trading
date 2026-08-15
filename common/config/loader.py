"""Layered YAML configuration loading.

Resolution order (spec section 9)::

    defaults -> global YAML -> runtime YAML -> strategy YAML -> permitted env overrides

Layout on disk::

    config/
    ├── global.yaml                       # global: {...}, plus optional *_defaults blocks
    ├── runtimes/<runtime_id>.yaml
    └── strategies/<strategy_id>.yaml

``global.yaml`` and a runtime file may carry ``runtime_defaults`` /
``strategy_defaults`` blocks; a more specific layer overrides them key by key.
That is what makes "every intraday-options strategy gets this risk floor unless
it says otherwise" expressible without repeating it in every strategy file.

Unknown keys are rejected (see :class:`~common.config.models._StrictModel`), so a
mistyped ``live_aproved`` fails at load instead of silently reading as False and
leaving an operator convinced a strategy was approved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import (
    GlobalConfig,
    ResolvedConfig,
    RuntimeConfig,
    StrategyConfig,
)
from .settings import Settings, load_settings


class ConfigError(RuntimeError):
    """Raised when configuration is missing, unreadable or invalid."""


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read one YAML mapping. An empty file is an empty mapping, not an error."""
    if not path.is_file():
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(
            f"Expected a mapping at the top level of {path}, got {type(raw).__name__}"
        )
    return raw


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` onto ``base``, recursing into nested mappings.

    Non-mapping values replace wholesale — a strategy that redefines a ``risk``
    list replaces it rather than appending to an inherited one, which is the
    behaviour an operator expects when reading a single file.
    """
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _build(model: type[Any], data: dict[str, Any], source: str) -> Any:
    """Validate a merged mapping into a typed model, with a locating message."""
    try:
        return model(**data)
    except Exception as exc:  # pydantic ValidationError and friends
        raise ConfigError(f"Invalid configuration for {source}: {exc}") from exc


# ------------------------------------------------------------ env overrides
def _parse_bool(value: str) -> bool | None:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None


def apply_env_overrides(global_config: GlobalConfig, settings: Settings) -> GlobalConfig:
    """Apply the permitted environment overrides.

    Only *one* override is permitted, and it is deliberately one-directional:
    ``ALGO_LIVE_TRADING_ENABLED`` can force global live trading **off**, and can
    never turn it on. An operator needs a fast kill switch that does not require
    editing and re-reading a YAML file; nobody needs to enable real money from
    an environment variable, where a stale shell export would be indistinguishable
    from an intentional decision.
    """
    raw = getattr(settings, "algo_live_trading_enabled", None)
    if raw is None:
        return global_config
    parsed = _parse_bool(str(raw))
    if parsed is False:
        return global_config.model_copy(update={"live_trading_enabled": False})
    return global_config


# ---------------------------------------------------------------- loading
def load_global_config(config_root: Path) -> GlobalConfig:
    """Load ``config/global.yaml``."""
    raw = _read_yaml(config_root / "global.yaml")
    section = raw.get("global", {})
    if not isinstance(section, dict):
        raise ConfigError("The 'global' block in global.yaml must be a mapping")
    result: GlobalConfig = _build(GlobalConfig, section, "global.yaml")
    return result


def load_runtime_config(config_root: Path, runtime_id: str) -> RuntimeConfig:
    """Load one runtime group's configuration, layered under global defaults."""
    global_raw = _read_yaml(config_root / "global.yaml")
    runtime_raw = _read_yaml(config_root / "runtimes" / f"{runtime_id}.yaml")

    defaults = global_raw.get("runtime_defaults", {}) or {}
    if not isinstance(defaults, dict):
        raise ConfigError("'runtime_defaults' in global.yaml must be a mapping")

    merged = deep_merge(defaults, runtime_raw)
    merged.setdefault("runtime_id", runtime_id)

    if merged["runtime_id"] != runtime_id:
        raise ConfigError(
            f"runtime_id mismatch: file runtimes/{runtime_id}.yaml declares "
            f"{merged['runtime_id']!r}. The filename is the identity."
        )
    result: RuntimeConfig = _build(RuntimeConfig, merged, f"runtimes/{runtime_id}.yaml")
    return result


def load_strategy_config(
    config_root: Path,
    strategy_id: str,
    *,
    runtime_id: str | None = None,
) -> StrategyConfig:
    """Load one strategy's configuration, layered under global/runtime defaults.

    ``runtime_id``, when given, is a caller-side expectation to validate
    against — it does **not** decide which runtime's ``strategy_defaults`` to
    layer. That decision belongs entirely to the strategy file's own,
    undefaulted ``runtime_id:`` key, read directly off ``strategy_raw`` below
    before any layering happens: a strategy's runtime membership must never
    be inherited from a defaults block (a global or runtime ``strategy_defaults``
    setting a ``runtime_id`` would let every strategy silently share one
    runtime, exactly the failure mode this field exists to close). A strategy
    file that omits ``runtime_id`` fails pydantic's required-field validation
    inside :func:`_build`; a blank one fails the model's own validator; one
    naming a runtime with no ``config/runtimes/<id>.yaml`` on disk fails here.
    """
    global_raw = _read_yaml(config_root / "global.yaml")
    strategy_raw = _read_yaml(config_root / "strategies" / f"{strategy_id}.yaml")

    declared_runtime_id = strategy_raw.get("runtime_id")
    if not isinstance(declared_runtime_id, str) or not declared_runtime_id.strip():
        raise ConfigError(
            f"strategies/{strategy_id}.yaml is missing a required, non-empty "
            "'runtime_id' — every strategy must declare, in its own file, which "
            "runtime group it belongs to. This is never inherited from "
            "strategy_defaults."
        )
    if runtime_id is not None and declared_runtime_id != runtime_id:
        raise ConfigError(
            f"strategies/{strategy_id}.yaml declares runtime_id={declared_runtime_id!r}, "
            f"which does not match the requested runtime_id={runtime_id!r}."
        )
    if not (config_root / "runtimes" / f"{declared_runtime_id}.yaml").is_file():
        raise ConfigError(
            f"strategies/{strategy_id}.yaml declares runtime_id={declared_runtime_id!r}, "
            f"but config/runtimes/{declared_runtime_id}.yaml does not exist."
        )

    layers: list[dict[str, Any]] = []

    global_defaults = global_raw.get("strategy_defaults", {}) or {}
    if not isinstance(global_defaults, dict):
        raise ConfigError("'strategy_defaults' in global.yaml must be a mapping")
    layers.append(global_defaults)

    runtime_raw = _read_yaml(config_root / "runtimes" / f"{declared_runtime_id}.yaml")
    runtime_defaults = runtime_raw.get("strategy_defaults", {}) or {}
    if not isinstance(runtime_defaults, dict):
        raise ConfigError(
            f"'strategy_defaults' in runtimes/{declared_runtime_id}.yaml must be a mapping"
        )
    layers.append(runtime_defaults)

    layers.append(strategy_raw)

    merged: dict[str, Any] = {}
    for layer in layers:
        merged = deep_merge(merged, layer)
    merged.setdefault("strategy_id", strategy_id)

    if merged["strategy_id"] != strategy_id:
        raise ConfigError(
            f"strategy_id mismatch: file strategies/{strategy_id}.yaml declares "
            f"{merged['strategy_id']!r}. The filename is the identity."
        )
    result: StrategyConfig = _build(StrategyConfig, merged, f"strategies/{strategy_id}.yaml")
    return result


def load_resolved_config(
    config_root: Path,
    runtime_id: str,
    strategy_id: str,
    *,
    settings: Settings | None = None,
) -> ResolvedConfig:
    """Load one strategy's fully layered configuration, as a worker sees it."""
    settings = settings if settings is not None else load_settings()

    global_config = apply_env_overrides(load_global_config(config_root), settings)
    runtime = load_runtime_config(config_root, runtime_id)
    strategy = load_strategy_config(config_root, strategy_id, runtime_id=runtime_id)

    return ResolvedConfig(
        global_config=global_config,
        runtime=runtime,
        strategy=strategy,
    )


def discover_enabled_strategies(
    config_root: Path,
    runtime_id: str,
    *,
    settings: Settings | None = None,
) -> list[ResolvedConfig]:
    """Every strategy in ``config/strategies/`` that belongs to ``runtime_id``
    and is enabled.

    This is a supervisor's entrypoint into configuration: without it,
    :func:`load_resolved_config` has to be called by hand, one ``strategy_id``
    at a time, which is what every caller before Phase 5 did (only tests).

    Strategy files are visited in sorted filename order, so which strategy a
    supervisor tries to spawn first is deterministic across runs.

    Raises:
        ConfigError: the same way :func:`load_resolved_config` does, for any
            strategy file that fails to parse or validate — including one
            that belongs to *this* runtime but is missing/unknown-runtime
            (see :func:`discover_strategies`). A broken strategy file is an
            operator error to fix, not a runtime condition this function
            degrades around — the group does not start until it is fixed,
            rather than starting short one strategy nobody noticed. A
            strategy file belonging to a *different*, valid runtime is not
            an error here — it is simply not returned.
    """
    return [
        cfg
        for cfg in discover_strategies(config_root, runtime_id, settings=settings)
        if cfg.strategy.enabled
    ]


def discover_strategies(
    config_root: Path,
    runtime_id: str,
    *,
    settings: Settings | None = None,
) -> list[ResolvedConfig]:
    """Resolve every strategy file that belongs to ``runtime_id``, including
    disabled strategies.

    Startup uses this wider view solely for live-to-disabled transition
    safety. Worker admission still filters on ``strategy.enabled``.

    **Exact runtime membership, not "every file."** Every strategy file's own
    ``runtime_id`` (required, non-empty — see :class:`~common.config.models.
    StrategyConfig`) is read first, cheaply, before any layering; a file
    naming a *different* runtime is skipped outright — its ``strategy_defaults``
    are never read from the wrong runtime's YAML, and two supervisors can call
    this safely against the same shared ``config/strategies/`` directory. A
    file that is missing ``runtime_id`` entirely, or names a runtime with no
    ``config/runtimes/<id>.yaml`` on disk, is a genuine configuration defect
    and raises — it is never silently skipped, because a typo'd runtime_id
    must not make a strategy quietly invisible to every runtime.
    """
    settings = settings if settings is not None else load_settings()
    strategies_dir = config_root / "strategies"
    if not strategies_dir.is_dir():
        return []

    resolved: list[ResolvedConfig] = []
    for path in sorted(strategies_dir.glob("*.yaml")):
        raw = _read_yaml(path)
        declared_runtime_id = raw.get("runtime_id")
        if not isinstance(declared_runtime_id, str) or not declared_runtime_id.strip():
            raise ConfigError(
                f"strategies/{path.stem}.yaml is missing a required, non-empty "
                "'runtime_id' — every strategy must declare which runtime group "
                "it belongs to."
            )
        if not (config_root / "runtimes" / f"{declared_runtime_id}.yaml").is_file():
            raise ConfigError(
                f"strategies/{path.stem}.yaml declares runtime_id="
                f"{declared_runtime_id!r}, but config/runtimes/{declared_runtime_id}.yaml "
                "does not exist."
            )
        if declared_runtime_id != runtime_id:
            continue
        cfg = load_resolved_config(config_root, runtime_id, path.stem, settings=settings)
        resolved.append(cfg)
    return resolved
