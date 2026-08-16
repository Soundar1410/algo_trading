"""``common.margin`` — the real Dhan margin-calculator source (summed per
leg, conservative-by-construction), the entry gate's exact 50% boundary, and
the independent-review correction that the offline fallback model is never
selected automatically: it only ever runs when explicitly injected.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from common.margin import (
    ConservativeMarginModel,
    LegMarginRequest,
    MarginEstimator,
    MarginUnavailable,
)
from common.models import OrderSide

_NOW = datetime(2026, 8, 19, 9, 26, 0, tzinfo=UTC)


def _leg(security_id: str = "1001", side: OrderSide = OrderSide.SELL) -> LegMarginRequest:
    return LegMarginRequest(
        security_id=security_id,
        exchange_segment="NSE_FNO",
        side=side,
        quantity=75,
        product_type="MARGIN",
        reference_price=80.0,
    )


def _legs() -> list[LegMarginRequest]:
    return [
        _leg("1001", OrderSide.BUY),
        _leg("1002", OrderSide.BUY),
        _leg("1003", OrderSide.SELL),
        _leg("1004", OrderSide.SELL),
    ]


# ------------------------------------------------------------- real fetcher
def test_estimator_sums_per_leg_fetcher_results() -> None:
    values = {"1001": 1_000.0, "1002": 2_000.0, "1003": 30_000.0, "1004": 40_000.0}
    estimator = MarginEstimator(
        margin_fetcher=lambda leg: values[leg.security_id], clock=lambda: _NOW
    )
    estimate = estimator.estimate_basket(_legs(), spot=24000.0, allocated_capital=500_000.0)
    assert estimate.estimated_margin == pytest.approx(73_000.0)
    assert estimate.source == "dhan_margin_calculator_summed_legs"
    assert estimate.estimated_at == _NOW
    assert len(estimate.per_leg) == 4


def test_exact_50_percent_boundary_passes() -> None:
    estimator = MarginEstimator(margin_fetcher=lambda leg: 62_500.0, clock=lambda: _NOW)
    estimate = estimator.estimate_basket(_legs(), spot=24000.0, allocated_capital=500_000.0)
    # 4 legs x 62,500 = 250,000 = exactly 50% of 500,000.
    assert estimate.utilization_percent == pytest.approx(50.0)


def test_one_point_above_the_cap() -> None:
    estimator = MarginEstimator(margin_fetcher=lambda leg: 62_600.0, clock=lambda: _NOW)
    estimate = estimator.estimate_basket(_legs(), spot=24000.0, allocated_capital=500_000.0)
    assert estimate.utilization_percent > 50.0


def test_one_point_below_the_cap() -> None:
    estimator = MarginEstimator(margin_fetcher=lambda leg: 62_400.0, clock=lambda: _NOW)
    estimate = estimator.estimate_basket(_legs(), spot=24000.0, allocated_capital=500_000.0)
    assert estimate.utilization_percent < 50.0


# --------------------------------------------------- unavailable/stale/invalid
def test_no_fetcher_and_no_fallback_raises_unavailable() -> None:
    estimator = MarginEstimator(margin_fetcher=None, clock=lambda: _NOW)
    with pytest.raises(MarginUnavailable):
        estimator.estimate_basket(_legs(), spot=24000.0, allocated_capital=500_000.0)


def test_fetcher_raising_blocks_even_with_a_fallback_configured() -> None:
    """Correction: a *configured* live source that fails always raises — it
    is never silently downgraded to the offline model, even if one happens
    to be set."""

    def _boom(_leg: LegMarginRequest) -> float:
        raise RuntimeError("network timeout")

    estimator = MarginEstimator(
        margin_fetcher=_boom, fallback_model=ConservativeMarginModel(), clock=lambda: _NOW
    )
    with pytest.raises(MarginUnavailable):
        estimator.estimate_basket(_legs(), spot=24000.0, allocated_capital=500_000.0)


def test_fetcher_returning_non_finite_value_blocks() -> None:
    estimator = MarginEstimator(margin_fetcher=lambda leg: float("nan"), clock=lambda: _NOW)
    with pytest.raises(MarginUnavailable):
        estimator.estimate_basket(_legs(), spot=24000.0, allocated_capital=500_000.0)


def test_fetcher_returning_negative_value_blocks() -> None:
    estimator = MarginEstimator(margin_fetcher=lambda leg: -1.0, clock=lambda: _NOW)
    with pytest.raises(MarginUnavailable):
        estimator.estimate_basket(_legs(), spot=24000.0, allocated_capital=500_000.0)


def test_stale_estimate_is_not_fresh() -> None:
    """``MarginEstimate.is_fresh`` — a real structural property, not dead
    code: ``MarginEstimator`` always self-stamps ``estimated_at`` at
    computation time (so its own internal freshness check is a defence-in-
    depth pass, not the only place this matters). A caller that re-checks a
    snapshot against a *later* ``now`` — e.g. the strategy re-validating a
    margin decision against the engine's own tick-driven clock, in case a
    real HTTP round trip took a while — must see it go stale exactly at the
    configured boundary."""
    from common.margin import MarginEstimate

    estimate = MarginEstimate(
        estimated_margin=100_000.0,
        source="dhan_margin_calculator_summed_legs",
        estimated_at=_NOW,
        allocated_capital=500_000.0,
    )
    just_inside = _NOW.timestamp() + 30.0
    just_outside = _NOW.timestamp() + 30.1
    assert estimate.is_fresh(
        now=datetime.fromtimestamp(just_inside, tz=UTC), max_age_seconds=30.0
    )
    assert not estimate.is_fresh(
        now=datetime.fromtimestamp(just_outside, tz=UTC), max_age_seconds=30.0
    )


def test_non_finite_estimate_is_never_fresh() -> None:
    from common.margin import MarginEstimate

    estimate = MarginEstimate(
        estimated_margin=float("nan"),
        source="dhan_margin_calculator_summed_legs",
        estimated_at=_NOW,
        allocated_capital=500_000.0,
    )
    assert not estimate.is_fresh(now=_NOW, max_age_seconds=30.0)


# ------------------------------------------------ offline/test-only fallback
def test_fallback_model_only_runs_when_explicitly_injected_and_no_fetcher_set() -> None:
    estimator = MarginEstimator(
        margin_fetcher=None, fallback_model=ConservativeMarginModel(), clock=lambda: _NOW
    )
    estimate = estimator.estimate_basket(_legs(), spot=24000.0, allocated_capital=500_000.0)
    assert estimate.source == "conservative_model_v1"
    assert estimate.estimated_margin > 0
    # Never equal to a fabricated zero.
    assert estimate.estimated_margin != 0.0


def test_conservative_model_never_wired_by_the_estimator_on_its_own() -> None:
    """The estimator itself never constructs a ConservativeMarginModel —
    only a caller that explicitly passes one gets offline behaviour."""
    estimator = MarginEstimator(margin_fetcher=None, clock=lambda: _NOW)
    assert estimator._fallback_model is None
