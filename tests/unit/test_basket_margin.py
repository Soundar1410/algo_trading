"""The hedged multi-leg margin path.

`weekly_delta_neutral` could never enter because a defined-risk iron condor
was priced leg-by-leg, which values it as two uncovered short options. The
numbers used as fixtures here are the real ones, measured against the live
Dhan API on 9 September 2026 with the same four legs and the same quantity:

    per-leg summed   Rs 61,07,252   152.68% of Rs 40,00,000
    hedged basket    Rs 15,92,934    39.82%

The cap is 50%, so the difference is the whole strategy.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from common.margin import LegMarginRequest, MarginEstimator, MarginUnavailable
from common.market_data.dhan_margin import (
    MULTI_MARGIN_CALCULATOR_URL,
    BasketMargin,
    build_dhan_basket_margin_fetcher,
)
from common.models import OrderSide

ALLOCATED_CAPITAL = 4_000_000.0

#: 9 September 2026, security ids from the strategy's own log line.
NAKED_SUM = 6_107_252.0
HEDGED_TOTAL = 1_592_934.2


def _leg(security_id: str, side: OrderSide) -> LegMarginRequest:
    return LegMarginRequest(
        security_id=security_id,
        exchange_segment="NSE_FNO",
        side=side,
        quantity=1300,  # 20 lots x NIFTY lot size 65
        product_type="MARGIN",
        reference_price=120.0,
    )


def _condor() -> list[LegMarginRequest]:
    return [
        _leg("47288", OrderSide.SELL),
        _leg("47309", OrderSide.SELL),
        _leg("47278", OrderSide.BUY),
        _leg("47321", OrderSide.BUY),
    ]


def _live_response() -> dict[str, object]:
    """The real 200 body, verbatim. Note it is camelCase, while the public
    documentation advertises snake_case — which is why the parser accepts
    both and why this fixture is a copy rather than a paraphrase."""
    return {
        "clientId": "REDACTED",
        "totalMargin": HEDGED_TOTAL,
        "spanMargin": 299455.0,
        "exposure": 1234979.2,
        "equityMargin": 0.0,
        "foMargin": HEDGED_TOTAL,
        "commodity": 0.0,
        "currency": 0.0,
        "hedgeBenefit": 0.0,
        "userFundLimit": 0.0,
        "insufficientFund": 0.0,
    }


def _fetcher(monkeypatch: pytest.MonkeyPatch, handler) -> object:
    captured: dict[str, object] = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return handler(kwargs.get("json"))

    monkeypatch.setattr(httpx, "post", fake_post)
    fetch = build_dhan_basket_margin_fetcher(client_id="CID", access_token="TOKEN")
    return fetch, captured


def _ok(payload: dict[str, object]):
    return httpx.Response(200, json=payload, request=httpx.Request("POST", "http://x"))


# ==================================================================== fetcher
def test_sends_one_request_for_the_whole_basket(monkeypatch: pytest.MonkeyPatch) -> None:
    """One call, not four. That is the entire mechanism by which the broker
    can see the hedges at all."""
    calls = []

    def handler(_body):
        calls.append(1)
        return _ok(_live_response())

    fetch, captured = _fetcher(monkeypatch, handler)
    result = fetch(_condor())

    assert len(calls) == 1
    assert captured["url"] == MULTI_MARGIN_CALCULATOR_URL
    assert result.total_margin == pytest.approx(HEDGED_TOTAL)


def test_sends_every_leg_with_its_own_side_and_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch, captured = _fetcher(monkeypatch, lambda _b: _ok(_live_response()))
    fetch(_condor())

    scrip_list = captured["json"]["scripList"]  # type: ignore[index]
    assert [s["securityId"] for s in scrip_list] == ["47288", "47309", "47278", "47321"]
    assert [s["transactionType"] for s in scrip_list] == ["SELL", "SELL", "BUY", "BUY"]
    assert {s["quantity"] for s in scrip_list} == {1300}
    assert {s["productType"] for s in scrip_list} == {"MARGIN"}


def test_excludes_the_existing_book_from_a_pre_entry_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Folding in open positions and orders would answer a different
    question than "what would this basket cost"."""
    fetch, captured = _fetcher(monkeypatch, lambda _b: _ok(_live_response()))
    fetch(_condor())

    body = captured["json"]
    assert body["includePosition"] is False  # type: ignore[index]
    assert body["includeOrders"] is False  # type: ignore[index]


def test_records_the_brokers_own_breakdown(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch, _ = _fetcher(monkeypatch, lambda _b: _ok(_live_response()))
    components = dict(fetch(_condor()).components)

    assert components["spanMargin"] == pytest.approx(299455.0)
    assert components["exposure"] == pytest.approx(1234979.2)
    # Dhan reports zero hedge benefit even here, where the relief is obvious
    # in spanMargin. Recorded as returned, never read as "hedging applied".
    assert components["hedgeBenefit"] == pytest.approx(0.0)


@pytest.mark.parametrize(
    "payload",
    [
        {"totalMargin": HEDGED_TOTAL},  # live shape (camelCase)
        {"total_margin": HEDGED_TOTAL},  # documented shape (snake_case)
        {"data": {"totalMargin": HEDGED_TOTAL}},  # enveloped
    ],
)
def test_accepts_every_response_shape_seen_or_documented(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    fetch, _ = _fetcher(monkeypatch, lambda _b: _ok(payload))
    assert fetch(_condor()).total_margin == pytest.approx(HEDGED_TOTAL)


def test_an_unrecognisable_payload_raises_rather_than_inventing_a_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch, _ = _fetcher(monkeypatch, lambda _b: _ok({"errorCode": "DH-905"}))
    with pytest.raises(ValueError, match="no recognisable totalMargin"):
        fetch(_condor())


def test_an_empty_basket_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch, _ = _fetcher(monkeypatch, lambda _b: _ok(_live_response()))
    with pytest.raises(ValueError, match="empty leg list"):
        fetch([])


# ================================================================== estimator
def _basket_fetcher(total: float, components: tuple[tuple[str, float], ...] = ()):
    def fetch(_legs: list[LegMarginRequest]) -> BasketMargin:
        return BasketMargin(total_margin=total, components=components)

    return fetch


def test_the_hedged_total_clears_the_cap_the_naked_sum_busts(
) -> None:
    """The regression that matters, in the real numbers."""
    estimator = MarginEstimator(
        margin_fetcher=None, basket_margin_fetcher=_basket_fetcher(HEDGED_TOTAL)
    )
    hedged = estimator.estimate_basket(
        _condor(), spot=25000.0, allocated_capital=ALLOCATED_CAPITAL
    )

    summed = MarginEstimator(margin_fetcher=lambda _leg: NAKED_SUM / 4).estimate_basket(
        _condor(), spot=25000.0, allocated_capital=ALLOCATED_CAPITAL
    )

    assert hedged.utilization_percent == pytest.approx(39.82, abs=0.01)
    assert summed.utilization_percent == pytest.approx(152.68, abs=0.01)
    assert hedged.utilization_percent <= 50.0 < summed.utilization_percent


def test_the_basket_source_is_preferred_over_per_leg_summing() -> None:
    per_leg_calls = []

    def per_leg(leg: LegMarginRequest) -> float:
        per_leg_calls.append(leg)
        return NAKED_SUM / 4

    estimator = MarginEstimator(
        margin_fetcher=per_leg, basket_margin_fetcher=_basket_fetcher(HEDGED_TOTAL)
    )
    estimate = estimator.estimate_basket(
        _condor(), spot=25000.0, allocated_capital=ALLOCATED_CAPITAL
    )

    assert estimate.source == "dhan_margin_calculator_multi"
    assert estimate.estimated_margin == pytest.approx(HEDGED_TOTAL)
    assert per_leg_calls == [], "the per-leg fetcher must not be called as well"


def test_a_failing_basket_source_never_falls_back_to_summing() -> None:
    """The most important guard here. A silent fallback would restore the
    naked total, re-block entry, and look exactly like the original bug —
    a strategy refused every evaluation with nothing explaining why."""

    def broken(_legs: list[LegMarginRequest]) -> BasketMargin:
        raise httpx.ConnectError("boom")

    estimator = MarginEstimator(
        margin_fetcher=lambda _leg: NAKED_SUM / 4, basket_margin_fetcher=broken
    )

    with pytest.raises(MarginUnavailable, match="multi-leg margin-calculator fetch failed"):
        estimator.estimate_basket(
            _condor(), spot=25000.0, allocated_capital=ALLOCATED_CAPITAL
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_an_invalid_basket_total_blocks(bad: float) -> None:
    estimator = MarginEstimator(
        margin_fetcher=None, basket_margin_fetcher=_basket_fetcher(bad)
    )
    with pytest.raises(MarginUnavailable):
        estimator.estimate_basket(
            _condor(), spot=25000.0, allocated_capital=ALLOCATED_CAPITAL
        )


def test_a_basket_estimate_goes_stale_against_a_later_clock() -> None:
    """The estimator self-stamps ``estimated_at`` at computation time, so its
    own internal freshness check can never observe drift — the same
    structural point ``test_margin_estimator.py`` already makes for the
    per-leg path. What matters is that the strategy's re-validation against
    the engine's tick-driven clock (``_entry_margin_estimate``) sees a basket
    estimate go stale at exactly the configured boundary, like any other."""
    stamped = datetime(2026, 9, 9, 5, 0, tzinfo=UTC)
    estimator = MarginEstimator(
        margin_fetcher=None,
        basket_margin_fetcher=_basket_fetcher(HEDGED_TOTAL),
        clock=lambda: stamped,
    )
    estimate = estimator.estimate_basket(
        _condor(), spot=25000.0, allocated_capital=ALLOCATED_CAPITAL
    )

    assert estimate.estimated_at == stamped
    just_inside = datetime.fromtimestamp(stamped.timestamp() + 30.0, tz=UTC)
    just_outside = datetime.fromtimestamp(stamped.timestamp() + 30.1, tz=UTC)
    assert estimate.is_fresh(now=just_inside, max_age_seconds=30.0)
    assert not estimate.is_fresh(now=just_outside, max_age_seconds=30.0)


def test_components_survive_onto_the_estimate() -> None:
    estimator = MarginEstimator(
        margin_fetcher=None,
        basket_margin_fetcher=_basket_fetcher(
            HEDGED_TOTAL, (("spanMargin", 299455.0), ("hedgeBenefit", 0.0))
        ),
    )
    estimate = estimator.estimate_basket(
        _condor(), spot=25000.0, allocated_capital=ALLOCATED_CAPITAL
    )

    assert dict(estimate.components)["spanMargin"] == pytest.approx(299455.0)
    # The basket endpoint returns no per-leg split; nothing invents one.
    assert estimate.per_leg == ()
