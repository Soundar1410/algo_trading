"""The PAPER-only gate. Every check must pass before any runtime starts.

This facility is paper-only by construction, not by convention. It does not
consult a "should we go live" flag — it *asserts* that every live gate is off
and refuses the whole start otherwise. There is deliberately no path here that
reroutes a live-designated strategy into paper: a strategy configured
``mode: live`` blocks the entire automatic start, exactly as
:func:`common.config.effective_live_gate` blocks it elsewhere, rather than
being quietly downgraded and traded anyway.

Nothing here writes configuration. It reads what the operator committed, and
either agrees to proceed or names what is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from common.config import (
    ConfigError,
    ExecutionMode,
    discover_enabled_strategies,
    load_global_config,
    load_runtime_config,
)
from common.logging import get_logger
from common.process import legacy_system_status
from scripts import assert_no_live_config_committed, validate_environment

from .retry import TerminalStartupError

_log = get_logger(__name__)


@dataclass(frozen=True)
class RuntimePlan:
    """One enabled runtime group and the enabled strategies under it."""

    runtime_id: str
    strategy_ids: tuple[str, ...]


@dataclass(frozen=True)
class PaperSafetyReport:
    """What may start, or why nothing may."""

    plans: tuple[RuntimePlan, ...]
    violations: tuple[str, ...] = field(default_factory=tuple)

    @property
    def safe(self) -> bool:
        return not self.violations

    @property
    def runtime_ids(self) -> tuple[str, ...]:
        return tuple(plan.runtime_id for plan in self.plans)

    @property
    def strategy_ids(self) -> tuple[str, ...]:
        return tuple(sid for plan in self.plans for sid in plan.strategy_ids)

    def raise_if_unsafe(self) -> None:
        if self.violations:
            raise TerminalStartupError(
                "automatic startup refused — paper-safety violations: "
                + "; ".join(self.violations)
            )


#: The runtime groups this platform knows how to start. Kept as the registry's
#: own keys rather than a second list, so adding a runtime is one entry in
#: ``scripts._runtimes`` and nothing here.
def _known_runtime_ids() -> tuple[str, ...]:
    from scripts._runtimes import RUNTIMES

    return tuple(sorted(RUNTIMES))


def verify_paper_only(
    config_root: Path,
    *,
    check_legacy: bool = True,
    check_environment: bool = True,
) -> PaperSafetyReport:
    """Discover what is enabled and prove all of it is paper-safe."""
    violations: list[str] = []
    plans: list[RuntimePlan] = []

    global_config = load_global_config(config_root)
    if global_config.live_trading_enabled:
        violations.append("global.live_trading_enabled is true")

    for runtime_id in _known_runtime_ids():
        runtime_path = config_root / "runtimes" / f"{runtime_id}.yaml"
        if not runtime_path.is_file():
            continue
        runtime = load_runtime_config(config_root, runtime_id)
        if not runtime.enabled:
            _log.info("auto-start: runtime %s is disabled; skipping", runtime_id)
            continue
        if runtime.live_execution_allowed:
            violations.append(f"runtime {runtime_id} has live_execution_allowed: true")

        try:
            strategies = discover_enabled_strategies(config_root, runtime_id)
        except (ConfigError, ValidationError) as exc:
            # A strategy that will not even load is a refusal, not a crash. The
            # strict config models reject some live shapes outright (a
            # ``mode: live`` strategy with no ``live_quantity_lots``, say), so
            # this path is reachable precisely when something live-ish is
            # misconfigured — exactly when blocking matters most.
            violations.append(f"runtime {runtime_id} has an unloadable strategy config: {exc}")
            continue

        for resolved in strategies:
            strategy = resolved.strategy
            if strategy.mode is not ExecutionMode.PAPER:
                violations.append(f"strategy {strategy.strategy_id} has mode: {strategy.mode}")
            if strategy.live_approved:
                violations.append(f"strategy {strategy.strategy_id} has live_approved: true")

        plans.append(
            RuntimePlan(
                runtime_id=runtime_id,
                strategy_ids=tuple(r.strategy.strategy_id for r in strategies),
            )
        )

    # The committed-config guard, reused rather than reimplemented: it is the
    # same assertion CI runs, so the two can never disagree about what "no live
    # config committed" means.
    if (
        assert_no_live_config_committed.main([str(config_root)])
        != assert_no_live_config_committed.EXIT_OK
    ):
        violations.append("scripts.assert_no_live_config_committed reported problems")

    if check_legacy:
        legacy = legacy_system_status()
        if legacy.active:
            detail = "could not be verified" if legacy.undetermined else legacy.describe()
            violations.append(
                f"legacy Trading_Automation {detail}. Automatic startup refuses while the "
                "legacy system may be running. To clear it manually: "
                "launchctl bootout gui/$UID/com.soundarraj.tradingautomation.starttrading"
            )

    if check_environment:
        for plan in plans:
            code = validate_environment.main(["--runtime-id", plan.runtime_id])
            if code != validate_environment.EXIT_OK:
                violations.append(
                    f"scripts.validate_environment failed for runtime {plan.runtime_id}"
                )

    if not plans and not violations:
        _log.info("auto-start: no runtime is enabled; nothing to start")

    return PaperSafetyReport(plans=tuple(plans), violations=tuple(violations))
