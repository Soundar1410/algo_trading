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
2. **A live strategy needs a real ``DhanLiveBroker`` construction dependency,
   not just a passing gate.** Phase 10 adds real order placement
   (:class:`~common.broker.dhan_live.DhanLiveBroker`), but a passing gate alone
   is still not enough to build one — the caller must also supply a
   :class:`LiveBrokerDependencies` bundle (client, segment, product type,
   correlation-id-derived identity are all worker-level concerns, wired in
   ``runtimes/intraday_options``, not invented here). Without one, this
   function refuses exactly as it always has — every committed config stays
   incapable of live execution regardless of this module's own capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common.config.models import ExecutionMode, ResolvedConfig, effective_live_gate
from common.logging import get_logger

from .base import Broker
from .dhan_live import DhanLiveBroker, DhanOrderClient
from .paper import InstrumentRulesLookup, PaperBroker
from .quotes import QuoteBook

_log = get_logger(__name__)


class LiveExecutionBlocked(RuntimeError):
    """Raised when a live strategy may not run. Never caught to fall back to paper."""


@dataclass(frozen=True, slots=True)
class LiveBrokerDependencies:
    """Everything :class:`~common.broker.dhan_live.DhanLiveBroker` needs
    that ``build_broker`` cannot derive from ``ResolvedConfig`` alone.
    Constructing one of these at all is itself a worker-level decision
    (Dhan credentials, the resolved contract's exchange segment) — nothing
    in this module manufactures one; its absence is why every committed
    config stays incapable of live execution regardless of the gate.
    """

    client: DhanOrderClient
    exchange_segment: str
    product_type: str


def build_broker(
    cfg: ResolvedConfig,
    *,
    preflight_passed: bool = False,
    paper_execution: dict[str, Any] | None = None,
    cost_rates: dict[str, Any] | None = None,
    quotes: QuoteBook | None = None,
    instrument_rules: InstrumentRulesLookup | None = None,
    live_dependencies: LiveBrokerDependencies | None = None,
) -> Broker:
    """Return the broker for one strategy, or refuse to build one.

    Args:
        cfg: the strategy's fully resolved configuration.
        preflight_passed: result of live preflight. Defaults False so a caller
            that forgets to run it gets a refusal rather than a live broker.
        quotes: recent quotes per instrument, for the paper fill model's latency
            selection and its resting limit orders (Phase 4 Part 5). Optional: a
            broker without one prices every fill at the submission quote and says
            so on each fill, rather than failing.
        instrument_rules: lot size, tick size and any quantity ceiling per
            ``security_id``, for the simulator's instrument and quantity rejection
            rules. Optional, and ``None`` leaves those rules inactive: a runtime
            with no instrument master (every simulated-contract run) must not have
            correct orders refused against a lot size nobody knows.
        live_dependencies: the Dhan client/segment/product-type bundle a real
            ``DhanLiveBroker`` needs. ``None`` (the only value any committed
            config path supplies today) means live execution stays refused
            even for a fully-approved, gate-passing strategy — see
            :class:`LiveBrokerDependencies`.

    Raises:
        LiveExecutionBlocked: if the strategy is live-mode and either the
            gate blocks it or no ``live_dependencies`` were supplied.
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
            quotes=quotes,
            instrument_rules=instrument_rules,
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

    if live_dependencies is None:
        raise LiveExecutionBlocked(
            f"Strategy {cfg.strategy.strategy_id!r} passed the live gate, but no "
            "LiveBrokerDependencies were supplied — refusing rather than guessing a "
            "Dhan client, exchange segment or product type."
        )

    _log.info(
        "routing strategy to DhanLiveBroker strategy_id=%s runtime_id=%s",
        cfg.strategy.strategy_id,
        cfg.runtime.runtime_id,
    )
    return DhanLiveBroker(
        client=live_dependencies.client,
        exchange_segment=live_dependencies.exchange_segment,
        product_type=live_dependencies.product_type,
    )
