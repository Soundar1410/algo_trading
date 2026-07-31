"""Strike resolution: ITM / ATM / OTM CE/PE selection.

Ported from the reference repository's ``framework/market_data/option_selector.py``
plus the simulated resolver from its ``option_chain.py`` (Phase 3 Part 2b-i).

Given the underlying spot and the strike step, find the nearest strike and build
the CE/PE contract to trade. The mapping from strike+expiry to a broker
``security_id`` is broker-specific, so it comes from an injected
:class:`OptionChainResolver`.

ITM/OTM offsets are sign-sensitive per option type: for a **call**, ITM means a
*lower* strike and OTM a *higher* one; for a **put** it is reversed.
:func:`resolve_strike` encodes that once, so strategies never reason about the
sign themselves — they declare ``moneyness`` and ``steps``.

The **Dhan** resolver is not ported here. Resolving real contracts needs the scrip
master, and this repository already has a live option-chain surface in
:mod:`common.market_data.option_chain` with its own throttle and freshness rules
(Phase 2). Wiring the two together belongs with the live path, not with the engine
port, so Part 2b-i ships the simulated resolver only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import Moneyness, OptionContract, OptionType


def nearest_atm_strike(spot: float, strike_step: int, offset: int = 0) -> int:
    """Round ``spot`` to the nearest tradable strike, applying an optional offset.

    Args:
        spot: underlying price.
        strike_step: gap between strikes (e.g. 50 for NIFTY).
        offset: number of strikes away from ATM (0 = pure ATM). Applied as
            ``offset * strike_step``; the sign convention is the caller's
            responsibility — see :func:`resolve_strike` for the CE/PE-aware form.
    """
    if strike_step <= 0:
        raise ValueError("strike_step must be positive")
    base = round(spot / strike_step) * strike_step
    return int(base + offset * strike_step)


def resolve_strike(
    spot: float,
    strike_step: int,
    option_type: OptionType,
    moneyness: Moneyness = Moneyness.ATM,
    steps: int = 0,
) -> int:
    """Resolve a strike for the given moneyness, correct for CE vs PE.

    For a call: ITM = lower strike (negative offset), OTM = higher strike
    (positive offset). For a put the directions invert. ``steps`` is always a
    non-negative "how many strikes away from ATM"; this applies the correct sign.
    """
    if moneyness is Moneyness.ATM or steps == 0:
        offset = 0
    elif option_type is OptionType.CE:
        offset = steps if moneyness is Moneyness.OTM else -steps
    else:  # PE
        offset = -steps if moneyness is Moneyness.OTM else steps
    return nearest_atm_strike(spot, strike_step, offset)


class OptionChainResolver(ABC):
    """Resolves a (strike, option_type, expiry) into a tradable contract."""

    @abstractmethod
    def resolve(
        self, strike: int, option_type: OptionType, expiry: str | None = None
    ) -> OptionContract: ...


class SimulatedOptionChainResolver(OptionChainResolver):
    """Builds synthetic but consistent contracts for paper/offline trading."""

    def __init__(self, underlying: str, lot_size: int, default_expiry: str = "WEEKLY") -> None:
        self._underlying = underlying
        self._lot_size = lot_size
        self._default_expiry = default_expiry

    def resolve(
        self, strike: int, option_type: OptionType, expiry: str | None = None
    ) -> OptionContract:
        exp = expiry or self._default_expiry
        return OptionContract(
            symbol=f"{self._underlying} {exp} {strike} {option_type.value}",
            security_id=f"SIM:{self._underlying}:{exp}:{strike}:{option_type.value}",
            strike=float(strike),
            option_type=option_type,
            expiry=exp,
            lot_size=self._lot_size,
        )


class OptionSelector:
    """Picks a CE/PE contract for a given spot using a chain resolver.

    ``default_moneyness``/``default_steps`` come from the strategy's own
    ``option_selection``; a per-signal override carried on a
    :class:`~common.engine.models.StrategySignal` takes precedence when provided.
    """

    def __init__(
        self,
        resolver: OptionChainResolver,
        strike_step: int,
        *,
        default_moneyness: Moneyness = Moneyness.ATM,
        default_steps: int = 0,
        expiry: str | None = None,
    ) -> None:
        self._resolver = resolver
        self._strike_step = strike_step
        self._default_moneyness = default_moneyness
        self._default_steps = default_steps
        self._expiry = expiry

    def select(
        self,
        spot: float,
        option_type: OptionType,
        moneyness: Moneyness | None = None,
        steps: int | None = None,
    ) -> OptionContract:
        strike = resolve_strike(
            spot,
            self._strike_step,
            option_type,
            moneyness if moneyness is not None else self._default_moneyness,
            steps if steps is not None else self._default_steps,
        )
        return self._resolver.resolve(strike, option_type, self._expiry)
