"""Typed configuration: environment secrets, layered YAML, paths and safety gates."""

from __future__ import annotations

# Re-exported alongside the other strategy-config enums (EngineKind,
# ExecutionMode) even though it is defined in common.risk — see models.py's
# import-direction note for why the definition lives there instead.
from common.risk.squareoff import ExpiryPolicy

from .fingerprint import fingerprint
from .loader import (
    ConfigError,
    apply_env_overrides,
    deep_merge,
    discover_enabled_strategies,
    load_global_config,
    load_resolved_config,
    load_runtime_config,
    load_strategy_config,
)
from .models import (
    EngineKind,
    ExecutionMode,
    GlobalConfig,
    HealthConfig,
    LiveGateDecision,
    ResolvedConfig,
    RetentionConfig,
    RuntimeConfig,
    StrategyConfig,
    effective_live_gate,
)
from .paths import ProjectPaths, ProjectRootError, load_paths, resolve_project_root
from .settings import Settings, load_settings

__all__ = [
    "ConfigError",
    "EngineKind",
    "ExecutionMode",
    "ExpiryPolicy",
    "GlobalConfig",
    "HealthConfig",
    "LiveGateDecision",
    "ProjectPaths",
    "ProjectRootError",
    "ResolvedConfig",
    "RetentionConfig",
    "RuntimeConfig",
    "Settings",
    "StrategyConfig",
    "apply_env_overrides",
    "deep_merge",
    "discover_enabled_strategies",
    "effective_live_gate",
    "fingerprint",
    "load_global_config",
    "load_paths",
    "load_resolved_config",
    "load_runtime_config",
    "load_settings",
    "load_strategy_config",
    "resolve_project_root",
]
