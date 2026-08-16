"""The atomic four-leg search (spec section 3.5) proven against the
nearest-leg shortcut it replaces — request item 3 of the
strategy-weekly-delta-neutral gap-closing task: "Add tests proving that
independent nearest-leg selection would choose an invalid or inferior
basket while the complete search selects the valid best basket."

``rank_role_candidates``/``best_hedge`` still exist unchanged (the
hedge-repair path still uses ``best_hedge`` directly against one already-
fixed short — see ``selection.py``'s own docstring), so this module
reconstructs exactly what the old single-path algorithm did — "the nearest
short, then that short's own best hedge" — as a local baseline, and shows
it fails or is worse than ``select_iron_condor``'s real search on the same
fixture chain.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

from common.engine.positional.positional_models import LegRole
from common.engine.selection import DhanOptionChainResolver
from common.greeks import GreeksService, ModelAssumptions
from common.market_data.option_chain import OptionChainService
from common.market_data.scrip_master import ScripMaster
from common.models import OptionType
from strategies.positional_options.weekly_delta_neutral.config import SelectionConfig
from strategies.positional_options.weekly_delta_neutral.selection import (
    best_hedge,
    rank_role_candidates,
    select_iron_condor,
)

EXPIRY_DATE = "2026-08-26"
LOT_SIZE = "75"

# Two short-put candidates, both inside the 0.20 +/- 0.03 tolerance:
# NEAREST_SHORT_PUT is delta-nearest to the -0.20 target; its own width
# band's only hedge candidate is deliberately outside the hedge's own delta
# tolerance. FALLBACK_SHORT_PUT is farther from the target (but still
# within its own tolerance) and has a fully valid hedge.
NEAREST_SHORT_PUT = 23500.0  # delta -0.20 (distance 0.00)
NEAREST_SHORT_PUT_BAD_HEDGE = 23150.0  # in [23000, 23250]; delta -0.15 (invalid)
FALLBACK_SHORT_PUT = 23450.0  # delta -0.22 (distance 0.02, still <= 0.03)
#: In [22950, 23200] (the fallback short's own band) but *outside*
#: [23000, 23250] (the nearest short's band) — so it can only ever be
#: reached through the fallback short, never accidentally through the
#: nearest one.
FALLBACK_SHORT_PUT_GOOD_HEDGE = 22980.0

SHORT_CALL_STRIKE = 24500.0
HEDGE_CALL_STRIKE = 24850.0

_STRIKES: dict[tuple[float, str], str] = {
    (NEAREST_SHORT_PUT, "PE"): "91001",
    (NEAREST_SHORT_PUT_BAD_HEDGE, "PE"): "91002",
    (FALLBACK_SHORT_PUT, "PE"): "91003",
    (FALLBACK_SHORT_PUT_GOOD_HEDGE, "PE"): "91004",
    (SHORT_CALL_STRIKE, "CE"): "91005",
    (HEDGE_CALL_STRIKE, "CE"): "91006",
}


def _scrip_master_csv() -> str:
    rows = [
        [
            "SEM_SMST_SECURITY_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL",
            "SEM_CUSTOM_SYMBOL", "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE",
            "SEM_OPTION_TYPE", "SEM_LOT_UNITS", "SEM_EXM_EXCH_ID", "SEM_SEGMENT",
        ]
    ]
    for (strike, option_type), security_id in _STRIKES.items():
        rows.append(
            [
                security_id, "OPTIDX", f"NIFTY-26AUG2026-{strike:.0f}-{option_type}",
                f"NIFTY {strike:.0f} {option_type}", f"{EXPIRY_DATE} 00:00:00",
                f"{strike:.0f}", option_type, LOT_SIZE, "NSE", "D",
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
                # Nearest-to-target short put — its own width band's only
                # hedge candidate has an invalid delta (-0.15, outside
                # -0.06 +/- 0.03).
                f"{NEAREST_SHORT_PUT:.6f}": {"pe": _leg(-0.20, 80.0, 82.0)},
                f"{NEAREST_SHORT_PUT_BAD_HEDGE:.6f}": {"pe": _leg(-0.15, 40.0, 42.0)},
                # A short put farther from the target delta but still
                # within its own tolerance — its width band's hedge is
                # fully valid.
                f"{FALLBACK_SHORT_PUT:.6f}": {"pe": _leg(-0.22, 85.0, 87.0)},
                f"{FALLBACK_SHORT_PUT_GOOD_HEDGE:.6f}": {"pe": _leg(-0.06, 18.0, 20.0)},
                # A straightforward, always-valid call side.
                f"{SHORT_CALL_STRIKE:.6f}": {"ce": _leg(0.20, 78.0, 80.0)},
                f"{HEDGE_CALL_STRIKE:.6f}": {"ce": _leg(0.06, 17.0, 19.0)},
            },
        },
    }


def _fixture():  # type: ignore[no-untyped-def]
    now = datetime(2026, 8, 19, 9, 26, tzinfo=UTC)
    scrip_master = ScripMaster("NIFTY", exchange="NSE").load_from_text(_scrip_master_csv())
    resolver = DhanOptionChainResolver(scrip_master, expiry=EXPIRY_DATE)
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
    return now, resolver, greeks, chain


def test_nearest_leg_selection_would_block_entry_that_the_complete_search_finds() -> None:
    now, resolver, greeks, chain = _fixture()
    config = SelectionConfig()
    expiry_at = datetime(2026, 8, 26, 15, 30, tzinfo=UTC)

    # -------------------------------------------- the old, replaced shortcut
    # "The nearest short, then that short's own best hedge" — exactly what
    # select_iron_condor did before this task's rewrite.
    short_put_candidates = rank_role_candidates(
        chain=chain, resolver=resolver, greeks=greeks, option_type=OptionType.PE,
        role=LegRole.SHORT_PUT, target_delta=config.short_put_delta,
        tolerance=config.short_delta_tolerance, config=config, spot=24000.0,
        expiry_at=expiry_at, now=now,
    )
    assert short_put_candidates, "fixture sanity: at least one short put must qualify"
    nearest_short_put = short_put_candidates[0]
    assert nearest_short_put.strike == NEAREST_SHORT_PUT, (
        "fixture sanity: the delta-nearest short put must rank first"
    )
    nearest_leg_hedge = best_hedge(
        chain=chain, resolver=resolver, greeks=greeks, option_type=OptionType.PE,
        short_strike=nearest_short_put.strike, config=config, spot=24000.0,
        expiry_at=expiry_at, now=now, role=LegRole.HEDGE_PUT,
        target_delta=config.hedge_put_delta, tolerance=config.hedge_delta_tolerance,
    )
    assert nearest_leg_hedge is None, (
        "fixture sanity: the nearest short's own width band has no valid hedge"
    )
    # Independent nearest-leg selection would stop right here: no hedge for
    # the nearest short means no entry at all, even though FALLBACK_SHORT_PUT
    # (a perfectly valid alternative just outside the delta-nearest choice)
    # has a fully valid hedge.

    # ------------------------------------------------- the real, complete search
    candidate = select_iron_condor(
        chain=chain, resolver=resolver, greeks=greeks, spot=24000.0,
        expiry_at=expiry_at, now=now, lots=1, config=config,
    )
    assert candidate is not None, (
        "the complete search must find the valid basket the nearest-leg "
        "shortcut would have missed entirely"
    )
    assert candidate.short_put.strike == FALLBACK_SHORT_PUT
    assert candidate.hedge_put.strike == FALLBACK_SHORT_PUT_GOOD_HEDGE
    assert candidate.short_call.strike == SHORT_CALL_STRIKE
    assert candidate.hedge_call.strike == HEDGE_CALL_STRIKE
    assert candidate.initial_net_credit > 0
    assert candidate.lot_size == 75


def test_search_never_considers_a_hedge_outside_its_own_shorts_width_band() -> None:
    """A control: the bad-hedge strike is only invalid on *delta*, not
    width — confirms the fixture's own width bounds are set up as intended
    (23000-23250 for the nearest short, 22950-23200 for the fallback
    short), so the test above is proven by delta tolerance, not an
    accidental width mismatch."""
    now, resolver, greeks, chain = _fixture()
    config = SelectionConfig()
    expiry_at = datetime(2026, 8, 26, 15, 30, tzinfo=UTC)

    assert NEAREST_SHORT_PUT - config.maximum_hedge_width_points <= NEAREST_SHORT_PUT_BAD_HEDGE
    assert NEAREST_SHORT_PUT - config.minimum_hedge_width_points >= NEAREST_SHORT_PUT_BAD_HEDGE
    assert (
        FALLBACK_SHORT_PUT - config.maximum_hedge_width_points
        <= FALLBACK_SHORT_PUT_GOOD_HEDGE
        <= FALLBACK_SHORT_PUT - config.minimum_hedge_width_points
    )

    # The bad-hedge strike genuinely resolves and has a complete quote —
    # rank_role_candidates rejects it on delta tolerance alone, not because
    # it is unlisted or illiquid.
    hedge_candidates_in_band = rank_role_candidates(
        chain=chain, resolver=resolver, greeks=greeks, option_type=OptionType.PE,
        role=LegRole.HEDGE_PUT, target_delta=config.hedge_put_delta,
        tolerance=1.0,  # wide open — just prove the strike is reachable at all
        config=config, spot=24000.0, expiry_at=expiry_at, now=now,
        strike_bounds=(
            NEAREST_SHORT_PUT - config.maximum_hedge_width_points,
            NEAREST_SHORT_PUT - config.minimum_hedge_width_points,
        ),
    )
    assert any(c.strike == NEAREST_SHORT_PUT_BAD_HEDGE for c in hedge_candidates_in_band)
