"""``common.greeks.service.GreeksService``: chain-first/model-second
precedence, freshness, one consistent snapshot per decision, and the
fail-open-for-exits contract (enforced by never being on an exit's own
decision path — this file proves *this service* never blocks by itself
when a caller simply chooses not to call it)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from common.greeks.models import GreekSource, GreeksUnavailable
from common.greeks.service import GreeksService, ModelAssumptions
from common.market_data.option_chain import OptionChainService
from common.models import OptionType

NOW = datetime(2026, 8, 19, 9, 25, tzinfo=UTC)

_FRESH_CHAIN_PAYLOAD = {
    "data": {
        "last_price": 24000.0,
        "oc": {
            "24000.000000": {
                "ce": {
                    "greeks": {"delta": 0.20, "gamma": 0.001, "theta": -5.0, "vega": 9.0},
                    "implied_volatility": 14.0,
                    "top_bid_price": 100.0,
                    "top_ask_price": 101.0,
                    "oi": 10000,
                    "volume": 5000,
                },
                "pe": {
                    "greeks": {"delta": -0.20, "gamma": 0.001, "theta": -4.5, "vega": 8.5},
                    "implied_volatility": 15.0,
                    "top_bid_price": 95.0,
                    "top_ask_price": 96.0,
                    "oi": 9000,
                    "volume": 4500,
                },
            }
        },
    }
}

_INCOMPLETE_CHAIN_PAYLOAD = {
    "data": {
        "last_price": 24000.0,
        "oc": {
            "24000.000000": {
                "ce": {"implied_volatility": 14.0, "top_bid_price": 100.0, "top_ask_price": 101.0},
                "pe": {},
            }
        },
    }
}


def _service(payload, *, clock=lambda: NOW, max_age_seconds=5.0) -> GreeksService:
    chain_service = OptionChainService(
        lambda security_id, segment, expiry: payload,
        monotonic=lambda: 0.0,
        wall_clock=clock,
    )
    return GreeksService(
        chain_service,
        assumptions=ModelAssumptions(risk_free_rate=0.065, dividend_yield=0.0),
        max_age_seconds=max_age_seconds,
        clock=clock,
    )


_UNDERLYING_ID = 13
_SEGMENT = "IDX_I"
_EXPIRY = "2026-08-21"


def _fetch_chain(service: GreeksService):
    return service.chain_snapshot(
        underlying_security_id=_UNDERLYING_ID, underlying_segment=_SEGMENT, expiry=_EXPIRY
    )


def test_chain_source_is_used_when_complete_and_fresh():
    service = _service(_FRESH_CHAIN_PAYLOAD)
    chain = _fetch_chain(service)
    snapshot = service.resolve(
        chain=chain,
        security_id="X",
        option_type=OptionType.CE,
        strike=24000.0,
        spot=24000.0,
        expiry_at=NOW + timedelta(days=2),
    )
    assert snapshot.source is GreekSource.BROKER_CHAIN
    assert snapshot.delta == 0.20


def test_model_source_is_used_when_chain_greeks_are_incomplete():
    service = _service(_INCOMPLETE_CHAIN_PAYLOAD)
    chain = _fetch_chain(service)
    snapshot = service.resolve(
        chain=chain,
        security_id="X",
        option_type=OptionType.CE,
        strike=24000.0,
        spot=24000.0,
        expiry_at=NOW + timedelta(days=2),
    )
    assert snapshot.source is GreekSource.MODEL
    assert snapshot.model_inputs is not None
    assert snapshot.model_inputs.implied_volatility == pytest.approx(0.14)


def test_model_source_is_used_when_chain_greeks_are_stale():
    later = NOW + timedelta(seconds=10)  # exceeds the 5s default max age
    # The chain was fetched (received) at NOW; the service evaluates 10s
    # later — the snapshot's age at evaluation time is what must trip the
    # freshness gate, so the two clocks are deliberately different here.
    chain_service = OptionChainService(
        lambda security_id, segment, expiry: _FRESH_CHAIN_PAYLOAD,
        monotonic=lambda: 0.0,
        wall_clock=lambda: NOW,
    )
    service = GreeksService(
        chain_service,
        assumptions=ModelAssumptions(risk_free_rate=0.065, dividend_yield=0.0),
        max_age_seconds=5.0,
        clock=lambda: later,
    )
    chain = _fetch_chain(service)
    snapshot = service.resolve(
        chain=chain,
        security_id="X",
        option_type=OptionType.CE,
        strike=24000.0,
        spot=24000.0,
        expiry_at=later + timedelta(days=2),
    )
    assert snapshot.source is GreekSource.MODEL


def test_one_chain_snapshot_serves_every_leg_in_one_decision():
    """Spec section 4.2: all four candidate legs use a consistent evaluation
    snapshot — proven by fetching the chain once and resolving two legs
    against the exact same ChainView object."""
    calls = {"count": 0}

    def fetcher(security_id, segment, expiry):
        calls["count"] += 1
        return _FRESH_CHAIN_PAYLOAD

    chain_service = OptionChainService(fetcher, monotonic=lambda: 0.0, wall_clock=lambda: NOW)
    service = GreeksService(
        chain_service, assumptions=ModelAssumptions(risk_free_rate=0.065), clock=lambda: NOW
    )
    chain = _fetch_chain(service)
    call_snapshot = service.resolve(
        chain=chain,
        security_id="CE24000",
        option_type=OptionType.CE,
        strike=24000.0,
        spot=24000.0,
        expiry_at=NOW + timedelta(days=2),
    )
    put_snapshot = service.resolve(
        chain=chain,
        security_id="PE24000",
        option_type=OptionType.PE,
        strike=24000.0,
        spot=24000.0,
        expiry_at=NOW + timedelta(days=2),
    )
    assert call_snapshot.source_timestamp == put_snapshot.source_timestamp
    assert calls["count"] == 1, "resolve() must never trigger its own chain fetch"


def test_no_usable_source_raises_greeks_unavailable():
    payload = {"data": {"oc": {}}}  # no strikes at all
    service = _service(payload)
    chain = _fetch_chain(service)
    with pytest.raises(GreeksUnavailable):
        service.resolve(
            chain=chain,
            security_id="X",
            option_type=OptionType.CE,
            strike=24000.0,
            spot=24000.0,
            expiry_at=NOW + timedelta(days=2),
        )


def test_a_malformed_chain_payload_raises_greeks_unavailable_at_snapshot_time():
    service = _service({"status": "success"})  # no 'data' key
    with pytest.raises(GreeksUnavailable):
        _fetch_chain(service)
