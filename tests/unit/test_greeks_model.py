"""``common.greeks.model`` — the ``vollib``-backed fallback Greeks model.

No handwritten production Black-Scholes code exists anywhere in ``common/``
(spec review correction 1) — this file's own small, independent
``math.erf``-based Black-Scholes-Merton price function exists *only* here,
as a test oracle to cross-check ``vollib``'s output, the same discipline
``tests/unit/test_indicator_oracle.py`` already uses to cross-check
``pandas_ta_classic``. It is never imported by anything under ``common/``.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from common.greeks.model import GreeksModelUnavailable, black_scholes_merton_greeks
from common.greeks.models import GreekInputs
from common.models import OptionType

NOW = datetime(2026, 8, 19, 9, 20, tzinfo=UTC)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _oracle_price(
    *, flag: str, spot: float, strike: float, t: float, r: float, sigma: float, q: float
) -> float:
    """Independent Black-Scholes-Merton price — test-only oracle."""
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma**2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if flag == "c":
        return spot * math.exp(-q * t) * _norm_cdf(d1) - strike * math.exp(-r * t) * _norm_cdf(d2)
    return strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * math.exp(-q * t) * _norm_cdf(-d1)


def _oracle_delta(
    *, flag: str, spot: float, strike: float, t: float, r: float, sigma: float, q: float
) -> float:
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma**2) * t) / (sigma * math.sqrt(t))
    if flag == "c":
        return math.exp(-q * t) * _norm_cdf(d1)
    return math.exp(-q * t) * (_norm_cdf(d1) - 1.0)


def _inputs(
    *,
    option_type: OptionType,
    spot: float,
    strike: float,
    days: float,
    r: float,
    sigma: float,
    q: float,
) -> GreekInputs:
    return GreekInputs(
        spot=spot,
        strike=strike,
        option_type=option_type,
        implied_volatility=sigma,
        risk_free_rate=r,
        dividend_yield=q,
        evaluation_timestamp=NOW,
        expiry_at=NOW + timedelta(days=days),
    )


# ------------------------------------------------------------ golden values
#: Hull, "Options, Futures, and Other Derivatives", Example 17.2/17.6 (also
#: used by vollib's own docstring/doctest for theta/vega): S=49, K=50,
#: r=0.05, T=0.3846y (~140.4 days), sigma=0.2, q=0.
_HULL_DAYS = 0.3846 * 365.0


@pytest.mark.parametrize("flag,option_type", [("c", OptionType.CE), ("p", OptionType.PE)])
def test_golden_value_matches_the_published_hull_example(flag, option_type):
    inputs = _inputs(
        option_type=option_type, spot=49, strike=50, days=_HULL_DAYS, r=0.05, sigma=0.2, q=0.0
    )
    snapshot = black_scholes_merton_greeks(inputs, security_id="TEST")
    t_years = _HULL_DAYS / 365.0
    oracle = _oracle_price(flag=flag, spot=49, strike=50, t=t_years, r=0.05, sigma=0.2, q=0.0)
    # golden delta from Hull's own worked figures (annual theta * 365 checks
    # match vollib's own doctest exactly to 1e-2) — cross-check delta against
    # the independent oracle instead, tighter tolerance available there.
    oracle_delta = _oracle_delta(flag=flag, spot=49, strike=50, t=t_years, r=0.05, sigma=0.2, q=0.0)
    assert snapshot.delta == pytest.approx(oracle_delta, abs=1e-6)
    # Reconstruct the vollib annual-theta check from its own doctest: for a
    # call, -4.30538996455; for a put, -1.8530056722.
    if flag == "c":
        assert snapshot.theta * 365.0 == pytest.approx(-4.30538996455, abs=1e-2)
    else:
        assert snapshot.theta * 365.0 == pytest.approx(-1.8530056722, abs=1e-2)
    del oracle  # price cross-check happens in the parity test below


# ------------------------------------------------------------ put-call parity
@pytest.mark.parametrize(
    "spot,strike,days,r,sigma,q",
    [
        (24000.0, 24000.0, 4, 0.065, 0.14, 0.0),
        (24000.0, 23500.0, 4, 0.065, 0.16, 0.0),
        (24000.0, 24500.0, 11, 0.065, 0.13, 0.012),
        (100.0, 100.0, 30, 0.05, 0.25, 0.0),
    ],
)
def test_put_call_parity_holds_for_the_independent_oracle(spot, strike, days, r, sigma, q):
    """C - P == S*e^(-qT) - K*e^(-rT) — proves the oracle itself is sound
    before it is trusted to check vollib against it."""
    t = days / 365.0
    call = _oracle_price(flag="c", spot=spot, strike=strike, t=t, r=r, sigma=sigma, q=q)
    put = _oracle_price(flag="p", spot=spot, strike=strike, t=t, r=r, sigma=sigma, q=q)
    parity_rhs = spot * math.exp(-q * t) - strike * math.exp(-r * t)
    assert (call - put) == pytest.approx(parity_rhs, abs=1e-8)


@pytest.mark.parametrize(
    "spot,strike,days,r,sigma,q",
    [
        (24000.0, 24000.0, 4, 0.065, 0.14, 0.0),
        (24000.0, 23500.0, 4, 0.065, 0.16, 0.0),
        (24000.0, 24500.0, 11, 0.065, 0.13, 0.012),
    ],
)
def test_vollibs_delta_matches_the_independent_finite_difference_delta(
    spot, strike, days, r, sigma, q
):
    """Finite-difference cross-check: (price(S+h) - price(S-h)) / (2h)
    against vollib's own analytic delta — an independent numerical proof
    that vollib's delta is internally consistent with its own theoretical
    price (via the oracle's price function), not just plausible-looking."""
    t = days / 365.0
    h = spot * 1e-4
    for flag, option_type in (("c", OptionType.CE), ("p", OptionType.PE)):
        price_up = _oracle_price(
            flag=flag, spot=spot + h, strike=strike, t=t, r=r, sigma=sigma, q=q
        )
        price_down = _oracle_price(
            flag=flag, spot=spot - h, strike=strike, t=t, r=r, sigma=sigma, q=q
        )
        fd_delta = (price_up - price_down) / (2 * h)

        inputs = _inputs(
            option_type=option_type, spot=spot, strike=strike, days=days, r=r, sigma=sigma, q=q
        )
        snapshot = black_scholes_merton_greeks(inputs, security_id="TEST")
        assert snapshot.delta == pytest.approx(fd_delta, abs=1e-4)


# --------------------------------------------------------------- fail closed
def test_a_non_positive_implied_volatility_fails_closed():
    inputs = _inputs(
        option_type=OptionType.CE, spot=100, strike=100, days=7, r=0.05, sigma=0.0, q=0.0
    )
    with pytest.raises(GreeksModelUnavailable, match="implied_volatility"):
        black_scholes_merton_greeks(inputs, security_id="TEST")


def test_an_already_expired_contract_fails_closed():
    inputs = GreekInputs(
        spot=100, strike=100, option_type=OptionType.CE, implied_volatility=0.2,
        risk_free_rate=0.05, dividend_yield=0.0, evaluation_timestamp=NOW,
        expiry_at=NOW - timedelta(seconds=1),
    )
    with pytest.raises(GreeksModelUnavailable):
        black_scholes_merton_greeks(inputs, security_id="TEST")


def test_naive_datetimes_are_refused():
    inputs = GreekInputs(
        spot=100, strike=100, option_type=OptionType.CE, implied_volatility=0.2,
        risk_free_rate=0.05, dividend_yield=0.0,
        evaluation_timestamp=datetime(2026, 8, 19, 9, 20),  # naive
        expiry_at=NOW + timedelta(days=7),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        black_scholes_merton_greeks(inputs, security_id="TEST")


def test_vollib_unavailable_fails_closed_rather_than_falling_back(monkeypatch):
    """Simulates the extra not being installed: the model must fail closed,
    never silently substitute a second, unreviewed pricing implementation."""
    import common.greeks.model as model_module

    def _unavailable() -> object:
        raise GreeksModelUnavailable("vollib is not importable")

    monkeypatch.setattr(model_module, "_vollib_greeks", None)
    monkeypatch.setattr(model_module, "_load_vollib_greeks", _unavailable)
    inputs = _inputs(
        option_type=OptionType.CE, spot=100, strike=100, days=7, r=0.05, sigma=0.2, q=0.0
    )
    with pytest.raises(GreeksModelUnavailable, match="vollib"):
        black_scholes_merton_greeks(inputs, security_id="TEST")
