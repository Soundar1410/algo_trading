"""Strategy-wise broker routing, with the safety gate the reference repo lacks.

The `Trading_Automation` factory branches on ``cfg.mode is TradingMode.LIVE`` and
builds a live broker from that alone. This one does not. Routing consults
:func:`~common.config.models.effective_live_gate` and **raises** when a live
strategy is not fully approved (deviation D5).

Two rules make this safe, and they pull in opposite directions from the obvious
implementation:

1. **A blocked live strategy must not fall back to paper.** Silently demoting it
   would leave an operator believing real orders are being placed when they are
   not — the failure would only surface as a missing position days later. The
   strategy refuses to start instead.
2. **Live is unimplemented, not merely gated.** Even a fully approved live
   strategy cannot obtain a broker here, because ``DhanLiveBroker`` order
   placement does not exist until Phase 10. The gate check runs first anyway, so
   the error an operator sees names the real blocker rather than the phase.
"""

from __future__ import annotations

from typing import Any

from common.config.models import ExecutionMode, ResolvedConfig, effective_live_gate
from common.logging import get_logger

from .base import Broker
from .paper import PaperBroker

_log = get_logger(__name__)


class LiveExecutionBlocked(RuntimeError):
    """Raised when a live strategy may not run. Never caught to fall back to paper."""


def build_broker(
    cfg: ResolvedConfig,
    *,
    preflight_passed: bool = False,
    paper_execution: dict[str, Any] | None = None,
    cost_rates: dict[str, Any] | None = None,
) -> Broker:
    """Return the broker for one strategy, or refuse to build one.

    Args:
        cfg: the strategy's fully resolved configuration.
        preflight_passed: result of live preflight. Defaults False so a caller
            that forgets to run it gets a refusal rather than a live broker.

    Raises:
        LiveExecutionBlocked: if the strategy is live-mode. Always, in Phase 1 —
            either because the gate blocks it or because live placement is
            unimplemented.
    """
    if cfg.strategy.mode is ExecutionMode.PAPER:
        _log.info(
            "routing strategy to paper broker strategy_id=%s runtime_id=%s",
            cfg.strategy.strategy_id,
            cfg.runtime.runtime_id,
        )
        return PaperBroker.from_config(
            paper_execution=paper_execution,
            cost_rates=cost_rates,
        )

    decision = effective_live_gate(cfg, preflight_passed=preflight_passed)
    if not decision.allowed:
        reasons = "; ".join(decision.blocked_reasons)
        _log.error(
            "refusing to start live strategy strategy_id=%s reasons=%s",
            cfg.strategy.strategy_id,
            reasons,
        )
        raise LiveExecutionBlocked(
            f"Strategy {cfg.strategy.strategy_id!r} is configured for live execution "
            f"but the live gate blocks it: {reasons}. "
            "This strategy will not start. It is deliberately NOT rerouted to paper — "
            "a live strategy running as paper would misrepresent real exposure."
        )

    # Unreachable while preflight is unimplemented, but written as a hard stop
    # rather than a fallthrough: if a future change ever lets the gate open,
    # this must still refuse until Phase 10 delivers real order placement.
    raise LiveExecutionBlocked(
        f"Strategy {cfg.strategy.strategy_id!r} passed the live gate, but live order "
        "placement is not implemented. DhanLiveBroker order methods arrive in Phase 10."
    )
