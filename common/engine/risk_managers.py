"""Concrete per-position risk managers, registered by name.

:mod:`common.engine.risk` ports the abstract ``RiskManager`` + registry with
**no** concrete implementation: "they arrive with Phase 9, alongside the
strategies that select them." This module is that arrival — exactly the
minimal, generic primitive ``c921_ema_cross_buy``'s spec (section 7) asks for:
"the risk manager may be minimal... a hard stop backstop... a RiskManager
**must** be supplied regardless — it is an abstract property — even if every
threshold is set to the disabled token `none`." Nothing else is ported here
(no ``sl_lock_trail``, no target/lock/trail framework) — the primary exit for
that strategy is the premium-candle combined exit; this is only the fast
backstop between its candle closes.

Why a per-lot rupee threshold, not a percentage of premium
------------------------------------------------------------
The spec's own config sketch names this ``catastrophic_stop_pct`` ("e.g. 40 to
floor a long-premium loss"). That cannot be computed honestly under the
existing :class:`~common.engine.risk.RiskManager` contract: :meth:`RiskManager.
on_pnl` receives only the position's absolute rupee P&L, and
:meth:`RiskManager.new_position` receives ``lots`` and ``entry_price`` but
**not** ``lot_size`` or ``quantity`` — deliberately, per its own docstring
("lots scales per-lot thresholds"), because ``lot_size`` is exchange-resolved
at runtime (see ``common.market_data.scrip_master``) and CLAUDE.md forbids
hardcoding it. Recovering a *price* percentage from a *rupee* P&L requires
dividing by quantity, which this manager is never given — inventing a
conversion would either hardcode a lot size (the exact hazard the interface
is built to avoid) or require widening ``RiskManager.new_position``'s
signature, which every other registered/test risk manager would then have to
accept too. So this manager instead follows the contract's own documented
idiom: an absolute rupee floor **per lot**, scaled by ``lots`` (always known).
Disabled (``catastrophic_stop_rupees_per_lot: none``), which is this
strategy's shipped default, it is a true no-op regardless.
"""

from __future__ import annotations

from typing import Any

from common.models import ExitReason

from .risk import RiskManager, opt_float, register_risk_manager


@register_risk_manager("hard_stop")
class HardStopRiskManager(RiskManager):
    """Closes a position once unrealised loss reaches a fixed rupee floor.

    A pure backstop, not a trade-management framework: no target, no lock, no
    trail. ``catastrophic_stop_rupees_per_lot`` (absolute rupees, scaled by the
    lots of the specific position) is the only threshold; any of the
    conventional "disabled" tokens (``none``/``null``/``off``/``""``, via
    :func:`~common.engine.risk.opt_float`) turns it off and makes
    :meth:`on_pnl` an unconditional no-op.

    Args:
        cfg: a mapping (or an object exposing ``.get``) holding
            ``catastrophic_stop_rupees_per_lot``. Accepts a plain ``dict`` (the
            shape every strategy in this repository passes to
            :func:`~common.engine.risk.get_risk_manager`) as well as ``None``
            (fully disabled, matching the abstract property's "must be
            supplied regardless" requirement with the cheapest possible body).
    """

    name = "hard_stop"

    def __init__(self, cfg: Any = None) -> None:
        super().__init__(cfg)
        params: dict[str, Any] = cfg if isinstance(cfg, dict) else {}
        stop = opt_float(params.get("catastrophic_stop_rupees_per_lot"))
        if stop is not None and stop <= 0:
            raise ValueError(
                "catastrophic_stop_rupees_per_lot must be > 0, or a disabled "
                f"token ('none'/'off'/...) — got {stop!r}"
            )
        self._stop_per_lot = stop
        self._lots = 0
        self._armed_stop: float | None = None

    def reset(self) -> None:
        self._lots = 0
        self._armed_stop = None

    def new_position(self, lots: int = 1, *, entry_price: float | None = None) -> None:
        self._lots = lots
        self._armed_stop = None if self._stop_per_lot is None else self._stop_per_lot * lots

    def on_pnl(self, pnl: float) -> ExitReason | None:
        if self._armed_stop is None:
            return None
        return ExitReason.STOP_LOSS if pnl <= -self._armed_stop else None

    @property
    def state(self) -> Any:
        return {
            "lots": self._lots,
            "catastrophic_stop_rupees_per_lot": self._stop_per_lot,
            "armed_stop": self._armed_stop,
        }
