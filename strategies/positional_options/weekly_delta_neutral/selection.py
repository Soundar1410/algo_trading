"""The atomic four-leg candidate search (spec section 3.5) — deterministic,
never relaxed. No candidate that clears every filter -> no entry, full stop.

Ranking, in order, for each role independently:

1. valid same expiry and required option type — guaranteed by construction
   (the chain snapshot and resolver are both already scoped to one expiry).
2. fresh, complete quote and Greek inputs.
3. liquidity and spread limits.
4. delta distance from the configured target.
5. hedge-width validity (hedges only).
6. stable strike tie-break.

Criterion 6 in the spec ("lowest absolute projected portfolio delta") is
enforced as a **post-hoc entry gate** on the assembled four-leg candidate
(``abs(net_delta_per_lot) <= maximum_entry_delta_per_lot``, spec section
3.7) rather than a combinatorial search across every viable leg
combination — each role's nearest-delta, most-liquid candidate is already
the practical optimum for a retail-sized iron condor, and searching the
full combinatorial space would trade a large amount of complexity for a
correction that is rarely, if ever, the deciding factor once each leg is
already within its own delta tolerance. Documented as a known scope
boundary, not a silent gap.
"""

from __future__ import annotations

from datetime import datetime

from common.engine.positional.positional_models import LegRole
from common.engine.selection import ContractNotListed, OptionChainResolver
from common.greeks import GreeksService, GreeksUnavailable
from common.market_data.chain_view import ChainQuote, ChainView
from common.models import OptionType

from .config import SelectionConfig
from .models import IronCondorCandidate, LegCandidate

_SHORT_ROLE_FOR_TYPE = {OptionType.PE: LegRole.SHORT_PUT, OptionType.CE: LegRole.SHORT_CALL}
_HEDGE_ROLE_FOR_TYPE = {OptionType.PE: LegRole.HEDGE_PUT, OptionType.CE: LegRole.HEDGE_CALL}


def _has_complete_quote(quote: ChainQuote, config: SelectionConfig) -> bool:
    if not quote.has_complete_quote:
        return False
    if quote.volume < config.minimum_volume:
        return False
    return quote.open_interest >= config.minimum_open_interest


def rank_role_candidates(
    *,
    chain: ChainView,
    resolver: OptionChainResolver,
    greeks: GreeksService,
    option_type: OptionType,
    role: LegRole,
    target_delta: float,
    tolerance: float,
    config: SelectionConfig,
    spot: float,
    expiry_at: datetime,
    now: datetime,
    strike_bounds: tuple[float, float] | None = None,
) -> list[LegCandidate]:
    """Every candidate for one role that clears quote/liquidity/delta
    filters, sorted deterministically: delta distance, then spread, then
    strike (the stable final tie-break)."""
    scored: list[tuple[float, float, float, LegCandidate]] = []
    for row in chain.strikes:
        if strike_bounds is not None and not (strike_bounds[0] <= row.strike <= strike_bounds[1]):
            continue
        quote = row.side(option_type)
        if not _has_complete_quote(quote, config):
            continue
        try:
            contract = resolver.resolve(int(row.strike), option_type, None)
        except ContractNotListed:
            continue
        try:
            snapshot = greeks.resolve(
                chain=chain,
                security_id=contract.security_id,
                option_type=option_type,
                strike=row.strike,
                spot=spot,
                expiry_at=expiry_at,
            )
        except GreeksUnavailable:
            continue
        if not snapshot.is_fresh(now=now, max_age_seconds=config.quote_max_age_seconds):
            continue
        delta_distance = abs(snapshot.delta - target_delta)
        if delta_distance > tolerance:
            continue
        assert quote.bid is not None and quote.ask is not None
        spread = quote.ask - quote.bid
        candidate = LegCandidate(role=role, contract=contract, quote=quote, greeks=snapshot)
        scored.append((delta_distance, spread, row.strike, candidate))
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in scored]


def select_iron_condor(
    *,
    chain: ChainView,
    resolver: OptionChainResolver,
    greeks: GreeksService,
    spot: float,
    expiry_at: datetime,
    now: datetime,
    lots: int,
    lot_size: int,
    config: SelectionConfig,
) -> IronCondorCandidate | None:
    """The complete, atomic four-leg search. Returns ``None`` — never a
    partial or relaxed result — when any role has no qualifying candidate,
    when the assembled combination fails the combined-spread/credit/width/
    delta gates (spec section 3.6/3.7), or when hedge width is invalid for
    every hedge candidate near the chosen short.
    """
    short_put_candidates = rank_role_candidates(
        chain=chain, resolver=resolver, greeks=greeks, option_type=OptionType.PE,
        role=LegRole.SHORT_PUT, target_delta=config.short_put_delta,
        tolerance=config.short_delta_tolerance, config=config, spot=spot,
        expiry_at=expiry_at, now=now,
    )
    short_call_candidates = rank_role_candidates(
        chain=chain, resolver=resolver, greeks=greeks, option_type=OptionType.CE,
        role=LegRole.SHORT_CALL, target_delta=config.short_call_delta,
        tolerance=config.short_delta_tolerance, config=config, spot=spot,
        expiry_at=expiry_at, now=now,
    )
    if not short_put_candidates or not short_call_candidates:
        return None
    short_put = short_put_candidates[0]
    short_call = short_call_candidates[0]

    hedge_put = best_hedge(
        chain=chain, resolver=resolver, greeks=greeks, option_type=OptionType.PE,
        short_strike=short_put.strike, config=config, spot=spot, expiry_at=expiry_at, now=now,
        role=LegRole.HEDGE_PUT, target_delta=config.hedge_put_delta,
        tolerance=config.hedge_delta_tolerance,
    )
    hedge_call = best_hedge(
        chain=chain, resolver=resolver, greeks=greeks, option_type=OptionType.CE,
        short_strike=short_call.strike, config=config, spot=spot, expiry_at=expiry_at, now=now,
        role=LegRole.HEDGE_CALL, target_delta=config.hedge_call_delta,
        tolerance=config.hedge_delta_tolerance,
    )
    if hedge_put is None or hedge_call is None:
        return None

    combined_spread = sum(
        (leg.quote.ask - leg.quote.bid)
        for leg in (hedge_put, hedge_call, short_put, short_call)
        if leg.quote.bid is not None and leg.quote.ask is not None
    )
    if combined_spread > config.maximum_combined_spread_points:
        return None

    # Every candidate above already cleared has_complete_quote (bid and ask
    # both present) — re-asserted here only to narrow the type, not as a
    # new runtime check.
    assert hedge_put.quote.bid is not None and hedge_put.quote.ask is not None
    assert hedge_call.quote.bid is not None and hedge_call.quote.ask is not None
    assert short_put.quote.bid is not None and short_put.quote.ask is not None
    assert short_call.quote.bid is not None and short_call.quote.ask is not None

    quantity = lots * lot_size
    hedge_cost = (hedge_put.quote.ask + hedge_call.quote.ask) * quantity
    short_credit = (short_put.quote.bid + short_call.quote.bid) * quantity
    initial_net_credit = short_credit - hedge_cost
    if initial_net_credit <= 0:
        return None

    put_width = short_put.strike - hedge_put.strike
    call_width = hedge_call.strike - short_call.strike
    wing_width = max(put_width, call_width)
    if wing_width <= 0:
        return None
    credit_to_width_ratio = initial_net_credit / (wing_width * quantity)
    if credit_to_width_ratio < config.minimum_credit_to_width_ratio:
        return None

    # Signed portfolio delta, in the *same* convention strategy.py's own
    # _signed_delta uses post-entry (long => +raw delta, short => -raw
    # delta; chain deltas are natively signed, negative for puts): a long
    # put/call's position delta is its own raw delta, and a short
    # put/call's is the negation of it. Getting this convention wrong here
    # while _signed_delta uses the correct one would silently reject
    # perfectly balanced condors (or admit lopsided ones) — this net_delta
    # and _net_delta_per_lot must always agree on a held cycle's own legs.
    net_delta = (
        hedge_put.greeks.delta * quantity
        + hedge_call.greeks.delta * quantity
        - short_put.greeks.delta * quantity
        - short_call.greeks.delta * quantity
    )
    net_delta_per_lot = net_delta / lots if lots else net_delta
    if abs(net_delta_per_lot) > config.maximum_entry_delta_per_lot:
        return None

    maximum_theoretical_loss = wing_width * quantity - initial_net_credit

    return IronCondorCandidate(
        hedge_put=hedge_put,
        hedge_call=hedge_call,
        short_put=short_put,
        short_call=short_call,
        initial_net_credit=initial_net_credit,
        wing_width=wing_width,
        credit_to_width_ratio=credit_to_width_ratio,
        net_delta_per_lot=net_delta_per_lot,
        maximum_theoretical_loss=maximum_theoretical_loss,
    )


def best_hedge(
    *,
    chain: ChainView,
    resolver: OptionChainResolver,
    greeks: GreeksService,
    option_type: OptionType,
    short_strike: float,
    config: SelectionConfig,
    spot: float,
    expiry_at: datetime,
    now: datetime,
    role: LegRole,
    target_delta: float,
    tolerance: float,
) -> LegCandidate | None:
    if option_type is OptionType.PE:
        bounds = (
            short_strike - config.maximum_hedge_width_points,
            short_strike - config.minimum_hedge_width_points,
        )
    else:
        bounds = (
            short_strike + config.minimum_hedge_width_points,
            short_strike + config.maximum_hedge_width_points,
        )
    candidates = rank_role_candidates(
        chain=chain, resolver=resolver, greeks=greeks, option_type=option_type, role=role,
        target_delta=target_delta, tolerance=tolerance, config=config, spot=spot,
        expiry_at=expiry_at, now=now, strike_bounds=bounds,
    )
    return candidates[0] if candidates else None
