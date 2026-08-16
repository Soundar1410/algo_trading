"""Exact boundary proofs for every threshold in the exit-priority ladder
(spec section 6.2/6.3, spec section 13.4) — ``risk.py``'s pure functions
directly, so a boundary can be proven to the cent without needing the full
engine to reach it. The *real* engine producing an actual exit action when
a threshold is crossed is proven separately, end-to-end, in
``tests/integration/test_weekly_delta_neutral_pnl_and_exits.py``.
"""

from __future__ import annotations

from common.config.models import ExecutionMode
from common.engine.positional.positional_models import Cycle
from strategies.positional_options.weekly_delta_neutral.config import ExitsConfig
from strategies.positional_options.weekly_delta_neutral.risk import (
    compute_pnl,
    is_capital_stop,
    is_emergency_stop,
    is_hard_stop,
    is_margin_breach,
    is_profit_target,
    is_soft_stop,
)

#: An arbitrary, fixed original credit — only its multiples matter here.
_B0 = 8910.0
_EXITS = ExitsConfig()  # spec section 6.3 defaults: 55% / 1.25x / 1.50x / 1.75x / 1% / 50%
_ALLOCATED_CAPITAL = 500_000.0


def _cycle_with_credit(credit: float = _B0) -> Cycle:
    return Cycle(
        cycle_id="wdn:test", strategy_id="weekly_delta_neutral",
        execution_mode=ExecutionMode.PAPER, underlying="NIFTY",
        resolved_expiry_date="2026-08-26", opened_trading_date="2026-08-19",
        original_net_credit=credit,
    )


def _pnl_for_net(net: float):  # type: ignore[no-untyped-def]
    """A PnlSnapshot with the given net_strategy_pnl, charges already
    folded in (compute_pnl's own job — this helper skips straight to its
    output, since risk.py's is_*_stop functions only ever read the
    resulting PnlSnapshot, never realised/unrealised/charges separately)."""
    from strategies.positional_options.weekly_delta_neutral.risk import PnlSnapshot

    return PnlSnapshot(
        realised_pnl=net, unrealised_pnl=0.0, charges=0.0, net_strategy_pnl=net,
        credit_captured_percent=None, loss_amount=max(0.0, -net),
    )


def test_compute_pnl_nets_realised_unrealised_and_charges() -> None:
    cycle = _cycle_with_credit()
    pnl = compute_pnl(cycle, charges=123.0)
    # No legs are open/closed on this bare cycle fixture — realised=0,
    # unrealised=0 — proves the formula's shape (net = R+U-charges), not a
    # specific leg P&L (that is exercised end-to-end in the integration
    # suite, including a closed *adjustment* leg's own realised P&L).
    assert pnl.net_strategy_pnl == -123.0
    assert pnl.charges == 123.0
    assert pnl.loss_amount == 123.0


def _boundary_cases():  # type: ignore[no-untyped-def]
    return [
        ("profit", _EXITS.profit_credit_capture_percent / 100.0 * _B0, is_profit_target, True),
        ("soft", _EXITS.soft_loss_credit_multiple * _B0, is_soft_stop, False),
        ("hard", _EXITS.hard_loss_credit_multiple * _B0, is_hard_stop, False),
        ("emergency", _EXITS.emergency_loss_credit_multiple * _B0, is_emergency_stop, False),
    ]


def test_exact_boundaries_pass_one_point_below_and_above() -> None:
    cycle = _cycle_with_credit()
    for name, threshold, check, is_profit in _boundary_cases():
        net_at = threshold if is_profit else -threshold
        net_below = (threshold - 1.0) if is_profit else -(threshold - 1.0)
        net_above = (threshold + 1.0) if is_profit else -(threshold + 1.0)

        assert check(cycle, _pnl_for_net(net_at), _EXITS), f"{name}: exact boundary must trigger"
        assert not check(cycle, _pnl_for_net(net_below), _EXITS), (
            f"{name}: one point inside the boundary must not trigger"
        )
        assert check(cycle, _pnl_for_net(net_above), _EXITS), (
            f"{name}: one point past the boundary must still trigger"
        )


def test_capital_stop_exact_boundary() -> None:
    threshold = _EXITS.maximum_cycle_loss_capital_percent / 100.0 * _ALLOCATED_CAPITAL
    assert threshold == 5_000.0  # spec section 6.3's own worked example
    at = _pnl_for_net(-threshold)
    below = _pnl_for_net(-(threshold - 1.0))
    above = _pnl_for_net(-(threshold + 1.0))
    assert is_capital_stop(at, _EXITS, allocated_capital=_ALLOCATED_CAPITAL)
    assert not is_capital_stop(below, _EXITS, allocated_capital=_ALLOCATED_CAPITAL)
    assert is_capital_stop(above, _EXITS, allocated_capital=_ALLOCATED_CAPITAL)


def test_margin_exact_boundary() -> None:
    """The ongoing exit-priority margin-breach check (spec 6.2 step 10) —
    the entry-gate's own 50% boundary is already covered exhaustively by
    tests/unit/test_margin_estimator.py's own exact/stale/unavailable/
    over-limit cases; this is the *same* threshold, re-used here."""
    at = 0.50 * _ALLOCATED_CAPITAL
    below = at - 1.0
    above = at + 1.0
    assert not is_margin_breach(
        estimated_margin=at, allocated_capital=_ALLOCATED_CAPITAL, config=_EXITS
    ), "spec 3.7/6.3: the boundary itself (<=50%) is not yet a breach"
    assert not is_margin_breach(
        estimated_margin=below, allocated_capital=_ALLOCATED_CAPITAL, config=_EXITS
    )
    assert is_margin_breach(
        estimated_margin=above, allocated_capital=_ALLOCATED_CAPITAL, config=_EXITS
    )
    # Missing estimate: never fabricated into a breach (this strategy's own
    # ongoing-loop no-op, spec section 4.2's fail-open-for-exits rule).
    assert not is_margin_breach(
        estimated_margin=None, allocated_capital=_ALLOCATED_CAPITAL, config=_EXITS
    )


def test_zero_or_negative_credit_never_fabricates_a_profit_or_soft_hard_emergency_trigger() -> None:
    """A cycle whose original credit could not be established (None, or
    somehow non-positive) must never let a stop/target compute a
    fabricated threshold from it — spec section 3.7/6.1 requires a valid,
    positive, persisted credit before any of these are meaningful."""
    for credit in (None, 0.0, -100.0):
        cycle = _cycle_with_credit(credit) if credit is not None else _cycle_with_credit()
        if credit is None:
            cycle.original_net_credit = None
        else:
            cycle.original_net_credit = credit
        huge_loss = _pnl_for_net(-1_000_000.0)
        assert not is_profit_target(cycle, _pnl_for_net(1_000_000.0), _EXITS)
        assert not is_soft_stop(cycle, huge_loss, _EXITS)
        assert not is_hard_stop(cycle, huge_loss, _EXITS)
        assert not is_emergency_stop(cycle, huge_loss, _EXITS)
