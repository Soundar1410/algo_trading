"""Typed parse of a Dhan Option Chain payload — the boundary between the raw
JSON :class:`~common.market_data.option_chain.ChainSnapshot` carries and
everything downstream (:mod:`common.greeks`, strategy selection).

**Payload shape.** Dhan's ``POST /v2/optionchain`` response (per its public
API reference, section "Option Chain"):

.. code-block:: json

    {
      "status": "success",
      "data": {
        "last_price": 24000.5,
        "oc": {
          "24000.000000": {
            "ce": {
              "greeks": {"delta": 0.5, "gamma": 0.001, "theta": -5.2, "vega": 10.1},
              "implied_volatility": 15.2,
              "last_price": 120.5,
              "oi": 500000,
              "top_ask_price": 121.0,
              "top_bid_price": 120.0,
              "volume": 25000
            },
            "pe": { "...": "same shape" }
          }
        }
      }
    }

The strike key is Dhan's own stringified-float format (``"24000.000000"``).
This module has not been proven against a live response in this environment
(no network/credentials here — see ``scripts/verify_vix_security_id.py`` for
the established pattern of a bounded, read-only, real-source check); it must
be re-verified against a live ``/v2/optionchain`` response, the same way the
runbook records for the VIX security id, before this parser is trusted for a
real paper session. Every field this module reads is named explicitly below
so that verification is a direct diff against this docstring, not a hunt
through the code.

**Never synthesizes.** A missing ``top_bid_price``/``top_ask_price`` is
``None``, not zero and not the last price — spec section 3.6 forbids
synthesizing a bid/ask spread from LTP, and this is the one place that rule
could be violated by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from common.models import OptionType


class ChainPayloadError(RuntimeError):
    """The chain payload is missing a field this module cannot proceed
    without, or is shaped in a way this parser does not recognise. Raised
    rather than guessed at — a malformed chain must block a decision, not
    silently produce wrong Greeks/quotes."""


@dataclass(frozen=True)
class ChainQuote:
    """One option (CE or PE) at one strike, as the chain reported it."""

    bid: float | None
    ask: float | None
    last_price: float | None
    volume: int
    open_interest: int
    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None

    @property
    def has_complete_quote(self) -> bool:
        """A real, two-sided, non-crossed, positive book — the minimum spec
        section 3.6 requires before this quote may be used for entry or
        adjustment. Deliberately does not consider OI/volume (checked
        separately against their own thresholds by the caller)."""
        if self.bid is None or self.ask is None:
            return False
        return 0 < self.bid <= self.ask

    @property
    def has_complete_greeks(self) -> bool:
        return None not in (self.delta, self.gamma, self.theta, self.vega)


@dataclass(frozen=True)
class ChainStrike:
    strike: float
    call: ChainQuote
    put: ChainQuote

    def side(self, option_type: OptionType) -> ChainQuote:
        return self.call if option_type is OptionType.CE else self.put


@dataclass(frozen=True)
class ChainView:
    """The whole parsed chain for one underlying/expiry snapshot."""

    underlying_last_price: float | None
    strikes: tuple[ChainStrike, ...]
    snapshot_at: datetime
    received_at: datetime

    def strike(self, strike: float) -> ChainStrike | None:
        for row in self.strikes:
            if row.strike == strike:
                return row
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quote_from_leg(leg: dict[str, Any]) -> ChainQuote:
    greeks = leg.get("greeks") or {}
    if not isinstance(greeks, dict):
        greeks = {}
    return ChainQuote(
        bid=_optional_float(leg.get("top_bid_price")),
        ask=_optional_float(leg.get("top_ask_price")),
        last_price=_optional_float(leg.get("last_price")),
        volume=int(leg.get("volume") or 0),
        open_interest=int(leg.get("oi") or 0),
        implied_volatility=_optional_float(leg.get("implied_volatility")),
        delta=_optional_float(greeks.get("delta")),
        gamma=_optional_float(greeks.get("gamma")),
        theta=_optional_float(greeks.get("theta")),
        vega=_optional_float(greeks.get("vega")),
    )


def parse_chain_payload(
    payload: dict[str, Any], *, snapshot_at: datetime, received_at: datetime
) -> ChainView:
    """Parse one raw Dhan option-chain payload into a :class:`ChainView`.

    Raises :class:`ChainPayloadError` if the top-level ``data``/``oc``
    structure is absent or malformed — a genuinely empty chain (every strike
    row present but empty) is not an error and parses to an empty
    ``strikes`` tuple; a structurally wrong payload is.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ChainPayloadError("option chain payload has no 'data' object")
    oc = data.get("oc")
    if not isinstance(oc, dict):
        raise ChainPayloadError("option chain payload has no 'data.oc' object")

    strikes: list[ChainStrike] = []
    for raw_strike, legs in oc.items():
        if not isinstance(legs, dict):
            raise ChainPayloadError(f"option chain strike {raw_strike!r} is not an object")
        try:
            strike_value = float(raw_strike)
        except (TypeError, ValueError) as exc:
            raise ChainPayloadError(
                f"option chain strike key {raw_strike!r} is not numeric"
            ) from exc
        ce_leg = legs.get("ce") or {}
        pe_leg = legs.get("pe") or {}
        strikes.append(
            ChainStrike(
                strike=strike_value,
                call=_quote_from_leg(ce_leg if isinstance(ce_leg, dict) else {}),
                put=_quote_from_leg(pe_leg if isinstance(pe_leg, dict) else {}),
            )
        )
    strikes.sort(key=lambda row: row.strike)

    return ChainView(
        underlying_last_price=_optional_float(data.get("last_price")),
        strikes=tuple(strikes),
        snapshot_at=snapshot_at,
        received_at=received_at,
    )
