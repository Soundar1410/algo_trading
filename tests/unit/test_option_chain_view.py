"""``common.market_data.chain_view``: typed parse of a Dhan Option Chain
payload — no synthetic bid/ask, fails closed on a malformed structure."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from common.market_data.chain_view import ChainPayloadError, ChainQuote, parse_chain_payload
from common.models import OptionType

NOW = datetime(2026, 8, 19, 9, 25, tzinfo=UTC)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

_VALID_PAYLOAD = {
    "status": "success",
    "data": {
        "last_price": 24000.5,
        "oc": {
            "24000.000000": {
                "ce": {
                    "greeks": {"delta": 0.52, "gamma": 0.001, "theta": -5.2, "vega": 10.1},
                    "implied_volatility": 14.2,
                    "last_price": 120.5,
                    "oi": 500000,
                    "top_ask_price": 121.0,
                    "top_bid_price": 120.0,
                    "volume": 25000,
                },
                "pe": {
                    "greeks": {"delta": -0.48, "gamma": 0.001, "theta": -4.9, "vega": 10.0},
                    "implied_volatility": 14.6,
                    "last_price": 118.0,
                    "oi": 480000,
                    "top_ask_price": 118.5,
                    "top_bid_price": 117.5,
                    "volume": 22000,
                },
            },
            "23900.000000": {
                "ce": {"last_price": 150.0, "oi": 1000, "volume": 500},
                "pe": {"last_price": 90.0, "oi": 1200, "volume": 600},
            },
        },
    },
}


def test_parses_underlying_last_price():
    view = parse_chain_payload(_VALID_PAYLOAD, snapshot_at=NOW, received_at=NOW)
    assert view.underlying_last_price == 24000.5


def test_parses_every_strike_sorted():
    view = parse_chain_payload(_VALID_PAYLOAD, snapshot_at=NOW, received_at=NOW)
    assert [row.strike for row in view.strikes] == [23900.0, 24000.0]


def test_parses_bid_ask_greeks_oi_volume_for_a_complete_strike():
    view = parse_chain_payload(_VALID_PAYLOAD, snapshot_at=NOW, received_at=NOW)
    row = view.strike(24000.0)
    assert row is not None
    ce = row.side(OptionType.CE)
    assert ce.bid == 120.0
    assert ce.ask == 121.0
    assert ce.delta == 0.52
    assert ce.gamma == 0.001
    assert ce.theta == -5.2
    assert ce.vega == 10.1
    assert ce.implied_volatility == 14.2
    assert ce.open_interest == 500000
    assert ce.volume == 25000
    assert ce.has_complete_quote is True
    assert ce.has_complete_greeks is True


def test_a_strike_with_no_bid_ask_has_no_synthetic_quote():
    """Spec section 3.6: never synthesize a bid/ask spread from LTP."""
    view = parse_chain_payload(_VALID_PAYLOAD, snapshot_at=NOW, received_at=NOW)
    row = view.strike(23900.0)
    assert row is not None
    ce = row.side(OptionType.CE)
    assert ce.bid is None
    assert ce.ask is None
    assert ce.has_complete_quote is False
    assert ce.has_complete_greeks is False
    assert ce.last_price == 150.0  # last_price is parsed even without a book


def test_a_crossed_quote_is_not_complete():
    payload = {
        "data": {
            "oc": {
                "100.000000": {
                    "ce": {"top_bid_price": 10.0, "top_ask_price": 9.0},
                    "pe": {},
                }
            }
        }
    }
    view = parse_chain_payload(payload, snapshot_at=NOW, received_at=NOW)
    row = view.strike(100.0)
    assert row is not None
    assert row.side(OptionType.CE).has_complete_quote is False


def test_a_zero_bid_is_not_complete():
    payload = {
        "data": {
            "oc": {
                "100.000000": {
                    "ce": {"top_bid_price": 0.0, "top_ask_price": 5.0},
                    "pe": {},
                }
            }
        }
    }
    view = parse_chain_payload(payload, snapshot_at=NOW, received_at=NOW)
    row = view.strike(100.0)
    assert row is not None
    assert row.side(OptionType.CE).has_complete_quote is False


def test_missing_data_object_fails_closed():
    with pytest.raises(ChainPayloadError, match="data"):
        parse_chain_payload({"status": "success"}, snapshot_at=NOW, received_at=NOW)


def test_missing_oc_object_fails_closed():
    with pytest.raises(ChainPayloadError, match="oc"):
        parse_chain_payload({"data": {"last_price": 100.0}}, snapshot_at=NOW, received_at=NOW)


def test_a_non_numeric_strike_key_fails_closed():
    payload = {"data": {"oc": {"not-a-number": {"ce": {}, "pe": {}}}}}
    with pytest.raises(ChainPayloadError, match="not-a-number"):
        parse_chain_payload(payload, snapshot_at=NOW, received_at=NOW)


def test_an_empty_but_structurally_valid_chain_is_not_an_error():
    payload = {"data": {"oc": {}}}
    view = parse_chain_payload(payload, snapshot_at=NOW, received_at=NOW)
    assert view.strikes == ()


# ============== Phase 4 gap-closing session: real-shape regression fixture
def test_parses_the_sanitized_real_shaped_fixture():
    """Permanent parser regression test (spec 4.2/11.3, Phase 4): loads
    ``tests/fixtures/dhan_option_chain_sample.json``, a sanitized payload
    whose envelope/field shape was verified against a *real* Dhan
    ``/v2/optionchain`` response by ``scripts/verify_dhan_option_chain.py``
    on 2026-08-16 (see that fixture's own ``_fixture_note`` and
    ``docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md`` section 11.10 Phase 4)
    — not a hand-typed shape this module merely hopes matches production.
    """
    payload = json.loads((_FIXTURES / "dhan_option_chain_sample.json").read_text())
    view = parse_chain_payload(payload, snapshot_at=NOW, received_at=NOW)

    assert view.underlying_last_price == 24350.0
    assert len(view.strikes) == 5, "every raw 'oc' strike key must parse, none dropped"

    # A liquid, near-the-money strike: complete two-sided quote and greeks,
    # every extra field the real response carries (average_price,
    # previous_close_price/_oi/_volume, security_id, top_*_quantity) simply
    # ignored rather than tripping the parser.
    row = view.strike(24350.0)
    assert row is not None
    ce = row.side(OptionType.CE)
    assert ce.has_complete_quote is True
    assert ce.has_complete_greeks is True
    assert 0.0 <= ce.delta <= 1.0  # type: ignore[operator]
    assert ce.bid is not None and ce.ask is not None and ce.bid <= ce.ask
    pe = row.side(OptionType.PE)
    assert pe.has_complete_quote is True
    assert -1.0 <= pe.delta <= 0.0  # type: ignore[operator]

    # A deep-OTM strike the real chain reports with an all-zero book
    # (bid/ask/oi/volume/greeks literally 0, never absent) — has_complete_
    # quote correctly rejects a zero bid rather than treating 0 as "no
    # data" (spec section 3.6: never synthesize a spread), and this is the
    # one place a real Dhan quirk (0, not null/missing, for "no book")
    # would have silently broken that guarantee if the parser assumed
    # "missing" meant an absent key.
    far = view.strike(18500.0)
    assert far is not None
    far_ce = far.side(OptionType.CE)
    assert far_ce.bid == 0.0
    assert far_ce.has_complete_quote is False
    # Phase 4A correction: an all-zero-and-IV-zero greeks set (this
    # strike's own CE leg — no real book at all) is now correctly rejected
    # as garbage, not accepted as "complete" merely because no field is
    # None. See test_has_complete_greeks_rejects_all_zero_values below for
    # the dedicated boundary coverage.
    assert far_ce.has_complete_greeks is False
    # Dhan's own real response can carry a nonzero IV on a strike whose
    # greeks are otherwise all zero (observed on the live PE leg this
    # fixture mirrors) — has_complete_greeks still rejects it (all-zero
    # delta/gamma/theta/vega alone is disqualifying, independent of IV).
    far_pe = far.side(OptionType.PE)
    assert far_pe.implied_volatility == 10.5
    assert far_pe.has_complete_quote is False
    assert far_pe.has_complete_greeks is False


# ================================== Phase 4A: has_complete_greeks boundaries
#
# Dedicated, isolated coverage for ChainQuote.has_complete_greeks (spec
# section 4.2; Phase 4A correction, 16 August 2026 gap-closing session) —
# every case built as a single one-strike payload through the real
# parse_chain_payload, never by constructing ChainQuote directly, so these
# stay proof of the actual parser's behaviour, not of some hand-built
# dataclass shortcut.
def _one_strike_payload(ce_leg: dict, pe_leg: dict | None = None) -> dict:
    return {
        "data": {
            "last_price": 24000.0,
            "oc": {"24000.000000": {"ce": ce_leg, "pe": pe_leg or {}}},
        }
    }


def _ce(payload: dict) -> ChainQuote:
    view = parse_chain_payload(payload, snapshot_at=NOW, received_at=NOW)
    row = view.strike(24000.0)
    assert row is not None
    return row.side(OptionType.CE)


def _pe(payload: dict) -> ChainQuote:
    view = parse_chain_payload(payload, snapshot_at=NOW, received_at=NOW)
    row = view.strike(24000.0)
    assert row is not None
    return row.side(OptionType.PE)


_VALID_CE_LEG = {
    "greeks": {"delta": 0.55, "gamma": 0.0015, "theta": -24.9, "vega": 8.0},
    "implied_volatility": 13.4,
    "top_bid_price": 125.3, "top_ask_price": 125.8,
}
_VALID_PE_LEG = {
    "greeks": {"delta": -0.42, "gamma": 0.0022, "theta": -10.7, "vega": 7.9},
    "implied_volatility": 8.6,
    "top_bid_price": 54.2, "top_ask_price": 54.45,
}


def test_has_complete_greeks_accepts_a_valid_ce_set():
    assert _ce(_one_strike_payload(_VALID_CE_LEG)).has_complete_greeks is True


def test_has_complete_greeks_accepts_a_valid_pe_set():
    assert _pe(_one_strike_payload({}, _VALID_PE_LEG)).has_complete_greeks is True


def test_has_complete_greeks_rejects_all_zero_values():
    """The exact real-response fingerprint verification found: every field
    present, every one of delta/gamma/theta/vega literally zero — even
    with a plausible IV, since IV alone was never the disqualifying field
    on the real deep-OTM sample this mirrors."""
    leg = {
        "greeks": {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0},
        "implied_volatility": 12.0,
        "top_bid_price": 0.0, "top_ask_price": 0.0,
    }
    assert _ce(_one_strike_payload(leg)).has_complete_greeks is False


@pytest.mark.parametrize("iv", [0.0, -0.01, -5.0])
def test_has_complete_greeks_rejects_zero_or_negative_iv(iv: float):
    leg = {**_VALID_CE_LEG, "implied_volatility": iv}
    assert _ce(_one_strike_payload(leg)).has_complete_greeks is False


@pytest.mark.parametrize(
    "field", ["delta", "gamma", "theta", "vega"],
)
def test_has_complete_greeks_rejects_nan_or_infinite_greeks(field: str):
    for bad in (float("nan"), float("inf"), float("-inf")):
        greeks = dict(_VALID_CE_LEG["greeks"])
        greeks[field] = bad
        leg = {**_VALID_CE_LEG, "greeks": greeks}
        assert _ce(_one_strike_payload(leg)).has_complete_greeks is False, (field, bad)


def test_has_complete_greeks_rejects_nan_iv():
    leg = {**_VALID_CE_LEG, "implied_volatility": float("nan")}
    assert _ce(_one_strike_payload(leg)).has_complete_greeks is False


def test_has_complete_greeks_accepts_small_but_genuine_nonzero_greeks():
    """A real deep-OTM/near-expiry option's true Greeks can be tiny —
    never rejected merely for smallness, only for being *all four*
    exactly zero at once."""
    leg = {
        "greeks": {"delta": 0.0004, "gamma": 0.00002, "theta": -0.0009, "vega": 0.0007},
        "implied_volatility": 22.0,
        "top_bid_price": 0.05, "top_ask_price": 0.1,
    }
    assert _ce(_one_strike_payload(leg)).has_complete_greeks is True


@pytest.mark.parametrize("delta", [-0.0001, 1.0001, 5.0])
def test_has_complete_greeks_rejects_ce_delta_outside_0_1(delta: float):
    leg = {**_VALID_CE_LEG, "greeks": {**_VALID_CE_LEG["greeks"], "delta": delta}}
    assert _ce(_one_strike_payload(leg)).has_complete_greeks is False


@pytest.mark.parametrize("delta", [0.0001, -1.0001, -5.0])
def test_has_complete_greeks_rejects_pe_delta_outside_neg1_0(delta: float):
    leg = {**_VALID_PE_LEG, "greeks": {**_VALID_PE_LEG["greeks"], "delta": delta}}
    assert _pe(_one_strike_payload({}, leg)).has_complete_greeks is False


def test_has_complete_greeks_accepts_delta_at_the_exact_boundary():
    ce_leg = {**_VALID_CE_LEG, "greeks": {**_VALID_CE_LEG["greeks"], "delta": 1.0}}
    assert _ce(_one_strike_payload(ce_leg)).has_complete_greeks is True
    pe_leg = {**_VALID_PE_LEG, "greeks": {**_VALID_PE_LEG["greeks"], "delta": -1.0}}
    assert _pe(_one_strike_payload({}, pe_leg)).has_complete_greeks is True


def test_has_complete_greeks_rejects_negative_gamma():
    leg = {**_VALID_CE_LEG, "greeks": {**_VALID_CE_LEG["greeks"], "gamma": -0.001}}
    assert _ce(_one_strike_payload(leg)).has_complete_greeks is False


def test_has_complete_greeks_rejects_negative_vega():
    leg = {**_VALID_CE_LEG, "greeks": {**_VALID_CE_LEG["greeks"], "vega": -0.5}}
    assert _ce(_one_strike_payload(leg)).has_complete_greeks is False


def test_has_complete_greeks_rejects_positive_theta():
    leg = {**_VALID_CE_LEG, "greeks": {**_VALID_CE_LEG["greeks"], "theta": 0.5}}
    assert _ce(_one_strike_payload(leg)).has_complete_greeks is False
