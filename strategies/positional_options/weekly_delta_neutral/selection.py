"""The atomic four-leg candidate search (spec section 3.5) — deterministic,
never relaxed. No candidate that clears every filter -> no entry, full stop.

**Filters, applied before any ranking (never relaxed when nothing
qualifies):**

1. valid same expiry and required option type — guaranteed by construction
   (the chain snapshot and resolver are both already scoped to one expiry).
2. fresh, complete quote and Greek inputs.
3. liquidity and per-leg delta-tolerance limits (``rank_role_candidates``).
4. hedge-width validity: a hedge candidate is only ever considered within
   its own short's 250-500 point band (``_side_combos``'s ``width_bounds``).
5. whole-basket validity: equal/positive resolved lot size, combined
   bid/ask spread, positive credit, positive wing width, credit/width
   ratio, and entry delta-per-lot tolerance (``_assemble_candidate``).

**Ranking, over every surviving complete four-leg combination** — the
deterministic order confirmed by independent review (do not reorder without
an authoritative spec change):

1. total distance from the four legs' own configured delta targets
   (ascending);
2. lowest absolute projected portfolio net delta per lot (ascending);
3. combined bid/ask spread across all four legs (ascending);
4. a stable strike/security-id tie-break.

**The search itself.** Each short role's candidate list is already small by
construction (tolerance-filtered around its target delta), so for every
short candidate this module enumerates every width-valid hedge candidate
for *that specific short's own strike* — not just the single nearest short's
hedge, which is exactly the shortcut this replaces (see
``test_weekly_delta_neutral_selection.py`` for a fixture where the nearest
short's own hedge fails the width band while a slightly-further-from-target
short has a fully valid one). Put-side and call-side combinations are
independent of each other (a put leg's width/credit does not depend on
which call combination is chosen), so the full basket search is the
Cartesian product of two already-bounded lists — operationally cheap even
in the worst case (each side's list is bounded by how many strikes clear
the configured delta tolerance, typically a handful), never a full scan of
every strike combination in the chain.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _SideCombo:
    """One side's (short, hedge) pairing — put side or call side — with the
    combined stats the whole-basket ranking needs. ``short``/``hedge`` are
    kept apart, never pre-summed into the candidate, so the two independent
    sides can be cross-joined without recomputing anything."""

    short: LegCandidate
    hedge: LegCandidate
    delta_distance: float
    spread: float


def _side_combos(
    *,
    chain: ChainView,
    resolver: OptionChainResolver,
    greeks: GreeksService,
    config: SelectionConfig,
    spot: float,
    expiry_at: datetime,
    now: datetime,
    shorts: list[LegCandidate],
    short_target_delta: float,
    hedge_option_type: OptionType,
    hedge_role: LegRole,
    hedge_target_delta: float,
    hedge_tolerance: float,
    width_bounds: Callable[[float], tuple[float, float]],
) -> list[_SideCombo]:
    """Every (short, hedge) pairing for one side — put or call — where the
    hedge is width-valid *for that specific short's own strike*. Never just
    the single nearest short's hedge."""
    combos: list[_SideCombo] = []
    for short in shorts:
        bounds = width_bounds(short.strike)
        hedge_candidates = rank_role_candidates(
            chain=chain,
            resolver=resolver,
            greeks=greeks,
            option_type=hedge_option_type,
            role=hedge_role,
            target_delta=hedge_target_delta,
            tolerance=hedge_tolerance,
            config=config,
            spot=spot,
            expiry_at=expiry_at,
            now=now,
            strike_bounds=bounds,
        )
        if not hedge_candidates:
            continue
        assert short.quote.bid is not None and short.quote.ask is not None
        short_distance = abs(short.greeks.delta - short_target_delta)
        short_spread = short.quote.ask - short.quote.bid
        for hedge in hedge_candidates:
            assert hedge.quote.bid is not None and hedge.quote.ask is not None
            hedge_distance = abs(hedge.greeks.delta - hedge_target_delta)
            hedge_spread = hedge.quote.ask - hedge.quote.bid
            combos.append(
                _SideCombo(
                    short=short,
                    hedge=hedge,
                    delta_distance=short_distance + hedge_distance,
                    spread=short_spread + hedge_spread,
                )
            )
    return combos


def select_iron_condor(
    *,
    chain: ChainView,
    resolver: OptionChainResolver,
    greeks: GreeksService,
    spot: float,
    expiry_at: datetime,
    now: datetime,
    lots: int,
    config: SelectionConfig,
) -> IronCondorCandidate | None:
    """The complete, atomic four-leg search over every valid combination.

    Returns ``None`` — never a partial or relaxed result — when no complete
    combination clears every filter in this module's own docstring. Never
    picks "the nearest short's own nearest hedge" independently per side and
    calls it done; every width-valid hedge for every qualifying short is a
    candidate combination, and the lowest-ranked complete one wins.
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

    put_combos = _side_combos(
        chain=chain, resolver=resolver, greeks=greeks, config=config, spot=spot,
        expiry_at=expiry_at, now=now, shorts=short_put_candidates,
        short_target_delta=config.short_put_delta, hedge_option_type=OptionType.PE,
        hedge_role=LegRole.HEDGE_PUT, hedge_target_delta=config.hedge_put_delta,
        hedge_tolerance=config.hedge_delta_tolerance,
        width_bounds=lambda strike: (
            strike - config.maximum_hedge_width_points,
            strike - config.minimum_hedge_width_points,
        ),
    )
    call_combos = _side_combos(
        chain=chain, resolver=resolver, greeks=greeks, config=config, spot=spot,
        expiry_at=expiry_at, now=now, shorts=short_call_candidates,
        short_target_delta=config.short_call_delta, hedge_option_type=OptionType.CE,
        hedge_role=LegRole.HEDGE_CALL, hedge_target_delta=config.hedge_call_delta,
        hedge_tolerance=config.hedge_delta_tolerance,
        width_bounds=lambda strike: (
            strike + config.minimum_hedge_width_points,
            strike + config.maximum_hedge_width_points,
        ),
    )
    if not put_combos or not call_combos:
        return None

    best: IronCondorCandidate | None = None
    best_key: tuple[float, float, float, float, float, float, float, str, str, str, str] | None = (
        None
    )
    for put_combo in put_combos:
        for call_combo in call_combos:
            candidate = _assemble_candidate(put_combo, call_combo, lots=lots, config=config)
            if candidate is None:
                continue
            key = _ranking_key(candidate, put_combo, call_combo)
            if best_key is None or key < best_key:
                best = candidate
                best_key = key
    return best


def _assemble_candidate(
    put_combo: _SideCombo, call_combo: _SideCombo, *, lots: int, config: SelectionConfig
) -> IronCondorCandidate | None:
    """Every whole-basket hard filter (spec section 3.6/3.7) — reject,
    never relax, exactly as the single-path version this replaces did."""
    hedge_put, short_put = put_combo.hedge, put_combo.short
    hedge_call, short_call = call_combo.hedge, call_combo.short

    # Spec section 3.1: lot size comes exclusively from each selected
    # contract's own resolved metadata — never a single value trusted from
    # the scrip master as a whole, and never a hardcoded/configured
    # constant. Verified here rather than assumed, and refused — not
    # guessed at — if it does not agree across all four legs.
    leg_lot_sizes = {
        hedge_put.contract.lot_size,
        hedge_call.contract.lot_size,
        short_put.contract.lot_size,
        short_call.contract.lot_size,
    }
    if len(leg_lot_sizes) != 1:
        return None
    lot_size = next(iter(leg_lot_sizes))
    if lot_size <= 0:
        return None

    combined_spread = put_combo.spread + call_combo.spread
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
    # delta; chain deltas are natively signed, negative for puts).
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
        lot_size=lot_size,
    )


def _ranking_key(
    candidate: IronCondorCandidate, put_combo: _SideCombo, call_combo: _SideCombo
) -> tuple[float, float, float, float, float, float, float, str, str, str, str]:
    """The deterministic ranking order confirmed by independent review:
    total delta distance, then lowest absolute projected portfolio delta,
    then combined spread, then a stable strike/security-id tie-break. A
    smaller tuple always wins (``min`` by lexicographic comparison)."""
    total_delta_distance = put_combo.delta_distance + call_combo.delta_distance
    combined_spread = put_combo.spread + call_combo.spread
    return (
        total_delta_distance,
        abs(candidate.net_delta_per_lot),
        combined_spread,
        candidate.short_put.strike,
        candidate.hedge_put.strike,
        candidate.short_call.strike,
        candidate.hedge_call.strike,
        candidate.short_put.contract.security_id,
        candidate.hedge_put.contract.security_id,
        candidate.short_call.contract.security_id,
        candidate.hedge_call.contract.security_id,
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
    """The single best hedge for one already-fixed short strike — used only
    by the hedge-repair path (spec section 7.2/12.3 step 2), which replaces
    exactly one leg against an already-open basket, not a fresh atomic
    four-leg search. ``select_iron_condor`` never calls this: it enumerates
    every width-valid hedge per short candidate itself (``_side_combos``),
    because the repair case and the entry-search case have different
    correctness requirements — repair has no sibling short to jointly
    optimise against.
    """
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
