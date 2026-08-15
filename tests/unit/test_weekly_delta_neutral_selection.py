"""``select_iron_condor``'s deterministic four-leg search — focused on the
lot-size consistency gate (spec section 3.1: lot size is resolved from each
selected Dhan contract's own metadata, never a single external value
trusted on its own, and never hardcoded/configured). The exchange's
*current* real NIFTY lot size is deliberately never used here — see
``tests/integration/test_weekly_delta_neutral_lot_size.py`` for the one,
dated place that value is allowed to appear at all, as a current-reference
assertion, never a production constant.

Uses the real ``DhanOptionChainResolver``/``ScripMaster``/``GreeksService``/
``OptionChainService`` stack against a small fixture scrip master — no
hand-rolled fakes — so a per-strike lot-size mismatch is expressed exactly
the way a real (if anomalous) instrument master would carry it.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

from common.engine.selection import DhanOptionChainResolver
from common.greeks import GreeksService, ModelAssumptions
from common.market_data.option_chain import OptionChainService
from common.market_data.scrip_master import ScripMaster
from strategies.positional_options.weekly_delta_neutral.config import SelectionConfig
from strategies.positional_options.weekly_delta_neutral.selection import select_iron_condor

EXPIRY_DATE = "2026-08-26"

HEDGE_PUT_STRIKE = 23150.0
SHORT_PUT_STRIKE = 23500.0
SHORT_CALL_STRIKE = 24500.0
HEDGE_CALL_STRIKE = 24850.0

_ROLE_STRIKES = {
    (HEDGE_PUT_STRIKE, "PE"): "90001",
    (SHORT_PUT_STRIKE, "PE"): "90002",
    (SHORT_CALL_STRIKE, "CE"): "90003",
    (HEDGE_CALL_STRIKE, "CE"): "90004",
}


def _scrip_master_csv(lot_sizes: dict[tuple[float, str], int]) -> str:
    """One row per ``_ROLE_STRIKES`` entry, each with its *own* lot size —
    letting a test express a real (if anomalous) per-contract mismatch,
    which a single shared ``lot_size=`` fixture parameter could not."""
    rows = [
        [
            "SEM_SMST_SECURITY_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL",
            "SEM_CUSTOM_SYMBOL", "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE",
            "SEM_OPTION_TYPE", "SEM_LOT_UNITS", "SEM_EXM_EXCH_ID", "SEM_SEGMENT",
        ]
    ]
    for key, security_id in _ROLE_STRIKES.items():
        strike, option_type = key
        rows.append(
            [
                security_id, "OPTIDX", f"NIFTY-26AUG2026-{strike:.0f}-{option_type}",
                f"NIFTY {strike:.0f} {option_type}", f"{EXPIRY_DATE} 00:00:00",
                f"{strike:.0f}", option_type, str(lot_sizes[key]), "NSE", "D",
            ]
        )
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue()


def _leg(delta: float, bid: float, ask: float) -> dict[str, Any]:
    return {
        "greeks": {"delta": delta, "gamma": 0.001, "theta": -5.0, "vega": 10.0},
        "implied_volatility": 14.0,
        "last_price": (bid + ask) / 2.0,
        "oi": 500_000,
        "top_bid_price": bid,
        "top_ask_price": ask,
        "volume": 25_000,
    }


def _chain_payload() -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "last_price": 24000.0,
            "oc": {
                f"{HEDGE_PUT_STRIKE:.6f}": {"pe": _leg(-0.06, 18.0, 20.0)},
                f"{SHORT_PUT_STRIKE:.6f}": {"pe": _leg(-0.20, 80.0, 82.0)},
                f"{SHORT_CALL_STRIKE:.6f}": {"ce": _leg(0.20, 78.0, 80.0)},
                f"{HEDGE_CALL_STRIKE:.6f}": {"ce": _leg(0.06, 17.0, 19.0)},
            },
        },
    }


def _select(lot_sizes: dict[tuple[float, str], int]):  # type: ignore[no-untyped-def]
    now = datetime(2026, 8, 19, 9, 26, tzinfo=UTC)
    scrip_master = ScripMaster("NIFTY", exchange="NSE").load_from_text(
        _scrip_master_csv(lot_sizes)
    )
    resolver = DhanOptionChainResolver(scrip_master, expiry=EXPIRY_DATE)
    # A fixed clock matching `now` below for both services: a chain
    # snapshot's own received_at must be comparable to the `now` a
    # freshness check runs against, or every candidate looks stale
    # against the real wall clock instead of this test's own fixed time
    # (the same clock-alignment fix worker.build_engine needed).
    chain_service = OptionChainService(
        lambda _sid, _seg, _exp: _chain_payload(), wall_clock=lambda: now
    )
    greeks = GreeksService(
        chain_service, assumptions=ModelAssumptions(risk_free_rate=0.065),
        max_age_seconds=30.0, clock=lambda: now,
    )
    chain = greeks.chain_snapshot(
        underlying_security_id=13, underlying_segment="IDX_I", expiry=EXPIRY_DATE
    )
    return select_iron_condor(
        chain=chain, resolver=resolver, greeks=greeks, spot=24000.0,
        expiry_at=datetime(2026, 8, 26, 15, 30, tzinfo=UTC), now=now, lots=1,
        config=SelectionConfig(),
    )


def test_a_consistent_non_zero_lot_size_across_all_four_legs_is_used() -> None:
    # An arbitrary value, deliberately not NIFTY's current real lot size —
    # this test proves the mechanism (consistency across legs), not any
    # particular exchange fact.
    lot_sizes = dict.fromkeys(_ROLE_STRIKES, 50)
    candidate = _select(lot_sizes)
    assert candidate is not None
    assert candidate.lot_size == 50


def test_a_mismatched_lot_size_across_legs_fails_closed() -> None:
    lot_sizes = dict.fromkeys(_ROLE_STRIKES, 50)
    # One leg's contract carries a different lot size — a real anomaly
    # (or a stale/partially-updated instrument master) must never be
    # averaged, guessed at, or silently outvoted; it must block entry.
    lot_sizes[(HEDGE_PUT_STRIKE, "PE")] = 75
    assert _select(lot_sizes) is None


def test_a_zero_lot_size_fails_closed() -> None:
    lot_sizes = dict.fromkeys(_ROLE_STRIKES, 0)
    assert _select(lot_sizes) is None
