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
**Verified against a real, live ``/v2/optionchain`` response** by
``scripts/verify_dhan_option_chain.py`` on 2026-08-16 (Phase 4 of the
gap-closing session; see ``docs/IMPLEMENTATION_STATUS_AND_RUNBOOK.md``
section 11.10) — the envelope, every field this module reads, and the
sign/range of the greeks all matched this docstring exactly. A permanent
regression fixture derived from that verified shape lives at
``tests/fixtures/dhan_option_chain_sample.json`` (loaded by
``tests/unit/test_option_chain_view.py::
test_parses_the_sanitized_real_shaped_fixture``), so a later drift in
Dhan's own response shape is caught without needing network access. Every
field this module reads is named explicitly below so that a re-verification
is a direct diff against this docstring, not a hunt through the code. One
real-response quirk that verification surfaced: a strike with no book at
all reports ``top_bid_price``/``top_ask_price`` (and every other numeric
field) as literal ``0``, not an absent key — already handled correctly
below (see "Never synthesizes"), just not previously confirmed live.

**Never synthesizes.** A missing ``top_bid_price``/``top_ask_price`` is
``None``, not zero and not the last price — spec section 3.6 forbids
synthesizing a bid/ask spread from LTP, and this is the one place that rule
could be violated by accident. In practice a real response represents "no
book" as the field literally being ``0`` rather than absent (see above);
either way this module never treats a zero/absent bid or ask as anything
but "no complete quote" — ``ChainQuote.has_complete_quote`` requires a
strictly positive bid.

**Fields present in a real response but not read here.** A real leg also
carries ``average_price``, ``previous_close_price``, ``previous_oi``,
``previous_volume``, ``security_id`` and ``top_bid_quantity``/
``top_ask_quantity`` — all confirmed present by the same 2026-08-16
verification above, all deliberately ignored (this module reads exactly the
fields listed above and nothing else); a future consumer that needs one of
these should add it explicitly rather than assume ``_quote_from_leg``
already captures it.
"""

from __future__ import annotations

import math
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

    option_type: OptionType
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
        """A genuinely usable Greek/IV set (spec section 4.2; Phase 4A
        correction, 16 August 2026 gap-closing session).

        The live verification behind Phase 4 confirmed a real Dhan
        response can report an option as structurally "present" — every
        key exists — with delta, gamma, theta and vega *all literally
        zero*, on a strike with no real book at all (see the deep-OTM
        sample in ``tests/fixtures/dhan_option_chain_sample.json``).
        Economically that means "no real market-implied Greeks were ever
        computed for this strike," not "the Greeks are genuinely all
        zero" — treating it as a valid, usable Greek set previously let
        :meth:`~common.greeks.service.GreeksService._from_chain` accept
        garbage over a real Black-Scholes-Merton fallback computation.
        Every check below fails closed to ``False`` (never raises); a
        caller that finds ``False`` here already knows to fall back to
        the model or, if that too is unusable, to
        :class:`~common.greeks.models.GreeksUnavailable` — which blocks
        entry/normal-adjustment risk and never blocks an exit (spec
        section 4.2's own fail-open-for-exits rule; no exit-priority check
        in this codebase consults Greeks at all, so this property cannot
        affect one regardless).

        Checks, in order:

        1. every one of delta/gamma/theta/vega/implied_volatility is
           present (not ``None``);
        2. every one of them is finite (rejects ``NaN``/``+-inf`` — Dhan's
           own JSON is permissive enough to carry a literal ``NaN`` token
           and ``httpx``'s default JSON decoding accepts it);
        3. ``implied_volatility > 0`` (a zero or negative IV can never
           describe a real option price);
        4. delta, gamma, theta and vega are not *all four* exactly zero
           (the specific "present but garbage" fingerprint verification
           found — a genuinely tiny but nonzero Greek, e.g. a deep-OTM
           option's real gamma of ``0.0001``, is never rejected by this
           check alone);
        5. delta stays within the conventional bound for this leg's own
           ``option_type`` — CE: ``0 <= delta <= 1``; PE:
           ``-1 <= delta <= 0``;
        6. gamma and vega are non-negative and theta is non-positive —
           the sign conventions this codebase's own fallback model
           (``common.greeks.model``, wrapping ``vollib``) and every real
           sample this session observed both agree on; a value outside
           these signs is not a real option's Greek, not merely an
           unusual one.
        """
        delta, gamma, theta, vega, iv = (
            self.delta, self.gamma, self.theta, self.vega, self.implied_volatility,
        )
        if delta is None or gamma is None or theta is None or vega is None or iv is None:
            return False
        if not (
            math.isfinite(delta)
            and math.isfinite(gamma)
            and math.isfinite(theta)
            and math.isfinite(vega)
            and math.isfinite(iv)
        ):
            return False
        if iv <= 0:
            return False
        if delta == 0.0 and gamma == 0.0 and theta == 0.0 and vega == 0.0:
            return False
        if self.option_type is OptionType.CE:
            if not (0.0 <= delta <= 1.0):
                return False
        elif not (-1.0 <= delta <= 0.0):
            return False
        return not (gamma < 0.0 or vega < 0.0 or theta > 0.0)


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
    #: Whatever :class:`~common.market_data.option_chain.ChainSnapshot.
    #: snapshot_at` carried in — an exchange/broker-supplied timestamp
    #: *only if the response actually contained one*. Phase 4A correction
    #: (16 August 2026): a real, live Dhan ``/v2/optionchain`` response
    #: never has — verified, not assumed — so this equals ``received_at``
    #: in every real case today. Never call this "the exchange timestamp"
    #: as though it were independently confirmed; treat it as provenance
    #: metadata only. Freshness math (``GreekSnapshot.is_fresh``/
    #: ``age_seconds``) deliberately uses ``received_at`` alone, never
    #: this field, for exactly this reason.
    snapshot_at: datetime
    #: When this process actually received the HTTP response — the one
    #: honest, unconditional timestamp this module has. A recent
    #: ``received_at`` proves only that the HTTP round trip was recent; it
    #: does **not** by itself prove the underlying quotes are genuinely
    #: live/moving market data (a broker could return a fast but stale
    #: internal snapshot). That stronger claim — *market-hours*
    #: freshness — can only be checked while the market is actually open,
    #: and only ever reported as observed, never inferred from this field
    #: alone (see ``scripts/verify_dhan_option_chain.py``'s own two-part
    #: report).
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


def _quote_from_leg(leg: dict[str, Any], *, option_type: OptionType) -> ChainQuote:
    greeks = leg.get("greeks") or {}
    if not isinstance(greeks, dict):
        greeks = {}
    return ChainQuote(
        option_type=option_type,
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
                call=_quote_from_leg(
                    ce_leg if isinstance(ce_leg, dict) else {}, option_type=OptionType.CE
                ),
                put=_quote_from_leg(
                    pe_leg if isinstance(pe_leg, dict) else {}, option_type=OptionType.PE
                ),
            )
        )
    strikes.sort(key=lambda row: row.strike)

    return ChainView(
        underlying_last_price=_optional_float(data.get("last_price")),
        strikes=tuple(strikes),
        snapshot_at=snapshot_at,
        received_at=received_at,
    )
